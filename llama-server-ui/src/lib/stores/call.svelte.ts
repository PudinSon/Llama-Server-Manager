/**
 * callStore - Reactive state for WebSocket call mode + TTS configuration
 *
 * Manages:
 * - WebSocket connection to piper_service (/ws/call)
 * - Call lifecycle (start, stop, mute)
 * - VAD events (score, speech_start, speech_end)
 * - Equalizer data (frequency bins)
 * - TTS playback state
 * - TTS settings (voice, speed, enabled) with localStorage persistence
 * - Available voices list (fetched from /voices)
 * - HTTP-based synthesize for chat TTS (non-call mode)
 */

import {
	playBase64Wav,
	primePlayback,
	startFifoUtterance,
	pushFifoChunk,
	finishFifoUtterance,
	fifoQueueLength,
	isFifoPlaying,
	type FifoChunk,
	startMicrophoneCapture,
	stopCurrentPlayback,
	stopMicrophoneCapture
} from '$lib/utils/audio';
import { getGroupedVoices, getVoiceLang, sanitizeTextForTTS, VOICE_METADATA, type LanguageGroup, DEFAULT_PIPER_VOICE } from '$lib/data/piper-voices';
import { serviceBase, serviceWsUrl } from '$lib/api';

export type CallState = 'idle' | 'connecting' | 'active' | 'ending';
export type CallPhase = 'IDLE' | 'LISTENING' | 'TRANSCRIBING' | 'WAITING_LLM' | 'SPEAKING' | 'INTERRUPTED' | 'FINISHED' | 'ERROR';
export interface CallSessionTurn {
	user: string;
	assistant: string;
	reasoning: string;
}

// Resueltos según el modo: `ws://host:8880/ws/call` en directo o
// `wss://dominio/ws/call` detrás de nginx (location /ws/).
const PIPER_WS_URL = serviceWsUrl('piper', '/ws/call');
// El servicio Piper expone /health, /synthesize... : tras nginx se prefija /piper.
const PIPER_HTTP_URL = serviceBase('piper');
const TTS_SETTINGS_KEY = 'LlamaUi.ttsSettings';

interface TTSSettings {
	enabled: boolean;
	autoSpeak: boolean;
	voice: string;
	speed: number;
	lang: string;
}

const DEFAULT_TTS_SETTINGS: TTSSettings = {
	enabled: false,
	autoSpeak: false,
	voice: DEFAULT_PIPER_VOICE,
	speed: 1.0,
	lang: 'es'
};

// Streaming como piperstream.py: hablar el fragmento acumulado cuando llega a
// CHAR_THRESHOLD caracteres o pasan TIMEOUT_SECONDS, SIN esperar a puntuación
// de frase completa. Da latencia baja (se pronuncia conforme llega el LLM).
const AUTO_CHAR_THRESHOLD = 150;
const AUTO_TIMEOUT_MS = 3000;

class CallStore {
	// ---------------------------------------------------------------------------
	// Call state
	// ---------------------------------------------------------------------------
	/** Current call state */
	state = $state<CallState>('idle');
	callPhase = $state<CallPhase>('IDLE');

	/** Current VAD score (0..1) for equalizer */
	vadScore = $state(0);

	/**
	 * Nivel real de entrada del micrófono (0..1, pico normalizado de PCM int16).
	 * Se mide en el worklet callback (dominio del tiempo), a diferencia del
	 * equalizer (frecuencias). Sirve para que el usuario vea ANTES de hablar si
	 * su micrófono está demasiado bajo (ej. -24 dBFS => Whisper no transcribe).
	 */
	micInputLevel = $state(0);

	/** Whether user is currently speaking */
	isSpeaking = $state(false);

	/** Whether TTS is currently playing */
	isTtsPlaying = $state(false);

	/** Last error */
	error = $state<string | null>(null);

	/** Last TTS error (separate from call error for UI display) */
	ttsError = $state<string | null>(null);

	// ---------------------------------------------------------------------------
	// TTS settings (persisted in localStorage)
	// ---------------------------------------------------------------------------
	/** Whether TTS is enabled for chat responses */
	ttsEnabled = $state(false);

	/** Whether auto-speak is enabled (auto-read assistant responses) */
	autoSpeak = $state(false);

	/** Selected voice name */
	voice = $state(DEFAULT_PIPER_VOICE);

	/** TTS speed multiplier */
	ttsSpeed = $state(1.0);

	/** TTS language */
	ttsLang = $state('es');

	/** Language forced for call transcription; fixed language improves short utterances. */
	sttLanguage = $state('es');

	/** Whether the call backend is transcribing or waiting for the chat response. */
	isCallProcessing = $state(false);

	/** Owner FIFO del TTS de la llamada (backend STT→LLM→TTS por WS). */
	private callFifoOwner: string | null = null;

	/** Cola FIFO: una sola utterance de auto-speak a la vez; evita voces múltiples. */
	private autoSpeechQueue: Promise<void> = Promise.resolve();
	/** Generación: si un auto-speak viejo termina después de uno nuevo, no repite. */
	private autoSpeechGeneration = 0;

	/** Time window used to exclude call-originated messages from automatic TTS. */
	private callStartedAt = 0;
	private callEndedAt = 0;
	private callResponsePending = false;
	private callAutoSpeakMessageIds = new Set<string>();

	/** Available voices fetched from server */
	availableVoices = $state<string[]>([]);

	/** Voices grouped by language */
	voiceGroups = $state<LanguageGroup[]>([]);

