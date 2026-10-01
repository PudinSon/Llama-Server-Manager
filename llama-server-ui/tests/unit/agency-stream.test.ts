import { afterEach, describe, expect, it, vi } from 'vitest';
import {
	AGENCY_STREAM_EVENT_TYPES,
	type AgencySseFrame,
	isAgencyTerminalFrame,
	streamAgencyJob
} from '$lib/utils/agency-stream';

type Listener = (event: MessageEvent) => void;

/**
 * `EventSource` de mentira: guarda los listeners por tipo y permite emitir
 * eventos con `data` (frames del job) o eventos nativos sin `data`.
 */
class FakeEventSource {
	static instances: FakeEventSource[] = [];
	listeners = new Map<string, Listener[]>();
	close = vi.fn();
	onerror: (() => void) | null = null;
	onopen: (() => void) | null = null;

	constructor(public url: string) {
		FakeEventSource.instances.push(this);
	}

	get from(): number {
		return Number(new URL(this.url).searchParams.get('from') ?? 0);
	}

	get last(): FakeEventSource {
		return FakeEventSource.instances[FakeEventSource.instances.length - 1];
	}

	addEventListener(type: string, listener: Listener) {
		this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
	}

	emit(type: string, frame: AgencySseFrame | Record<string, unknown>) {
		for (const listener of this.listeners.get(type) ?? []) {
			listener({ data: JSON.stringify(frame) } as MessageEvent);
		}
	}

	/** Frame con `data` que NO es JSON: el stream dejó de ser un flujo de frames. */
	emitCorrupt(type: string, raw: string) {
		for (const listener of this.listeners.get(type) ?? []) {
			listener({ data: raw } as MessageEvent);
		}
	}

	/** Evento NATIVO del navegador (Event sin `data`): keepalive o caída. */
	emitNative(type: string) {
		for (const listener of this.listeners.get(type) ?? []) {
			listener(new Event(type) as MessageEvent);
		}
	}

	/** Corte de conexión: es el `onerror` de `EventSource`, no un evento SSE. */
	fail() {
		this.onerror?.();
	}
}

/** Espera a que se resuelvan los `await` internos del transporte. */
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

afterEach(() => {
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
	vi.useRealTimers();
	FakeEventSource.instances = [];
});

describe('isAgencyTerminalFrame', () => {
	it('solo el done SIN meta cierra el job', () => {
		expect(isAgencyTerminalFrame({ type: 'done' })).toBe(true);
		expect(isAgencyTerminalFrame({ type: 'done', meta: {} })).toBe(true);
		// Los `done` del bus (especialistas, síntesis, pasos) son hitos.
		expect(isAgencyTerminalFrame({ meta: { agent: 'Analista' }, type: 'done' })).toBe(false);
		expect(isAgencyTerminalFrame({ type: 'text' })).toBe(false);
	});
});

