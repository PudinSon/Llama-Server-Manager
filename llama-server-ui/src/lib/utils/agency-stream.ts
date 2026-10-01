/**
 * Transporte ÚNICO del stream SSE de un job agencial (`/agent_events/{jobId}`).
 *
 * Por qué existe
 * --------------
 * Había DOS implementaciones del mismo stream, cada una con su propia lista de
 * tipos de evento, su propia reconexión y su propio criterio de "el job
 * terminó":
 *
 * 1. `agentStore.connectToJob` — la del panel de agentes. Escuchaba un
 *    subconjunto de tipos (sin `usage`, sin `incomplete`, sin `sse_start`) y
 *    reconectaba abriendo la conexión desde cero, reenviando el job entero.
 * 2. `chatStore.consumeAgencyJob` — la del chat nativo. Reanudaba por índice
 *    (`?from=N` + `sse_start` + `frame.index`), toleraba frames corruptos y no
 *    daba el job por perdido en un corte.
 *
 * El resultado era que el mismo backend se veía distinto según quién
 * escuchara: el panel perdía eventos que el chat sí mostraba. Aquí vive la
 * única versión (la buena: la del chat), y los dos consumidores se limitan a
 * traducir frames a su modelo de UI.
 *
 * Qué NO hace
 * -----------
 * No interpreta los eventos: no toca stores ni pinta nada. Entrega cada frame
 * aceptado a `onFrame` con su índice de cola, y decide solo lo que es
 * protocolo: índice, reintentos, frames corruptos y fin del job.
 */

export const AGENCY_RECONNECT_BASE_DELAY_MS = 750;
export const AGENCY_RECONNECT_MAX_DELAY_MS = 30_000;
export const AGENCY_RECONNECT_MAX_ATTEMPTS = 24;

/** Marco SSE crudo del job agencial (`event: <tipo>` + `data: {...}`). */
export interface AgencySseFrame {
	/** Índice absoluto de la cola del job (lo incrusta el backend en cada frame). */
	index?: number;
	meta?: Record<string, unknown>;
	payload?: unknown;
	type?: string;
}

/**
 * Tipos de evento que el backend emite por el stream del job.
 *
 * Se registran todos en el MISMO transporte: antes cada consumidor tenía su
 * lista y una lista incompleta significaba perder eventos en silencio (el
 * `EventSource` solo entrega los tipos con listener explícito).
 */
export const AGENCY_STREAM_EVENT_TYPES = [
	'text',
	'reasoning',
	'usage',
	'progress',
	'tool_call',
	'native_tool_call',
	'tool_result',
	'tool_denied',
	'step_start',
	'step_end',
	'agency_selected',
	'plan',
	'agent_start',
	'agent_end',
	'handoff',
	'supervisor',
	'incomplete',
	'sse_start',
	'error',
	'done'
] as const;

export interface AgencyStreamOptions {
	jobId: string;
	endpoint: string;
	/** Se llama por cada frame aceptado, en orden. Puede ser async. */
	onFrame: (frame: AgencySseFrame, queueIndex: number) => void | Promise<void>;
	signal?: AbortSignal;
	/** Índice del último evento aplicado (base del `?from=` inicial). */
	from?: number;
	/** Índice del último evento aplicado, tras cada frame. */
	onAppliedIndex?: (appliedIndex: number) => void;
	onOpen?: () => void;
	onReconnect?: (attempt: number, delayMs: number) => void;
	/** Cancela el job en el BACKEND (no solo la conexión). */
	onCancel?: () => void;
	maxMalformedFrames?: number;
	maxAttempts?: number;
	baseDelayMs?: number;
	maxDelayMs?: number;
}

/**
 * ¿Este frame cierra el job?
 *
 * Los `done` que viajan por el bus (especialistas, síntesis y pasos del plan)
 * llegan con `meta` y son hitos intermedios: el fin real es el `done` SIN meta
 * que el worker del job appendea en su `finally`.
 */
export function isAgencyTerminalFrame(frame: AgencySseFrame): boolean {
	return frame.type === 'done' && Object.keys(frame.meta ?? {}).length === 0;
}

/**
 * Consume el stream del job hasta que termina, se agota, se aborta o el stream
 * se pierde de forma irrecuperable.
 *
 * Reanudación: cada conexión pide `?from=appliedIndex`; si el backend no lo
 * soporta manda `sse_start` con `from=0` y el cliente descarta por índice lo ya
 * aplicado. Nunca se pierde ni se duplica un evento en un corte.
 */