	/** Whether voices have been loaded */
	voicesLoaded = $state(false);

	/** ID of the last message that was auto-spoken (to avoid repeating) */
	private lastAutoSpokenMsgId: string | null = null;

	/** Whether auto-speak is currently in progress */
	autoSpeakInProgress = $state(false);

	/** Texto ya hablado por mensaje (para streaming incremental) */
	private autoSpokenTextByMsg: Map<string, string> = new Map();

	/** Inicio temporal (ms) del fragmento streaming actual; 0 = sin fragmento en curso. */
	private autoSpeechFragmentStartAt = 0;

	/** Mensaje cuyo streaming está activo; cambia invalida fragmentos de mensajes viejos. */
	private autoSpeechCurrentMsgId: string | null = null;

	/** Contador para generar owners FIFO únicos por fragmento (evita colisión de seq). */
	private autoSpeechFragmentSeq = 0;

	/** Transcript events from call pipeline */
	transcriptEvents = $state<Array<{ role: string; text: string; timestamp: number }>>([]);
	private callSessionTurns: CallSessionTurn[] = [];
	private sessionFinishedHandler: ((turns: CallSessionTurn[]) => void) | null = null;
	callAssistantText = $state('');
	callReasoningText = $state('');

	inputDevices = $state<MediaDeviceInfo[]>([]);
	selectedInputDeviceId = $state('');

	private ws: WebSocket | null = null;
	private audioAnalyser: AnalyserNode | null = null;
	private animFrame: number | null = null;
	private speechAbortController: AbortController | null = null;
	private speechQueue: Promise<void> = Promise.resolve();
	private speechGeneration = 0;
	private callSpeechConsumed = 0;
	private transcriptHandler: ((text: string) => void) | null = null;

	constructor() {
		this.loadSettings();
	}

	// ---------------------------------------------------------------------------
	// Settings persistence
	// ---------------------------------------------------------------------------

	private loadSettings(): void {
		try {
			const raw = localStorage.getItem(TTS_SETTINGS_KEY);
			if (raw) {
				const saved: Partial<TTSSettings> = JSON.parse(raw);
				this.ttsEnabled = saved.enabled ?? DEFAULT_TTS_SETTINGS.enabled;
				this.autoSpeak = saved.autoSpeak ?? DEFAULT_TTS_SETTINGS.autoSpeak;
				const savedVoice = saved.voice ?? DEFAULT_TTS_SETTINGS.voice;
				this.voice = this.normalizeVoiceId(savedVoice);
				this.ttsSpeed = saved.speed ?? DEFAULT_TTS_SETTINGS.speed;
				this.ttsLang = saved.lang ?? getVoiceLang(this.voice) ?? DEFAULT_TTS_SETTINGS.lang;
			} else {
				this.voice = DEFAULT_TTS_SETTINGS.voice;
				this.ttsLang = DEFAULT_TTS_SETTINGS.lang;
			}
		} catch {
			this.voice = DEFAULT_TTS_SETTINGS.voice;
			this.ttsLang = DEFAULT_TTS_SETTINGS.lang;
		}
	}

	private normalizeVoiceId(voice: string, availableVoices: string[] = this.availableVoices): string {
		if (!voice) return DEFAULT_TTS_SETTINGS.voice;

		const trimmed = voice.trim();
		if (!trimmed) return DEFAULT_TTS_SETTINGS.voice;

		if (availableVoices.includes(trimmed)) return trimmed;
		if (VOICE_METADATA[trimmed]) return trimmed;

		const exactByName = Object.entries(VOICE_METADATA)
			.filter(([voiceId, meta]) => availableVoices.includes(voiceId) && meta.name.toLowerCase() === trimmed.toLowerCase())
			.map(([voiceId]) => voiceId);
		if (exactByName.length > 0) {
			const preferredOrder = ['es_MX-claude-14947-epoch-high', 'es_MX-cortana-19669-epoch-high', 'es_MX-emma-high', 'es_MX-laura-high'];
			const preferred = preferredOrder.find((id) => exactByName.includes(id));
			if (preferred) return preferred;
			return exactByName[0];
		}

		const spanishFallback = ['es_MX-claude-14947-epoch-high', 'es_MX-cortana-19669-epoch-high', 'es_MX-emma-high', 'es_MX-laura-high'].find((id) => availableVoices.includes(id));
		if (spanishFallback) return spanishFallback;

		if (availableVoices.length > 0) return availableVoices[0];
		return DEFAULT_TTS_SETTINGS.voice;
	}

	private saveSettings(): void {
		const settings: TTSSettings = {
			enabled: this.ttsEnabled,
			autoSpeak: this.autoSpeak,
			voice: this.voice,
			speed: this.ttsSpeed,
			lang: this.ttsLang
		};
		try {
			localStorage.setItem(TTS_SETTINGS_KEY, JSON.stringify(settings));
		} catch {
			// Ignore
		}
	}

	/** Update TTS settings and persist */
	updateTTS(settings: Partial<TTSSettings>): void {
		if (settings.enabled !== undefined) this.ttsEnabled = settings.enabled;
		if (settings.voice !== undefined) {
			this.voice = settings.voice;
			const voiceLang = getVoiceLang(settings.voice);
			if (voiceLang) this.ttsLang = voiceLang;
		}
		if (settings.speed !== undefined) this.ttsSpeed = settings.speed;
		if (settings.lang !== undefined) this.ttsLang = settings.lang;
		this.saveSettings();
	}

