/**
 * audio.ts - Web Audio utilities for call mode
 *
 * Handles:
 * - Microphone capture → PCM 16kHz mono Int16Array
 * - AnalyserNode for equalizer visualization
 * - Base64 WAV → AudioBuffer playback
 * - Barge-in (abort TTS playback)
 */

const TARGET_SAMPLE_RATE = 16000;

/** Status of the audio pipeline */
export interface AudioPipelineState {
	stream: MediaStream | null;
	audioContext: AudioContext | null;
	source: MediaStreamAudioSourceNode | null;
	analyser: AnalyserNode | null;
	processor: AudioWorkletNode | null;
}

let pipelineState: AudioPipelineState = {
	stream: null,
	audioContext: null,
	source: null,
	analyser: null,
	processor: null
};

let currentPlaybackSource: AudioBufferSourceNode | null = null;
let playbackContext: AudioContext | null = null;

/**
 * Asegura que el contexto de audio de reproducción exista y esté en marcha.
 *
 * Se reutiliza SIEMPRE el mismo AudioContext durante toda la vida de la página:
 * cerrarlo en cada stop agotaba el límite de contextos del navegador (~6) y, a
 * partir de ahí, `new AudioContext()` fallaba y el TTS quedaba mudo hasta
 * recargar la pestaña. Nunca lanza: un fallo al resumir no debe romper al llamador.
 */
export async function primePlayback(): Promise<void> {
	if (typeof AudioContext === 'undefined') return;
	try {
		if (!playbackContext || playbackContext.state === 'closed') {
			playbackContext = new AudioContext();
		}
		if (playbackContext.state === 'suspended') {
			// resume() puede rechazar (sin gesto de usuario) o quedarse colgado.
			await Promise.race([
				playbackContext.resume(),
				new Promise<void>((resolve) => setTimeout(resolve, 800))
			]);
		}
	} catch (error) {
		console.warn('[TTS] No se pudo preparar el contexto de audio:', error);
	}
}

/**
 * Start capturing microphone audio.
 * Calls onAudioChunk(pcmInt16: Int16Array) for each ~30ms frame (480 samples at 16kHz).
 *
 * Throws descriptive Error messages for:
 * - No microphone connected (NotFoundError)
 * - Permission denied by user (NotAllowedError)
 * - Microphone in use by another app (NotReadableError)
 * - No HTTPS / insecure context (SecurityError)
 */
