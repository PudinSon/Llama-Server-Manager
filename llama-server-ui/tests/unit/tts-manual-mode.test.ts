import { beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({
	played: 0,
	requests: [] as Array<{ url: string; body: Record<string, unknown> }>
}));

// Aislamos el store del Web Audio real: solo nos interesa QUE se pida a Piper
// y CUANTAS veces se reproduce.
vi.mock('$lib/utils/audio', () => ({
	fifoQueueLength: vi.fn(() => 0),
	finishFifoUtterance: vi.fn(),
	isFifoPlaying: vi.fn(() => false),
	playBase64Wav: vi.fn(async () => {
		state.played += 1;
	}),
	primePlayback: vi.fn(async () => {}),
	pushFifoChunk: vi.fn(),
	pushFifoChunks: vi.fn(),
	resetFifoPlayback: vi.fn(),
	startFifoUtterance: vi.fn(() => true),
	stopCurrentPlayback: vi.fn()
}));

import { callStore } from '$lib/stores/call.svelte';

describe('TTS modo manual (bocina de respuesta / boton de prueba)', () => {
	beforeEach(() => {
		vi.spyOn(console, 'info').mockImplementation(() => {});
		state.played = 0;
		state.requests = [];
		vi.stubGlobal(
			'fetch',
			vi.fn(async (url: string, init?: RequestInit) => {
				state.requests.push({
					body: JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>,
					url: String(url)
				});

				return {
					json: async () => ({ audio: 'QUJD' }),
					ok: true,
					status: 200
				} as unknown as Response;
			})
		);
	});

	it('pide el texto COMPLETO a /synthesize (sin streaming) y omite el razonamiento', async () => {
		await callStore.synthesizeAndPlay(
			'Hola mundo. \u003cthinking\u003eno debe oirse\u003c/thinking\u003e'
		);

		expect(state.requests).toHaveLength(1);
		expect(state.requests[0].url).toContain('/synthesize');
		expect(state.requests[0].url).not.toContain('/stream');
		expect(String(state.requests[0].body.text)).toContain('Hola mundo.');
		expect(String(state.requests[0].body.text)).not.toContain('no debe oirse');
		expect(state.played).toBe(1);
	});

	it('reproduce en intentos consecutivos (regresion: solo sonaba una vez hasta F5)', async () => {
		await callStore.synthesizeAndPlay('Primera respuesta.');
		await callStore.synthesizeAndPlay('Segunda respuesta.');

		expect(state.requests).toHaveLength(2);
		expect(state.played).toBe(2);
	});
});