	/** Toggle TTS on/off */
	toggleTTS(): void {
		if (this.ttsEnabled) {
			this.stopPlayback();
		}
		this.ttsEnabled = !this.ttsEnabled;
		this.ttsError = null;
		this.saveSettings();
	}

	/** Toggle auto-speak on/off */
	toggleAutoSpeak(): void {
		this.autoSpeak = !this.autoSpeak;
		this.saveSettings();
	}

	async loadInputDevices(): Promise<void> {
		if (!navigator.mediaDevices?.enumerateDevices) return;
		try {
			const devices = await navigator.mediaDevices.enumerateDevices();
			this.inputDevices = devices.filter((device) => device.kind === 'audioinput');
			if (!this.inputDevices.some((device) => device.deviceId === this.selectedInputDeviceId)) {
				this.selectedInputDeviceId = this.inputDevices[0]?.deviceId ?? '';
			}
			console.info('[Piper Call] Input devices:', this.inputDevices.map((device) => ({ id: device.deviceId, label: device.label })));
		} catch (err) {
			console.warn('[Piper Call] Could not enumerate input devices:', err);
		}
	}

	setInputDevice(deviceId: string): void {
		this.selectedInputDeviceId = deviceId;
	}

	setTranscriptHandler(handler: ((text: string) => void) | null): void {
		this.transcriptHandler = handler;
	}

		setSessionFinishedHandler(handler: ((turns: CallSessionTurn[]) => void) | null): void {
			this.sessionFinishedHandler = handler;
		}

		getSessionTranscript(): CallSessionTurn[] {
			return this.callSessionTurns.map((turn) => ({ ...turn }));
		}

		private commitPartialCallTurn(): void {
			const turn = this.callSessionTurns[this.callSessionTurns.length - 1];
			if (!turn) return;
			turn.assistant = this.callAssistantText;
			turn.reasoning = this.callReasoningText;
		}

	setCallResponsePending(pending: boolean): void {
		this.callResponsePending = pending;
	}

	pauseListening(): void {
		this.sendMessage({ type: 'pause_listening' });
	}

	resumeListening(): void {
		this.sendMessage({ type: 'resume_listening' });
	}

	// ---------------------------------------------------------------------------
	// Voices
	// ---------------------------------------------------------------------------

	/** Fetch available voices from the Piper service */
	async fetchVoices(): Promise<void> {
		const voiceEndpoints = [`${PIPER_HTTP_URL}/v1/audio/voices`, `${PIPER_HTTP_URL}/v1/voices`, `${PIPER_HTTP_URL}/voices`];
		for (const endpoint of voiceEndpoints) {
			try {
				const resp = await fetch(endpoint);
				if (!resp.ok) continue;
				const data = await resp.json();
				const voiceList: Array<string | { id?: string; name?: string }> = Array.isArray(data.voices) ? data.voices : [];
				const serverVoices = voiceList
					.map((voice) => typeof voice === 'string' ? voice : (voice.id ?? voice.name ?? ''))
					.filter((voice): voice is string => Boolean(voice));
				if (!serverVoices.length) continue;
				this.availableVoices = serverVoices.filter((voiceId: string) => !!VOICE_METADATA[voiceId]);
				this.voiceGroups = getGroupedVoices(this.availableVoices);
				this.voicesLoaded = true;
				const resolvedVoice = this.normalizeVoiceId(this.voice, this.availableVoices);
				if (resolvedVoice !== this.voice || !this.availableVoices.includes(this.voice)) {
					this.voice = resolvedVoice;
					this.ttsLang = getVoiceLang(this.voice);
					this.saveSettings();
				}
				console.info('[Piper TTS] Voices loaded from', endpoint, this.availableVoices);
				return;
			} catch (err) {
				console.warn(`[Piper TTS] Failed to fetch voices from ${endpoint}:`, err);
			}
		}
		console.warn('[Piper TTS] No voice list available from Piper FastAPI');
	}

	// ---------------------------------------------------------------------------
	// HTTP TTS (for chat mode, non-call)
	// ---------------------------------------------------------------------------

	/**
	 * Reproducción MANUAL (bocina al final de la respuesta y botón "Probar" de ajustes).
	 *
	 * Envía el TEXTO COMPLETO a Piper (/synthesize) y reproduce el WAV cuando Piper
	 * termina: sin streaming por chunks, sin trocear la respuesta y sin razonamiento
	 * (se descarta en sanitizeTextForTTS). Una sola voz continua de principio a fin.
	 */
	async synthesizeAndPlay(text: string): Promise<void> {
		if (!text.trim()) return;

		const cleanText = sanitizeTextForTTS(text);
		if (!cleanText) return;

		this.stopPlayback();
		console.info('[Piper TTS] Manual (texto completo):', {
			chars: cleanText.length,
			voice: this.voice,
			lang: this.ttsLang,
			speed: this.ttsSpeed
		});
		try {
			await this.speakComplete(cleanText, this.speechGeneration);
		} catch (err) {
			if (err instanceof DOMException && err.name === 'AbortError') return;
			if (err instanceof Error && err.name === 'AbortError') return;
			console.error('[Piper TTS] Manual synthesize error:', err);
			this.ttsError =
				err instanceof Error ? err.message : 'No se pudo conectar al servicio Piper en puerto 8880';
		}
	}

	async primePlayback(): Promise<void> {
		try {
			await primePlayback();
		} catch (error) {
			console.warn('[Piper TTS] Audio playback could not be primed:', error);
		}
	}