export async function startMicrophoneCapture(
	onAudioChunk: (pcm: Int16Array) => void,
	onAnalyserReady?: (analyser: AnalyserNode) => void,
	deviceId?: string
): Promise<AudioPipelineState> {
	// Some browsers still block mic access on non-secure origins, but the app
	// should not hard-code a localhost-only restriction. Allow any HTTP origin
	// to attempt permission, and show a real browser error only if access is truly denied.
	if (!navigator.mediaDevices?.getUserMedia) {
		throw new Error(
			'Tu navegador no expone la API del micrófono. '
			+ 'Prueba en un navegador moderno y acepta el permiso de micrófono cuando aparezca el aviso.'
		);
	}

	let stream: MediaStream;
	try {
		stream = await navigator.mediaDevices.getUserMedia({
			audio: {
				...(deviceId ? { deviceId: { exact: deviceId } } : {}),					sampleRate: { ideal: TARGET_SAMPLE_RATE },
					channelCount: { ideal: 1 },
					// AGC + supresión de ruido ON: micrófonos con volumen bajo
					// (-24 dBFS) llegaban a Whisper vacío/alucinado. AGC del
					// navegador normaliza la ganancia de entrada.
					// EchoCancellation OFF: si no, el TTS del altavoz entra por el
					// mic y el navegador lo cancela como "eco", cortando la voz.
					echoCancellation: false,
					noiseSuppression: true,
					autoGainControl: true
			}
		});
	} catch (err) {
		if (err instanceof DOMException) {
			switch (err.name) {
				case 'NotFoundError':
					throw new Error(
						'No se detectó ningún micrófono. '
						+ 'Conecta un micrófono o verifica que no esté deshabilitado en la configuración del sistema.'
					);
				case 'NotAllowedError':
					throw new Error(
						'Permiso de micrófono denegado. '
						+ 'Haz clic en el icono 🔒/🎤 en la barra de direcciones del navegador para permitir el acceso al micrófono, '
						+ 'o revisa la configuración de permisos del sitio.'
					);
				case 'NotReadableError':
					throw new Error(
						'El micrófono está en uso por otra aplicación. '
						+ 'Cierra otras aplicaciones que usen el micrófono (Zoom, Teams, etc.) y vuelve a intentar.'
					);
				case 'OverconstrainedError':
					throw new Error(
						'El micrófono no soporta las configuraciones solicitadas. '
						+ 'Intenta con otro micrófono.'
					);
				default:
					throw new Error(
						`Error al acceder al micrófono: ${err.message}`
					);
			}
		}
		throw new Error(
			`Error inesperado al acceder al micrófono: ${err instanceof Error ? err.message : String(err)}`
		);
	}

	const audioContext = new AudioContext({ sampleRate: TARGET_SAMPLE_RATE });
	if (audioContext.state === 'suspended') {
		await audioContext.resume();
	}
	const source = audioContext.createMediaStreamSource(stream);
	const analyser = audioContext.createAnalyser();
	analyser.fftSize = 256;

	source.connect(analyser);

	// ScriptProcessorNode requires a power-of-two buffer size.
	// 1024 samples is approximately 64ms at 16kHz.
	await audioContext.audioWorklet.addModule('/audio-worklet-processor.js');
	const processor = new AudioWorkletNode(audioContext, 'call-audio-processor', {
		numberOfInputs: 1,
		numberOfOutputs: 1,
		outputChannelCount: [1]
	});
	let audioFrameCount = 0;

	processor.port.onmessage = (event: MessageEvent<Float32Array>) => {
		const float32Data = event.data;
		// Resample if AudioContext chose a different sample rate
		const resampled = resampleIfNeeded(float32Data, audioContext.sampleRate, TARGET_SAMPLE_RATE);

		// Convert float32 to int16
		const int16 = float32ToInt16(resampled);
		audioFrameCount += 1;
		if (audioFrameCount % 25 === 0) {
			const peak = int16.reduce((max, sample) => Math.max(max, Math.abs(sample)), 0);
			console.debug('[Piper Call] PCM frames sent:', audioFrameCount, { samples: int16.length, peak });
		}
		onAudioChunk(int16);
	};

	source.connect(analyser);
	source.connect(processor);
	processor.connect(audioContext.destination);

	pipelineState = { stream, audioContext, source, analyser, processor };

	if (onAnalyserReady) {
		onAnalyserReady(analyser);
	}

	return pipelineState;
}

/**
 * Stop microphone capture and release resources.
 */
export function stopMicrophoneCapture(): void {
	const { stream, audioContext, processor, source } = pipelineState;

	if (processor) {
		processor.disconnect();
	}
	if (source) {
		source.disconnect();
	}
	if (stream) {
		stream.getTracks().forEach((track) => track.stop());
	}
	if (audioContext && audioContext.state !== 'closed') {
		audioContext.close();
	}

	pipelineState = {
		stream: null,
		audioContext: null,
		source: null,
		analyser: null,
		processor: null
	};
}

/**
 * Play base64-encoded WAV audio. Returns a promise that resolves when playback ends.
 * Calling this again aborts any in-progress playback (barge-in).
 */
export async function playBase64Wav(base64Audio: string, playbackRate = 1): Promise<void> {
	const wavBytes = Uint8Array.from(atob(base64Audio), (c) => c.charCodeAt(0));
	await playWavBytes(wavBytes, playbackRate);
}

export async function playWavBytes(wavBytes: Uint8Array, playbackRate = 1): Promise<void> {
	stopCurrentPlayback();
	await primePlayback();
	const context = playbackContext;
	if (!context) throw new Error('Audio playback context unavailable');
	const audioBuffer = await decodeWav(wavBytes, context.sampleRate);

	const source = context.createBufferSource();
	source.buffer = audioBuffer;
	source.playbackRate.value = Math.max(0.5, Math.min(2, playbackRate));
	source.connect(context.destination);

	currentPlaybackSource = source;

	return new Promise<void>((resolve) => {
		source.onended = () => {
			currentPlaybackSource = null;
			resolve();
		};
		source.start();
	});
}

/**
 * Play PCM chunks in FIFO order with crossfade (streaming mode).
 * Chunks must be Int16 PCM (base64) with a `seq` field for ordering.
 * Crossfade of ~20ms eliminates tonal jumps between chunks.
 */
export interface FifoChunk {
	audio: string; // base64 PCM Int16
	sample_rate: number;
	seq: number;
	done?: boolean; // marca de fin de stream (backend manda {"done": true})
}