export function streamAgencyJob(options: AgencyStreamOptions): Promise<void> {
	const {
		baseDelayMs = AGENCY_RECONNECT_BASE_DELAY_MS,
		endpoint,
		jobId,
		maxAttempts = AGENCY_RECONNECT_MAX_ATTEMPTS,
		maxDelayMs = AGENCY_RECONNECT_MAX_DELAY_MS,
		maxMalformedFrames = 3,
		onAppliedIndex,
		onCancel,
		onFrame,
		onOpen,
		onReconnect,
		signal,
		from = 0
	} = options;
	const base = endpoint.replace(/\/$/, '');

	return new Promise<void>((resolve, reject) => {
		let appliedIndex = from;
		let source: EventSource | null = null;
		let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
		let reconnectAttempt = 0;
		let settled = false;
		let serverDone = false;
		let malformedFrames = 0;
		let receivedOnConnection = 0;
		let baseIndex = appliedIndex;
		let frameQueue: Promise<void> = Promise.resolve();

		const teardown = () => {
			if (reconnectTimer) clearTimeout(reconnectTimer);

			reconnectTimer = null;
			source?.close();
			source = null;
		};

		function settle(error?: Error): void {
			if (settled) return;

			settled = true;
			teardown();
			signal?.removeEventListener('abort', onAbort);

			if (error) reject(error);
			else resolve();
		}

		function onAbort(): void {
			onCancel?.();
			settle(new DOMException('Aborted', 'AbortError'));
		}

		/**
		 * Reenganche con backoff. El job sigue vivo en el backend, así que el
		 * stream se reintenta en lugar de dar la ejecución por perdida: es el
		 * caso de los tools largos (minutos sin emitir eventos) y de los
		 * proxies intermedios que cortan conexiones SSE ociosas.
		 */
		const scheduleReconnect = () => {
			if (settled || serverDone || signal?.aborted) return;

			if (reconnectAttempt >= maxAttempts) {
				onCancel?.();
				settle(new Error('Conexion SSE del agente perdida'));

				return;
			}

			const delay = Math.min(baseDelayMs * 2 ** reconnectAttempt, maxDelayMs);

			reconnectAttempt += 1;
			onReconnect?.(reconnectAttempt, delay);
			reconnectTimer = setTimeout(() => {
				reconnectTimer = null;
				connect();
			}, delay);
		};

		const handle = async (message: MessageEvent) => {
			// Los comentarios `: keepalive` del backend y el evento `error`
			// NATIVO del navegador llegan sin `data`: no son eventos del job y
			// no consumen índice.
			if (typeof message?.data !== 'string' || !message.data) return;

			const index = baseIndex + receivedOnConnection;

			receivedOnConnection += 1;

			let frame: AgencySseFrame;

			try {
				frame = JSON.parse(message.data) as AgencySseFrame;
			} catch {
				// Un frame corrupto no debe matar una ejecución de varios
				// minutos: solo se aborta si el stream deja de ser JSON.
				malformedFrames += 1;
				console.warn('[agency] Evento SSE no parseable', message.data.slice(0, 200));

				if (malformedFrames >= maxMalformedFrames) settle(new Error('Evento de agente invalido'));

				return;
			}
			malformedFrames = 0;

			if (frame.type === 'sse_start') {
				// El backend reporta el índice real desde el que empieza a
				// mandar la cola: fija la base y no consume índice.
				const reported = Number((frame.payload as { from?: unknown } | undefined)?.from ?? 0);

				baseIndex = Number.isFinite(reported) && reported >= 0 ? reported : 0;
				receivedOnConnection = 0;

				return;
			}

			// Índice real de la cola (el backend lo incrusta en cada frame):
			// un recuento local se desincroniza si el stream trae tipos que
			// este cliente no escucha. Los frames antiguos sin índice caen al
			// recuento local.
			const queueIndex =
				typeof frame.index === 'number' && Number.isFinite(frame.index) && frame.index >= 0
					? frame.index
					: index;

			// Evento ya aplicado en una conexión anterior (replay).
			if (queueIndex < appliedIndex) return;

			await onFrame(frame, queueIndex);
			appliedIndex = Math.max(appliedIndex, queueIndex + 1);
			onAppliedIndex?.(appliedIndex);

			if (isAgencyTerminalFrame(frame)) {
				serverDone = true;
				settle();
			}
		};

		const connect = () => {
			if (settled || serverDone || signal?.aborted) return;

			baseIndex = appliedIndex;
			receivedOnConnection = 0;

			const next = new EventSource(`${base}/agent_events/${jobId}?from=${appliedIndex}`);

			source = next;
			next.onopen = () => {
				reconnectAttempt = 0;
				onOpen?.();
			};

			const forward = (message: MessageEvent) => {
				frameQueue = frameQueue
					.then(() => handle(message))
					.catch((error: unknown) => {
						settle(error instanceof Error ? error : new Error(String(error)));
					});
			};

			for (const type of AGENCY_STREAM_EVENT_TYPES) {
				next.addEventListener(type, forward);
			}

			next.onerror = () => {
				if (settled || serverDone) return;

				// El corte NO cancela el job: el backend sigue trabajando y el
				// reintento reanuda por índice.
				next.close();
				source = null;
				scheduleReconnect();
			};
		};

		if (signal?.aborted) {
			settle(new DOMException('Aborted', 'AbortError'));

			return;
		}

		signal?.addEventListener('abort', onAbort);
		connect();
	});
}