	private queueCallParagraphs(final: boolean): void {
		// Voces simultaneas en llamada: el evento 'assistant_delta' llegaba MUY
		// rapido (cada token o cada micro-lote) y llamaba a queueCallParagraphs
		// con fragmentos incompletos. Limitar cada flush a UNA utterance de
		// ~150 chars (piperstream.py CHAR_THRESHOLD) y a parrafos/oraciones
		// COMPLETAS mantiene el stream FIFO limpio: una voz, sin solapados.
		const pending = this.callAssistantText.slice(this.callSpeechConsumed);
		if (!pending.trim()) return;
		// Umbral: 150 chars antes de empezar a hablar (igual que piperstream.py).
		// Sin este gate, cada delta encolaba un fetch HTTP -> saturacion.
		if (!final && pending.trim().length < 150) return;
		const chunks: string[] = [];
		let consumed = 0;
		const paragraphBoundary = /\n\s*\n/g;
		let match: RegExpExecArray | null;
		while ((match = paragraphBoundary.exec(pending)) !== null) {
			const end = match.index + match[0].length;
			const paragraph = pending.slice(consumed, match.index).trim();
			if (paragraph) chunks.push(sanitizeTextForTTS(paragraph));
			consumed = end;
		}
		if (!final && consumed === 0 && this.callSpeechConsumed === 0) {
			// Solo parrafos/oraciones COMPLETAS (>=60 chars + puntuacion final).
			// NO hablar fragmentos a medias: el siguiente delta completara la oracion.
			const firstSentence = pending.match(/^.{60,}?[.!?](?:\s|$)/s)?.[0];
			if (firstSentence && firstSentence.trim().length >= 60) {
				chunks.push(sanitizeTextForTTS(firstSentence));
				consumed = firstSentence.length;
			}
		}
		if (final && consumed < pending.length) {
			const remainder = sanitizeTextForTTS(pending.slice(consumed));
			if (remainder) chunks.push(remainder);
			consumed = pending.length;
		}
		if (!consumed || !chunks.length) return;
		this.callSpeechConsumed += consumed;
		console.info('[Piper Call] Queueing completed paragraph(s)', { count: chunks.length });
		this.sendMessage({ type: 'tts_playing' });
		this.enqueueSpeechChunks(chunks, false, true);
	}

	private enqueueSpeechChunks(chunks: string[], requireEnabled = true, callAudio = false): Promise<void> {
		const generation = this.speechGeneration;
		this.speechQueue = this.speechQueue.then(async () => {
			for (const chunk of chunks) {
				if (generation !== this.speechGeneration || (requireEnabled && !this.ttsEnabled)) return;
				await this.speakStreaming(chunk, generation);
			}
		}).catch((err) => {
			if (err instanceof DOMException && err.name === 'AbortError') return;
			if (err instanceof Error && err.name === 'AbortError') return;
			console.error('[Piper TTS] queue error:', err);
			this.ttsError = err instanceof Error ? err.message : 'No se pudo reproducir el audio';
		}).finally(() => {
			if (callAudio) this.sendMessage({ type: 'tts_stopped' });
		});
		return this.speechQueue;
	}

