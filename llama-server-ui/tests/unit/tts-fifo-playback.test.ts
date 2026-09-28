import {
	fifoQueueLength,
	finishFifoUtterance,
	isFifoPlaying,
	pushFifoChunk,
	resetFifoPlayback,
	startFifoUtterance,
	stopCurrentPlayback
} from '$lib/utils/audio';
import { beforeEach, describe, expect, it } from 'vitest';

// --- Stub mínimo de Web Audio (el entorno `unit` de vitest corre en node) ---

let played = 0;
let contextCreations = 0;
let contextCloses = 0;

class FakeBufferSource {
	buffer: unknown = null;
	onended: (() => void) | null = null;
	connect() {}
	start() {
		played += 1;
		queueMicrotask(() => this.onended?.());
	}
	stop() {
		queueMicrotask(() => this.onended?.());
	}
}

class FakeAudioContext {
	destination = {};
	sampleRate = 22050;
	state = 'running';
	async close() {
		contextCloses += 1;
		this.state = 'closed';
	}
	createBuffer(_channels: number, length: number, sampleRate: number) {
		const data = new Float32Array(length);

		return { getChannelData: () => data, length, sampleRate };
	}
	createBufferSource() {
		return new FakeBufferSource();
	}
	async decodeAudioData(): Promise<never> {
		throw new Error('decodeAudioData no se usa en el flush FIFO');
	}
	async resume() {}
}

function installStub(): void {
	played = 0;
	contextCreations = 0;
	contextCloses = 0;
	(globalThis as Record<string, unknown>).AudioContext = class extends FakeAudioContext {
		constructor() {
			super();
			contextCreations += 1;
		}
	};
}

function pcmBase64(samples = 480): string {
	const int16 = new Int16Array(samples);

	for (let i = 0; i < samples; i++) int16[i] = Math.round(Math.sin(i / 8) * 8000);
	const bytes = new Uint8Array(int16.buffer);

	let binary = '';

	for (const byte of bytes) binary += String.fromCharCode(byte);

	return btoa(binary);
}

async function waitForDrain(timeoutMs = 1500): Promise<void> {
	const started = Date.now();

	while ((isFifoPlaying() || fifoQueueLength() > 0) && Date.now() - started < timeoutMs) {
		await new Promise((resolve) => setTimeout(resolve, 5));
	}
}

/** Simula una utterance completa (como hace synthStream con el SSE). */
async function playUtterance(owner: string, chunkCount = 1): Promise<void> {
	startFifoUtterance(owner);
	for (let seq = 0; seq < chunkCount; seq++) {
		pushFifoChunk({ audio: pcmBase64(), sample_rate: 22050, seq }, owner);
	}
	finishFifoUtterance(owner);
	await waitForDrain();
}

beforeEach(() => {
	installStub();
	resetFifoPlayback();
});

describe('TTS FIFO playback', () => {
	it('reproduce la segunda utterance después de la primera (regresión)', async () => {
		await playUtterance('u1', 2);
		expect(fifoQueueLength()).toBe(0);
		expect(played).toBe(2);

		// Antes del fix los chunks de u2 se encolaban con seq 2,3 mientras el flush
		// esperaba seq 0 -> no sonaba nada hasta recargar la página.
		await playUtterance('u2');
		expect(fifoQueueLength()).toBe(0);
		expect(played).toBe(3);
		expect(isFifoPlaying()).toBe(false);
	});

	it('encadena varias utterances seguidas sin quedar mudo', async () => {
		for (let i = 0; i < 4; i++) {
			await playUtterance(`chain-${i}`);
		}
		expect(played).toBe(4);
		expect(fifoQueueLength()).toBe(0);
		expect(isFifoPlaying()).toBe(false);
	});

	it('tras un barge-in (stopCurrentPlayback) la siguiente utterance suena', async () => {
		await playUtterance('a');
		const afterFirst = played;

		expect(afterFirst).toBe(1);

		stopCurrentPlayback();
		await playUtterance('b');
		expect(played).toBe(afterFirst + 1);
	});

	it('no crea ni cierra AudioContexts al detener la reproducción', async () => {
		await playUtterance('c');
		expect(played).toBe(1);

		stopCurrentPlayback();
		expect(contextCloses).toBe(0);

		await playUtterance('d');
		expect(contextCreations).toBe(0);
		expect(contextCloses).toBe(0);
		expect(played).toBe(2);
	});
});