describe('streamAgencyJob', () => {
	it('entrega los frames en orden con su índice de cola y termina en el done sin meta', async () => {
		vi.stubGlobal('EventSource', FakeEventSource);

		const frames: Array<[string, number]> = [];
		const done = streamAgencyJob({
			endpoint: 'http://127.0.0.1:15000/',
			jobId: 'job-1',
			onFrame: (frame, queueIndex) => {
				frames.push([String(frame.type), queueIndex]);
			}
		});

		const source = FakeEventSource.instances[0];
		source.onopen?.();
		source.emit('plan', { index: 0, payload: { steps: [] }, type: 'plan' });
		source.emit('text', { index: 1, payload: { text: 'hola' }, type: 'text' });
		// Hito intermedio del bus: NO debe cerrar el stream.
		source.emit('done', { index: 2, meta: { agent: 'Analista' }, type: 'done' });

		await flush();

		expect(frames).toEqual([
			['plan', 0],
			['text', 1],
			['done', 2]
		]);

		source.emit('done', { index: 3, payload: {}, type: 'done' });

		await expect(done).resolves.toBeUndefined();
		expect(source.close).toHaveBeenCalled();
		expect(source.url).toBe('http://127.0.0.1:15000/agent_events/job-1?from=0');
	});

	it('espera a aplicar cada frame async antes de aplicar el siguiente', async () => {
		vi.stubGlobal('EventSource', FakeEventSource);
		let releaseFirst!: () => void;
		const firstFrameGate = new Promise<void>((resolve) => {
			releaseFirst = resolve;
		});
		const applied: number[] = [];
		let appliedIndex = -1;
		const done = streamAgencyJob({
			endpoint: 'http://127.0.0.1:15000',
			jobId: 'job-serial',
			onAppliedIndex: (index) => {
				appliedIndex = index;
			},
			onFrame: async (_frame, index) => {
				if (index === 0) await firstFrameGate;
				applied.push(index);
			}
		});
		const source = FakeEventSource.instances[0];
		source.emit('tool_call', { index: 0, type: 'tool_call', payload: {} });
		source.emit('tool_result', { index: 1, type: 'tool_result', payload: {} });
		await flush();

		expect(applied).toEqual([]);
		expect(appliedIndex).toBe(-1);
		releaseFirst();
		await flush();
		expect(applied).toEqual([0, 1]);
		expect(appliedIndex).toBe(2);
		source.emit('done', { index: 2, type: 'done' });
		await expect(done).resolves.toBeUndefined();
	});

	it('ignora el evento nativo sin data (keepalive o caída) sin consumir índice', async () => {
		vi.stubGlobal('EventSource', FakeEventSource);

		const frames: number[] = [];
		void streamAgencyJob({
			endpoint: 'http://127.0.0.1:15000',
			jobId: 'job-2',
			onFrame: (_frame, queueIndex) => {
				frames.push(queueIndex);
			}
		});

		const source = FakeEventSource.instances[0];
		source.emitNative('error');
		source.emit('text', { index: 0, payload: { text: 'uno' }, type: 'text' });
		source.emitNative('text');

		await flush();

		expect(frames).toEqual([0]);
	});

	it('reanuda por índice en un corte y descarta lo ya aplicado del replay', async () => {
		vi.stubGlobal('EventSource', FakeEventSource);
		vi.useFakeTimers();

		const seen: string[] = [];
		const reconnects: Array<[number, number]> = [];
		let applied = -1;
		const done = streamAgencyJob({
			endpoint: 'http://127.0.0.1:15000',
			jobId: 'job-3',
			onAppliedIndex: (index) => {
				applied = index;
			},
			onFrame: (frame) => {
				seen.push(String(frame.type));
			},
			onReconnect: (attempt, delayMs) => {
				reconnects.push([attempt, delayMs]);
			}
		});

		const first = FakeEventSource.instances[0];
		first.emit('text', { index: 0, payload: { text: 'a' }, type: 'text' });
		first.emit('text', { index: 1, payload: { text: 'b' }, type: 'text' });

		await vi.advanceTimersByTimeAsync(0);

		expect(seen).toEqual(['text', 'text']);
		expect(applied).toBe(2);

		// Corte del proxy: el job sigue vivo, se reintenta con `?from=2`.
		first.fail();

		await vi.advanceTimersByTimeAsync(750);

		expect(reconnects).toEqual([[1, 750]]);
		expect(FakeEventSource.instances).toHaveLength(2);

		const second = FakeEventSource.instances[1];

		expect(second.url).toBe('http://127.0.0.1:15000/agent_events/job-3?from=2');

		// Un backend que reenvía el job entero desde cero no duplica nada.
		second.emit('text', { index: 0, payload: { text: 'a' }, type: 'text' });
		second.emit('text', { index: 1, payload: { text: 'b' }, type: 'text' });
		second.emit('text', { index: 2, payload: { text: 'c' }, type: 'text' });
		second.emit('done', { index: 3, type: 'done' });

		await vi.advanceTimersByTimeAsync(0);

		expect(seen).toEqual(['text', 'text', 'text', 'done']);
		await expect(done).resolves.toBeUndefined();
	});

	it('si el backend no soporta ?from, sse_start fija la base real', async () => {
		vi.stubGlobal('EventSource', FakeEventSource);
		vi.useFakeTimers();

		const indexes: number[] = [];
		const done = streamAgencyJob({
			endpoint: 'http://127.0.0.1:15000',
			from: 10,
			jobId: 'job-4',
			onFrame: (_frame, queueIndex) => {
				indexes.push(queueIndex);
			}
		});

		const source = FakeEventSource.instances[0];

		expect(source.url).toBe('http://127.0.0.1:15000/agent_events/job-4?from=10');

		// El backend ignora `from` y reporta que empieza en 0: los frames sin
		// `index` se numeran desde ahí y los ya aplicados se descartan.
		source.emit('sse_start', { payload: { from: 0 }, type: 'sse_start' });
		source.emit('text', { payload: { text: 'viejo' }, type: 'text' });
		source.emit('text', { payload: { text: 'viejo 2' }, type: 'text' });

		await vi.advanceTimersByTimeAsync(0);

		expect(indexes).toEqual([]);

		source.emit('text', { index: 10, payload: { text: 'nuevo' }, type: 'text' });
		source.emit('done', { index: 11, type: 'done' });

		await vi.advanceTimersByTimeAsync(0);

		expect(indexes).toEqual([10, 11]);
		await expect(done).resolves.toBeUndefined();
	});

	it('muere solo si el stream deja de ser JSON', async () => {
		vi.stubGlobal('EventSource', FakeEventSource);

		const frames: string[] = [];
		const done = streamAgencyJob({
			endpoint: 'http://127.0.0.1:15000',
			jobId: 'job-5',
			onFrame: (frame) => {
				frames.push(String(frame.type));
			}
		});

		const source = FakeEventSource.instances[0];

		source.emitCorrupt('text', '<html>proxy</html>');
		source.emit('text', { index: 0, payload: { text: 'ok' }, type: 'text' });
		// Un frame válido reinicia el contador de basura (el timeout de un
		// proxy puede colar dos respuestas de error sueltas).
		source.emitCorrupt('text', '<html>proxy</html>');
		source.emitCorrupt('text', '<html>proxy</html>');

		await flush();

		expect(frames).toEqual(['text']);

		source.emitCorrupt('text', '<html>proxy</html>');

		await expect(done).rejects.toThrow('Evento de agente invalido');
	});

	it('al agotar los reintentos cancela el job en el backend', async () => {
		vi.stubGlobal('EventSource', FakeEventSource);
		vi.useFakeTimers();

		const onCancel = vi.fn();
		const failures: Error[] = [];
		const done = streamAgencyJob({
			endpoint: 'http://127.0.0.1:15000',
			jobId: 'job-6',
			maxAttempts: 2,
			onCancel,
			onFrame: () => {}
		}).catch((error: Error) => failures.push(error));

		FakeEventSource.instances[0].fail();

		await vi.advanceTimersByTimeAsync(750);
		FakeEventSource.instances[1].fail();

		await vi.advanceTimersByTimeAsync(1500);
		FakeEventSource.instances[2].fail();

		await vi.advanceTimersByTimeAsync(3000);

		await done;

		expect(onCancel).toHaveBeenCalledTimes(1);
		expect(failures[0].message).toBe('Conexion SSE del agente perdida');
	});

	it('abortar corta el job en el backend y no reintenta', async () => {
		vi.stubGlobal('EventSource', FakeEventSource);

		const controller = new AbortController();
		const onCancel = vi.fn();
		const frames: string[] = [];
		const done = streamAgencyJob({
			endpoint: 'http://127.0.0.1:15000',
			jobId: 'job-7',
			onCancel,
			onFrame: (frame) => {
				frames.push(String(frame.type));
			},
			signal: controller.signal
		});

		const source = FakeEventSource.instances[0];

		source.emit('text', { index: 0, payload: { text: 'a' }, type: 'text' });

		await flush();
		controller.abort();

		await expect(done).rejects.toMatchObject({ name: 'AbortError' });

		expect(onCancel).toHaveBeenCalledTimes(1);
		expect(source.close).toHaveBeenCalled();
		expect(frames).toEqual(['text']);
	});

	it('escucha TODOS los tipos que emite el backend', () => {
		vi.stubGlobal('EventSource', FakeEventSource);

		void streamAgencyJob({
			endpoint: 'http://127.0.0.1:15000',
			jobId: 'job-8',
			onFrame: () => {}
		});

		const source = FakeEventSource.instances[0];

		for (const type of AGENCY_STREAM_EVENT_TYPES) {
			expect(source.listeners.get(type), `falta el listener de "${type}"`).toHaveLength(1);
		}

		expect(source.listeners.get('incomplete')).toHaveLength(1);
		expect(source.listeners.get('tool_denied')).toHaveLength(1);
	});
});