	/**
	 * Síntesis COMPLETA (sin streaming): POST a /synthesize y reproduce el WAV entero
	 * de una sola vez. Es la base del modo manual (bocina de la respuesta / botón de prueba).
	 *
	 * El timeout escala con el texto porque Piper sintetiza todo en una sola pasada; si
	 * se supera se aborta y se reporta un error real (no un AbortError silencioso).
	 */
	private async speakComplete(text: string, generation: number): Promise<void> {
		this.speechAbortController?.abort();
		const controller = new AbortController();
		this.speechAbortController = controller;
		const timeoutMs = Math.max(60_000, Math.min(300_000, text.length * 80));
		let timedOut = false;
		const timeoutId = setTimeout(() => {
			timedOut = true;
			controller.abort();
		}, timeoutMs);
		this.isTtsPlaying = true;
		try {
			const resp = await fetch(`${PIPER_HTTP_URL}/synthesize`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				signal: controller.signal,
				body: JSON.stringify({ text, voice: this.voice, speed: this.ttsSpeed })
			});
			if (generation !== this.speechGeneration) return;
			if (!resp.ok) throw new Error(`Error del servidor Piper (HTTP ${resp.status})`);
			const data = (await resp.json()) as { audio?: string; error?: string };
			if (data.error) throw new Error(data.error);
			if (!data.audio) throw new Error('Piper no devolvió audio para el texto enviado');
			if (generation !== this.speechGeneration) return;
			await playBase64Wav(data.audio, this.ttsSpeed);
		} catch (err) {
			if (timedOut) {
				throw new Error(`Piper tardó más de ${Math.round(timeoutMs / 1000)} s en sintetizar`);
			}
			throw err;
		} finally {
			clearTimeout(timeoutId);
			if (this.speechAbortController === controller) this.speechAbortController = null;
			if (generation === this.speechGeneration) this.isTtsPlaying = false;
		}
	}

	/**
	 * Streaming limpio (UNA sola voz, valores piperstream.py). Modo automático y llamada.
	 *
	 * Consume el SSE de /synthesize/stream y encola cada chunk en el FIFO de audio, que
	 * reproduce en vivo con el crossfade inline del backend. Reclama el regulador FIFO
	 * global para que nunca suenen dos voces a la vez.
	 */
	private async speakStreaming(
		text: string,
		generation: number,
		utteranceOwner?: string
	): Promise<void> {
		const inAutoQueue = utteranceOwner !== undefined;
		// Owner FIFO ÚNICO por llamada (nunca compartido entre fragmentos). Cada
		// request /synthesize/stream reinicia seq a 0 en el backend; si dos
		// fragmentos comparten el MISMO owner, el remap FIFO no avanza la base y
		// los seq colisionan => chunks huérfanos y 1 min de espera. Con owner
		// único por fragmento, el remap reserva una base contigua y el flush
		// reproduce en orden continuo (UNA sola voz, como piperstream.py).
		const owner = utteranceOwner ?? `stream-${Date.now()}-${Math.random().toString(36).slice(2)}`;
		startFifoUtterance(owner);
		// Solo abortar el fetch anterior si NO venimos de la cola auto (la cola ya
		// serializa: abortar aqui cortaria la frase anterior del mismo mensaje).
		if (!inAutoQueue) {
			this.speechAbortController?.abort();
			this.speechAbortController = new AbortController();
		}
		// Aborter garantizado para este fetch (en modo cola auto puede ser null).
		const aborter = this.speechAbortController ?? new AbortController();
		// Timeout solo para ESTABLECER la conexión: si Piper no responde, no dejamos el
		// botón "enganchado". Una vez llegan chunks, el ritmo lo marca el propio flujo.
		let connectTimedOut = false;
		const connectTimeout = setTimeout(() => {
			connectTimedOut = true;
			aborter.abort();
		}, 30000);
		this.isTtsPlaying = true;
		this.sendMessage({ type: 'tts_playing' });
		// Reader del SSE declarado fuera del try para poder cancelarlo en finally:
		// un reader sin cerrar LEAKEA conexiones HTTP (el navegador las mantiene
		// activas, ~6 por host) => pool agotado => /health y nuevos streams se
		// bloquean => "Piper no disponible" y el botón queda "enganchado" hasta F5.
		let reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
		try {
			const resp = await fetch(`${PIPER_HTTP_URL}/synthesize/stream`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				signal: aborter.signal,
				body: JSON.stringify({
					text, voice: this.voice, speed: this.ttsSpeed
				})
			});
			clearTimeout(connectTimeout);
			if (generation !== this.speechGeneration) return;
			if (!resp.ok || !resp.body) throw new Error(`Error stream Piper (HTTP ${resp.status})`);
			reader = resp.body.getReader();
			const decoder = new TextDecoder();
			let buffer = '';
			let sawDone = false;
			while (true) {
				const { done, value } = await reader.read();
				if (done) break;
				buffer += decoder.decode(value, { stream: true });
				const lines = buffer.split('\n');
				buffer = lines.pop() ?? '';
				for (const line of lines) {
					if (!line.startsWith('data: ')) continue;
					const payload = line.slice(6);
					if (payload === '[DONE]') { sawDone = true; break; }
					let chunk: FifoChunk & { type?: string; message?: string };
					try {
						chunk = JSON.parse(payload) as FifoChunk & { type?: string; message?: string };
					} catch {
						continue; // ignore non-JSON SSE lines
					}
					// El backend cierra el stream con {"done": true} (NO el literal
					// '[DONE]'). Si no lo tratamos como fin, se encola como chunk
					// inválido (seq NaN) que nunca drena y el botón se atasca 60s.
					if (chunk.done === true) { sawDone = true; break; }
					// Errores de síntesis reportados por el backend en el propio SSE:
					// sin esto se ignoraban y la UI quedaba muda sin explicación.
					if (chunk.type === 'error') {
						throw new Error(chunk.message || 'Error de síntesis en Piper');
					}
					if (generation !== this.speechGeneration) return;
					pushFifoChunk(chunk, owner); // remap por utterance: UNA voz en stream
				}
				if (sawDone) break;
			}
			// Cerrar la utterance para que el flush pueda drenar en orden.
			finishFifoUtterance(owner);
			// Reproducción desacoplada como piperstream.py: el player FIFO suena en
			// background mientras la cola sigue. En auto NO esperamos el drenado.
			if (!inAutoQueue) {
				// El tope escala con el texto: una respuesta larga tarda más en
				// reproducirse, pero nunca bloqueamos indefinidamente.
				const drainCapMs = Math.max(30000, Math.min(600000, text.length * 120));
				const startedAt = Date.now();
				while (Date.now() - startedAt < drainCapMs) {
					if (generation !== this.speechGeneration) break;
					if (fifoQueueLength() === 0 && !isFifoPlaying()) break;
					await new Promise((r) => setTimeout(r, 50));
				}
			}
		} catch (err) {
			// Distinguir el timeout de conexión de un abort del usuario.
			if (connectTimedOut) {
				throw new Error('Piper no respondió (timeout de conexión en 30 s)');
			}
			throw err;
		} finally {
			clearTimeout(connectTimeout);
			// Cerrar la conexión SSE SIEMPRE (tras done, error o abort). Dejar el
			// reader sin cancelar LEAKEA conexiones HTTP: cada stream abierto y no
			// consumido hasta el final agota el pool del navegador (~6 por host) =>
			// /health y los siguientes /synthesize/stream quedan bloqueados =>
			// "Piper no disponible" (health 3s timeout) y botón enganchado hasta F5.
			try {
				await reader?.cancel();
			} catch {
				// ya cerrada o cancelada
			}
			// Cerrar la utterance también en error/abort: si no, fifoStreaming se
			// queda en true y el FIFO no vuelve a estado ocioso.
			try {
				finishFifoUtterance(owner);
			} catch {
				// no-op
			}
			this.sendMessage({ type: 'tts_stopped' });
			if (this.speechAbortController === aborter) this.speechAbortController = null;
			if (generation === this.speechGeneration) this.isTtsPlaying = false;
		}
	}

	/** Feed only newly completed phrases from a live assistant response. */
	updateAutoSpeech(msgId: string, text: string, isStreaming: boolean): void {
		if (
			!this.autoSpeak ||
			!this.ttsEnabled ||
			!text.trim()
		) return;

		// Obtener el último texto que ya se habló para este mensaje
		const prevSpoken = this.autoSpokenTextByMsg.get(msgId) ?? '';

		// Owner FIFO único por fragmento: el backend reinicia seq a 0 por request,
		// así que el remap necesita una base nueva por utterance para no colisionar.
		const newOwner = () => `auto-${msgId}-${++this.autoSpeechFragmentSeq}`;
		// Los fragmentos del MISMO mensaje comparten generación (no incrementar por
		// fragmento, eso invalidaba los anteriores en cola y "no se oían").
		if (this.autoSpeechCurrentMsgId !== msgId) {
			this.autoSpeechCurrentMsgId = msgId;
			this.autoSpeechGeneration += 1; // invalida utterances de OTRO mensaje
		}
		const gen = this.autoSpeechGeneration;
		const enqueue = (speakText: string, owner: string) => {
			this.autoSpeechQueue = this.autoSpeechQueue
				.then(() => {
					if (gen !== this.autoSpeechGeneration) return;
					if (this.autoSpeechCurrentMsgId !== msgId) return;
					return this.speakStreaming(speakText, this.speechGeneration, owner);
				})
				.catch((err) => {
					console.error('[Piper TTS] auto stream error:', err);
				});
		};

		if (isStreaming) {
			// Streaming como piperstream.py: hablar el fragmento acumulado cuando
			// llega a AUTO_CHAR_THRESHOLD (150) caracteres o pasan AUTO_TIMEOUT_MS
			// (3s), SIN esperar a puntuación de frase completa. Así se pronuncia
			// conforme llega el LLM (latencia baja), no todo al final.
			const newText = text.slice(prevSpoken.length);
			if (!newText.trim()) return;

			// Marcar el inicio temporal del fragmento actual la primera vez que
			// hay texto nuevo sin hablar.
			const now = Date.now();
			if (this.autoSpeechFragmentStartAt === 0) {
				this.autoSpeechFragmentStartAt = now;
			}
			const elapsed = now - this.autoSpeechFragmentStartAt;
			if (newText.length < AUTO_CHAR_THRESHOLD && elapsed < AUTO_TIMEOUT_MS) {
				return; // aún no alcanza el umbral ni el timeout: seguir acumulando
			}

			const speakText = sanitizeTextForTTS(newText);
			if (!speakText) return;
			this.autoSpokenTextByMsg.set(msgId, prevSpoken + newText);
			this.autoSpeechFragmentStartAt = 0; // próximo fragmento empieza de cero
			console.info('[Piper TTS] Streaming speak (piperstream)', { msgId, len: speakText.length });
			enqueue(speakText, newOwner());
			return;
		}

		// Fin del stream: hablar SOLO el resto no dicho (si existe).
		// NO re-sintetizar el texto completo: el stream ya hablo la mayoria.
		// prevSpoken es un prefijo EXACTO del texto crudo hablado en streaming,
		// así que el resto se calcula en crudo (sin desalineación por sanitizado).
		const remainingRaw = text.startsWith(prevSpoken)
			? text.slice(prevSpoken.length).trim()
			: text.trim();
		console.info('[Piper TTS] Automatic response complete', { msgId, remainingLen: remainingRaw.length });

		// Marcar como hablado (NO borrar): llamadas posteriores con isStreaming=false
		// tendrán prevSpoken == text => remaining vacío y autoSpeakMessage retorna
		// false. Evita re-leer TODO el texto repetidamente (bug del "synthesizeAndPlay").
		this.autoSpokenTextByMsg.set(msgId, text);
		this.autoSpeechFragmentStartAt = 0;

		const remaining = remainingRaw ? sanitizeTextForTTS(remainingRaw) : '';
		if (remaining) {
			console.info('[Piper TTS] Automatic response remainder', { msgId, len: remaining.length });
			enqueue(remaining, newOwner());
		}
	}

	/** Stop any current TTS playback */
	stopPlayback(): void {
		this.speechGeneration += 1;
		this.speechAbortController?.abort();
		this.speechAbortController = null;
		this.speechQueue = Promise.resolve();
		stopCurrentPlayback();
		this.isTtsPlaying = false;
		this.autoSpeakInProgress = false;
		this.ttsError = null;
	}

	/**
	 * Try to auto-speak a message. Called by ChatMessageAssistant when a
	 * message finishes loading. Will only speak each message once.
	 * Returns true if auto-speak was triggered.
	 */
	autoSpeakMessage(msgId: string, text: string): boolean {
		if (!this.autoSpeak || !text.trim()) return false;
		if (this.lastAutoSpokenMsgId === msgId) return false;
		// Si el stream incremental ya hablo este mensaje, no re-leer el texto completo.
		if (this.autoSpokenTextByMsg.has(msgId)) return false;
		if (this.autoSpeakInProgress || this.isTtsPlaying) return false;

		this.lastAutoSpokenMsgId = msgId;
		this.autoSpeakInProgress = true;

		// Fire and forget - don't block the UI. El auto-speak va SIEMPRE por
		// streaming: mandar la respuesta completa en una sola petición dejaría la
		// primera palabra esperando a que Piper sintetice todo el texto.
		const cleanText = sanitizeTextForTTS(text);
		if (!cleanText) {
			this.autoSpeakInProgress = false;
			return false;
		}
		this.speakStreaming(
			cleanText,
			this.speechGeneration,
			`auto-msg-${++this.autoSpeechFragmentSeq}`
		).finally(() => {
			this.autoSpeakInProgress = false;
		});

		return true;
	}

	/** Reset auto-speak state (e.g. when conversation changes) */
	resetAutoSpeak(): void {
		this.lastAutoSpokenMsgId = null;
		this.autoSpeakInProgress = false;
		this.autoSpokenTextByMsg.clear();
		this.autoSpeechFragmentStartAt = 0;
		this.autoSpeechGeneration += 1; // invalida utterances viejas en cola
		this.autoSpeechQueue = Promise.resolve();
	}

	// ---------------------------------------------------------------------------
	// Call mode (WebSocket)
	// ---------------------------------------------------------------------------

	async startCall(): Promise<void> {
		if (this.state !== 'idle') return;

		this.state = 'connecting';
			this.callPhase = 'LISTENING';
		this.error = null;
		this.callStartedAt = Date.now();
		this.callEndedAt = 0;
			this.callSessionTurns = [];

		try {
			console.info('[Piper TTS] Starting call session via', PIPER_WS_URL);
			// Crear/resumir el AudioContext DENTRO del gesto de usuario (clic en
			// "iniciar llamada"). Sin esto, el contexto se crea al llegar el primer
			// tts_chunk (evento asíncrono fuera del gesto) y Chrome lo deja en
			// 'suspended' => el audio del modo llamada no se oye.
			await primePlayback();
			await startMicrophoneCapture(
				(pcm) => this.sendAudioChunk(pcm),
				(analyser) => {
					this.audioAnalyser = analyser;
				},
				this.selectedInputDeviceId || undefined
			);
			await this.loadInputDevices();

			this.ws = new WebSocket(PIPER_WS_URL);

			this.ws.onopen = () => {
				console.info('[Piper TTS] WebSocket connected');
				this.state = 'active';
				this.sendMessage({
					type: 'config',
					threshold: 0.008,
					hangover: 300,
					min_speech_duration_frames: 4,
					voice: this.voice,
					speed: this.ttsSpeed,
					lang: this.ttsLang,
					stt_language: this.sttLanguage
				});
			};

			this.ws.onmessage = (event) => {
				const raw = String(event.data);
				try {
					const message = JSON.parse(raw) as { type?: string };
					if (message.type !== 'vad_score') {
						console.info('[Piper TTS] WebSocket message:', raw);
					}
				} catch {
					console.warn('[Piper TTS] Invalid WebSocket message');
				}
				this.handleServerMessage(raw);
			};

			this.ws.onerror = () => {
				console.error('[Piper TTS] WebSocket error connecting to', PIPER_WS_URL);
				this.error = 'Error de conexion con Piper TTS';
			};

			this.ws.onclose = () => {
				if (this.state !== 'idle') {
					// If we were still connecting, show a connection error
					if (this.state === 'connecting') {
						this.error = this.error ?? 'No se pudo conectar al servicio Piper FastAPI en puerto 8880';
					}
					this.stopCall();
				}
			};
		} catch (err) {
			console.error('[Piper Call] Failed to start call:', err);
			this.error = err instanceof Error ? err.message : 'No se pudo iniciar la llamada';
			this.state = 'idle';
			stopMicrophoneCapture();
		}
	}

	stopCall(): void {
		this.commitPartialCallTurn();
		const sessionTurns = this.callSessionTurns.map((turn) => ({ ...turn }));
		this.callEndedAt = Date.now();
		this.state = 'ending';
		stopMicrophoneCapture();
		this.stopPlayback();
		this.transcriptEvents = [];

		if (this.ws && this.ws.readyState === WebSocket.OPEN) {
			this.sendMessage({ type: 'stop' });
			this.ws.close();
		}
		this.ws = null;

		if (this.animFrame) {
			cancelAnimationFrame(this.animFrame);
			this.animFrame = null;
		}
		this.audioAnalyser = null;
		this.vadScore = 0;
		this.micInputLevel = 0;
		this.isSpeaking = false;
		this.state = 'idle';
		this.callPhase = sessionTurns.length ? 'FINISHED' : 'IDLE';
		if (sessionTurns.length) this.sessionFinishedHandler?.(sessionTurns);
		this.callSessionTurns = [];
		this.callAssistantText = '';
		this.callReasoningText = '';
		this.callSpeechConsumed = 0;
	}

	shouldSuppressAutoSpeak(messageId: string, messageTimestamp: unknown): boolean {
		const timestamp = Number(messageTimestamp);
		if (this.callResponsePending) this.callAutoSpeakMessageIds.add(messageId);
		return this.callAutoSpeakMessageIds.has(messageId) ||
			(Number.isFinite(timestamp) && this.callStartedAt > 0 &&
			timestamp >= this.callStartedAt &&
			(this.callEndedAt === 0 || timestamp <= this.callEndedAt));
	}

	sendTextForSynthesis(text: string): void {
		if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
		this.sendMessage({
			type: 'synthesize',
			text,
			voice: this.voice,
			speed: this.ttsSpeed,
			lang: this.ttsLang
		});
	}

	getEqualizerData(): Uint8Array | null {
		if (!this.audioAnalyser) return null;
		const data = new Uint8Array(this.audioAnalyser.frequencyBinCount);
		this.audioAnalyser.getByteFrequencyData(data);
		return data;
	}

	// ---------------------------------------------------------------------------
	// Internal
	// ---------------------------------------------------------------------------

	private sendAudioChunk(pcmInt16: Int16Array): void {
		if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
		// Pico de este frame (0..1) para el medidor de nivel de micrófono.
		let peak = 0;
		for (let i = 0; i < pcmInt16.length; i++) {
			const abs = pcmInt16[i] < 0 ? -pcmInt16[i] : pcmInt16[i];
			if (abs > peak) peak = abs;
		}
		// Decaimiento suave hacia el nivel actual (evita parpadeo frame a frame).
		const normalized = peak / 32768;
		this.micInputLevel = Math.max(normalized, this.micInputLevel * 0.9);
		this.ws.send(pcmInt16.buffer as ArrayBuffer);
	}

	private sendMessage(data: Record<string, unknown>): void {
		if (this.ws && this.ws.readyState === WebSocket.OPEN) {
			this.ws.send(JSON.stringify(data));
		}
	}

	private handleServerMessage(raw: string): void {
		try {
			const msg = JSON.parse(raw);

			switch (msg.type) {
				case 'vad_score':
					this.vadScore = msg.score ?? 0;
					break;
				case 'speech_start':
					this.isSpeaking = true;
					break;
				case 'speech_end':
					this.isSpeaking = false;
					// No reproducir audio aquí: speech_end solo marca fin de habla del micrófono
					break;
				case 'transcript':
					// Transcript events from call pipeline (STT user text + LLM assistant reply)
					this.transcriptEvents.push({
						role: msg.role ?? 'unknown',
						text: msg.text ?? '',
						timestamp: Date.now()
					});
					// Keep only last 20 events
					if (this.transcriptEvents.length > 20) {
						this.transcriptEvents.splice(0, this.transcriptEvents.length - 20);
					}
					if (msg.role === 'user' && msg.text?.trim()) {
						this.callSessionTurns.push({ user: msg.text.trim(), assistant: '', reasoning: '' });
						this.callPhase = 'WAITING_LLM';
					}
					break;
				case 'voice_stop':
					this.commitPartialCallTurn();
					this.callPhase = 'INTERRUPTED';
					this.stopPlayback();
					break;
				case 'llm_start':
					this.callAssistantText = '';
					this.callReasoningText = '';
					this.callPhase = 'WAITING_LLM';
					break;
				case 'reasoning_delta':
					this.callReasoningText += msg.text ?? '';
					this.callPhase = 'WAITING_LLM';
					break;
				case 'assistant_delta':
					this.callAssistantText += msg.text ?? '';
					this.callPhase = 'SPEAKING';
					this.queueCallParagraphs(false);
					break;
				case 'assistant_final':
					this.callAssistantText = msg.text ?? this.callAssistantText;
					this.callReasoningText = msg.reasoning ?? this.callReasoningText;
					if (this.callSessionTurns.length > 0) {
						const turn = this.callSessionTurns[this.callSessionTurns.length - 1];
						turn.assistant = this.callAssistantText;
						turn.reasoning = this.callReasoningText;
					}
					this.queueCallParagraphs(true);
					break;
				case 'tts_audio':
					if (msg.audio) this.playTtsAudio(msg.audio);
					break;
				case 'tts_chunk':
					// Pipeline de llamada (STT→LLM→TTS en backend): reproducir chunks
					// PCM en vivo en orden FIFO con UNA sola voz (valores piperstream.py).
					if (msg.audio !== undefined) {
						if (this.callFifoOwner === null) {
							this.callFifoOwner = `call-${Date.now()}`;
							startFifoUtterance(this.callFifoOwner);
						}
						pushFifoChunk(
							{ audio: msg.audio, sample_rate: msg.sample_rate ?? 22050, seq: msg.seq ?? 0 },
							this.callFifoOwner
						);
					}
					break;
				case 'tts_playing':
					this.isTtsPlaying = true;
					this.callPhase = 'SPEAKING';
					break;
				case 'tts_stopped':
					this.isTtsPlaying = false;
					// Fin del TTS de la llamada: drenar la cola FIFO de UNA voz.
					if (this.callFifoOwner !== null) {
						const owner = this.callFifoOwner;
						this.callFifoOwner = null;
						finishFifoUtterance(owner);
					}
					break;
				case 'processing_start':
					this.isCallProcessing = true;
					this.callPhase = 'TRANSCRIBING';
					break;
				case 'vad_calibrating':
					this.callPhase = 'LISTENING';
					break;
				case 'interrupted':
					this.commitPartialCallTurn();
					this.callPhase = 'INTERRUPTED';
					this.isCallProcessing = false;
					this.stopPlayback();
					break;
				case 'processing_end':
					this.isCallProcessing = false;
					this.callPhase = this.state === 'active' ? 'LISTENING' : this.callPhase;
					break;
				case 'error':
					this.error = msg.message ?? 'Error del servidor';
					break;
			}
		} catch {
			// Ignore parse errors
		}
	}

	private async playTtsAudio(base64Audio: string): Promise<void> {
		this.isTtsPlaying = true;
		this.sendMessage({ type: 'tts_playing' });
		try {
			await playBase64Wav(base64Audio, this.ttsSpeed);
		} catch (err) {
			console.error('TTS playback error:', err);
		} finally {
			this.isTtsPlaying = false;
			this.sendMessage({ type: 'tts_stopped' });
		}
	}

}

export const callStore = new CallStore();