/**
 * FIFO streaming de una sola voz (valores piperstream.py):
 * - startFifoUtterance(owner): reclama el regulador global y aísla la cola.
 * - pushFifoChunk(s): encola por seq y dispara flush si no hay flush en curso.
 * - finishFifoUtterance(owner): marca fin de stream; el flush drena la cola.
 * - resetFifoPlayback(): invalida todo (barge-in / stop / nueva utterance prioritaria).
 * El flush ordena por seq y usa crossfade inline ya aplicado en el backend
 * (20ms). Con un solo owner solo puede sonar UNA voz a la vez: stream limpio.
 */
let fifoOwner: string | null = null;
let fifoStreaming = false;
let fifoChunks: FifoChunk[] = [];
let fifoNextSeq = 0;
let fifoPlaying = false;
/** Generación FIFO: un reset invalida el loop de reproducción en curso. */
let fifoGeneration = 0;

export function claimFifoUtterance(owner: string): void {
	// Reclama el regulador global para una nueva utterance.
	// La utterance anterior ya termino (su flush se drena por la cola FIFO
	// de call.svelte/autoSpeechQueue). NO invalidar por generacion aqui:
	// eso cortaria la voz anterior a mitad de frase (= multiples voces).
	// La continuidad la garantiza el remap de seq en pushFifoChunk.
	if (fifoOwner === owner) return; // misma utterance: no-op
	fifoOwner = owner;
	fifoStreaming = true;
	// NO limpiar fifoChunks ni tocar fifoNextSeq: el flush sigue drenando
	// la utterance anterior en orden; la nueva se encola tras ella (remap).
}

export function startFifoUtterance(owner: string): boolean {
	claimFifoUtterance(owner);
	return true;
}

/**
 * Push con remap de seq por utterance (stream limpio, valores piperstream.py).
 *
 * Cada utterance FIFO llega con seqs propios que SIEMPRE empiezan en 0
 * (el backend reinicia seq por request). Sin remap, el flush global
 * ordenaria mal (varias utterance con seq 0/1/2 mezcladas = multiples
 * voces / cortes). El remap asigna seqs globales monotonicos:
 *   seq_global = base_de_la_utterance + seq_del_chunk
 * Asi el flush reproduce en orden de llegada = UNA sola voz continua
 * con crossfade inline del backend (20ms).
 */
let fifoRemapBase = 0;
let fifoRemapOwner: string | null = null;
let fifoRemapNext = 0;

export function pushFifoChunk(chunk: FifoChunk, owner?: string): void {
	if (owner !== undefined && owner !== fifoOwner) return; // utterance vieja: descartar
	// Guardia anti-zombie: ignorar marcas de fin y eventos sin audio. Encolar un
	// chunk inválido (el backend cierra el SSE con {"done": true}, que JSON.parse
	// convierte en {seq: NaN, audio: undefined}) deja un "zombie" que NUNCA drena
	// (el flush nunca encuentra su seq) => fifoQueueLength() nunca vuelve a 0 y
	// el bucle de espera de speakStreaming se atasca 60s con el botón "enganchado".
	if (chunk.done) return;
	if (!chunk.audio || typeof chunk.seq !== 'number' || !Number.isFinite(chunk.seq)) return;
	// Nueva utterance (owner distinto): reservar base contigua tras lo encolado.
	if (owner !== undefined && owner !== fifoRemapOwner) {
		fifoRemapOwner = owner;
		fifoRemapBase = fifoRemapNext;
		// Si la cola está ociosa (la utterance anterior ya terminó), alinear el puntero
		// de consumo con la base nueva. Sin esto fifoNextSeq vuelve a 0 mientras los
		// chunks empiezan en fifoRemapBase > 0: el flush no encuentra su seq y TODAS
		// las reproducciones siguientes quedan mudas hasta recargar la página.
		if (fifoChunks.length === 0 && !fifoPlaying) {
			fifoNextSeq = fifoRemapBase;
		}
	}
	const remapped: FifoChunk = { ...chunk, seq: fifoRemapBase + chunk.seq };
	if (remapped.seq >= fifoRemapNext) fifoRemapNext = remapped.seq + 1;
	fifoChunks.push(remapped);
	fifoChunks.sort((x, y) => x.seq - y.seq);
	void flushFifoQueue();
}

export function pushFifoChunks(chunks: FifoChunk[]): void {
	if (chunks.length === 0) return;
	fifoChunks.push(...chunks);
	fifoChunks.sort((a, b) => a.seq - b.seq);
	void flushFifoQueue();
}

export function finishFifoUtterance(owner: string): void {
	if (fifoOwner !== owner) return;
	fifoStreaming = false;
	void flushFifoQueue();
}

export function isFifoOwner(owner: string): boolean {
	return fifoOwner === owner && fifoStreaming;
}

/** Longitud actual de la cola FIFO (para esperar drenado). */
export function fifoQueueLength(): number {
	return fifoChunks.length;
}

/** True si el flush está reproduciendo ahora mismo. */
export function isFifoPlaying(): boolean {
	return fifoPlaying;
}

async function flushFifoQueue(): Promise<void> {
	if (fifoPlaying) return;
	fifoPlaying = true;
	const myGen = fifoGeneration;
	// El try envuelve TODO (incluido primePlayback): si algo falla, el finally
	// libera fifoPlaying. Dejarlo en true bloqueaba para siempre cada flush
	// posterior (el TTS dejaba de sonar hasta recargar la página).
	try {
		try {
			await primePlayback();
		} catch (error) {
			console.warn('[TTS] Sin contexto de audio para reproducir:', error);
		}
		const context = playbackContext;
		if (!context) return;
		while (fifoChunks.length > 0) {
			if (myGen !== fifoGeneration) break; // utterance superada o detenida
			// fifoChunks está ordenado por seq. Si el seq esperado quedó por debajo
			// del mínimo encolado, ese chunk ya no llegará (utterance anterior o
			// reset): reanudar desde el primero disponible en lugar de quedarse mudo.
			if (fifoNextSeq < fifoChunks[0].seq) fifoNextSeq = fifoChunks[0].seq;
			const idx = fifoChunks.findIndex((c) => c.seq === fifoNextSeq);
			if (idx < 0) break; // esperar chunk faltante (llegó otro antes) / fin de stream
			const chunk = fifoChunks.splice(idx, 1)[0];
			fifoNextSeq = chunk.seq + 1;
			const pcmBytes = Uint8Array.from(atob(chunk.audio), (c) => c.charCodeAt(0));
			const samples = new Int16Array(pcmBytes.buffer);
			if (samples.length === 0) continue;
			// El crossfade (20 ms) ya viene aplicado inline desde el backend.
			const buffer = context.createBuffer(1, samples.length, chunk.sample_rate || 22050);
			const channel = buffer.getChannelData(0);
			for (let i = 0; i < samples.length; i++) {
				channel[i] = samples[i] / 32768;
			}
			const source = context.createBufferSource();
			source.buffer = buffer;
			source.connect(context.destination);
			currentPlaybackSource = source;
			const durationMs = (samples.length / (chunk.sample_rate || 22050)) * 1000;
			await new Promise<void>((resolve) => {
				let settled = false;
				const finish = () => {
					if (settled) return;
					settled = true;
					resolve();
				};
				source.onended = finish;
				// Watchdog: con el contexto suspendido onended puede no dispararse
				// nunca y el flush quedaría "enganchado" bloqueando TODO el TTS.
				setTimeout(finish, durationMs + 2000);
				source.start();
			});
			if (currentPlaybackSource === source) currentPlaybackSource = null;
		}
	} finally {
		if (myGen === fifoGeneration) {
			fifoPlaying = false;
			if (!fifoStreaming && fifoChunks.length === 0) {
				// Utterance terminada y drenada: liberar regulador y remap para que
				// la siguiente utterance empiece de cero de forma consistente.
				fifoOwner = null;
				fifoNextSeq = 0;
				fifoRemapOwner = null;
				fifoRemapBase = 0;
				fifoRemapNext = 0;
			}
		}
	}
}

export async function playFifoChunks(chunks: FifoChunk[]): Promise<void> {
	// Compat: utterance anónima de un solo lote.
	const owner = `bulk-${Date.now()}-${Math.random()}`;
	if (!startFifoUtterance(owner)) return;
	pushFifoChunks(chunks);
	finishFifoUtterance(owner);
	// Esperar drenado
	while ((fifoPlaying || fifoChunks.length > 0) && fifoOwner === owner) {
		await new Promise((r) => setTimeout(r, 50));
	}
}

export function resetFifoPlayback(): void {
	fifoGeneration++; // invalida el loop de reproducción en curso
	fifoOwner = null;
	fifoStreaming = false;
	fifoChunks = [];
	fifoNextSeq = 0;
	fifoPlaying = false;
	// El remap debe volver a cero junto con fifoNextSeq: si se conserva, la
	// siguiente utterance arranca en un seq alto que el flush nunca espera.
	fifoRemapOwner = null;
	fifoRemapBase = 0;
	fifoRemapNext = 0;
	if (currentPlaybackSource) {
		try {
			currentPlaybackSource.stop();
		} catch {
			// Already stopped
		}
		currentPlaybackSource = null;
	}
}

/**
 * Stop current TTS playback (barge-in).
 */
export function stopCurrentPlayback(): void {
	resetFifoPlayback();
	// El AudioContext NO se cierra a propósito: se reutiliza durante toda la vida
	// de la página. Cerrarlo en cada stop agotaba el límite de contextos del
	// navegador y el TTS dejaba de sonar hasta recargar la pestaña (F5).
}

/** Check if TTS is currently playing */
export function isPlaybackActive(): boolean {
	return currentPlaybackSource !== null;
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/**
 * Convert Float32Array to Int16Array (PCM 16-bit).
 */
function float32ToInt16(float32: Float32Array): Int16Array {
	const int16 = new Int16Array(float32.length);
	for (let i = 0; i < float32.length; i++) {
		const s = Math.max(-1, Math.min(1, float32[i]));
		int16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
	}
	return int16;
}

/**
 * Simple linear resampling from sourceRate to TARGET_SAMPLE_RATE.
 */
function resampleIfNeeded(samples: Float32Array, sourceRate: number, targetRate: number): Float32Array {
	if (sourceRate === targetRate) return samples;

	const ratio = sourceRate / targetRate;
	const newLength = Math.round(samples.length / ratio);
	const result = new Float32Array(newLength);
	for (let i = 0; i < newLength; i++) {
		const srcIndex = i * ratio;
		const index = Math.floor(srcIndex);
		const fraction = srcIndex - index;
		result[i] = samples[index] * (1 - fraction) + (samples[index + 1] || 0) * fraction;
	}
	return result;
}

/**
 * Decode raw WAV bytes to AudioBuffer using AudioContext.decodeAudioData.
 * Falls back to manual parsing if decodeAudioData fails.
 */
async function decodeWav(wavBytes: Uint8Array, targetSampleRate: number): Promise<AudioBuffer> {
	// Copia del buffer: decodeAudioData lo "detacha" y no se puede reutilizar.
	const arrayBuffer = wavBytes.buffer.slice(
		wavBytes.byteOffset,
		wavBytes.byteOffset + wavBytes.byteLength
	) as ArrayBuffer;
	// Decodificar con el contexto compartido. Crear un OfflineAudioContext por
	// reproducción agotaba el límite de AudioContexts del navegador (~6) y el TTS
	// dejaba de sonar tras las primeras reproducciones.
	try {
		if (!playbackContext || playbackContext.state === 'closed') {
			await primePlayback();
		}
		if (playbackContext) {
			return await playbackContext.decodeAudioData(arrayBuffer);
		}
	} catch {
		// Fall through to manual decode
	}

	// Manual WAV decode (PCM 16-bit mono)
	const view = new DataView(wavBytes.buffer, wavBytes.byteOffset, wavBytes.byteLength);

	// Find 'data' chunk
	let dataOffset = 44; // Standard WAV header size
	let dataSize = wavBytes.length - 44;

	// Parse header for actual data position
	const channels = view.getUint16(22, true);
	const sampleRate = view.getUint32(24, true);
	const bitsPerSample = view.getUint16(34, true);

	// Scan for 'data' chunk
	for (let i = 12; i < wavBytes.length - 8; i++) {
		if (
			wavBytes[i] === 0x64 &&
			wavBytes[i + 1] === 0x61 &&
			wavBytes[i + 2] === 0x74 &&
			wavBytes[i + 3] === 0x61
		) {
			dataSize = view.getUint32(i + 4, true);
			dataOffset = i + 8;
			break;
		}
	}

	const numSamples = Math.floor(dataSize / (bitsPerSample / 8));
	const float32 = new Float32Array(numSamples);

	if (bitsPerSample === 16) {
		for (let i = 0; i < numSamples; i++) {
			float32[i] = view.getInt16(dataOffset + i * 2, true) / 32768;
		}
	} else if (bitsPerSample === 32) {
		for (let i = 0; i < numSamples; i++) {
			float32[i] = view.getFloat32(dataOffset + i * 4, true);
		}
	}

	// Resample if needed
	const resampled = resampleIfNeeded(float32, sampleRate, targetSampleRate);

	const audioBuffer = new AudioBuffer({
		length: resampled.length,
		numberOfChannels: 1,
		sampleRate: targetSampleRate
	});
	audioBuffer.getChannelData(0).set(resampled);

	return audioBuffer;
}
