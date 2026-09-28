import { afterEach, describe, expect, it, vi } from 'vitest';
import { agentStore } from '$lib/stores/agentStore.svelte';

type Listener = (event: MessageEvent) => void;

class FakeEventSource {
	static instances: FakeEventSource[] = [];
	listeners = new Map<string, Listener[]>();
	close = vi.fn();
	onerror: (() => void) | null = null;
	onopen: (() => void) | null = null;
	onmessage: Listener | null = null;

	constructor(public url: string) {
		FakeEventSource.instances.push(this);
	}

	addEventListener(type: string, listener: Listener) {
		this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
	}

	emit(type: string, payload: unknown) {
		const event = { data: JSON.stringify(payload) } as MessageEvent;
		this.emitRaw(type, event);
	}

	/** Evento nativo del navegador: un `Event` sin `data` (caída de conexión). */
	emitRaw(type: string, event: Event | MessageEvent) {
		for (const listener of this.listeners.get(type) ?? []) listener(event as MessageEvent);
		if (type === 'message') this.onmessage?.(event as MessageEvent);
	}
}

afterEach(() => {
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
	FakeEventSource.instances = [];
});

describe('agentStore execution transport', () => {
	it('executes, consumes SSE events, cancels, and restores an active job', async () => {
		const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input);
			if (url.endsWith('/agent_execute') && init?.method === 'POST') {
				return new Response(JSON.stringify({ job_id: 'job-1', status: 'running' }), {
					status: 202
				});
			}
			if (url.endsWith('/agent_execute/job-1') && init?.method === 'DELETE') {
				return new Response(JSON.stringify({ job_id: 'job-1', status: 'cancelled' }), {
					status: 200
				});
			}
			if (url.endsWith('/agent_jobs')) {
				return new Response(JSON.stringify({ jobs: [{ id: 'job-2', status: 'running' }] }), {
					status: 200
				});
			}
			throw new Error(`Unexpected request: ${url}`);
		});
		vi.stubGlobal('fetch', fetchMock);
		vi.stubGlobal('EventSource', FakeEventSource);

		const jobId = await agentStore.execute('Inspect the project');
		expect(jobId).toBe('job-1');
		expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
			agents: [],
			context: undefined,
			research: false,
			task: 'Inspect the project'
		});

		const source = FakeEventSource.instances[0];
		source.onopen?.();
		source.emit('reasoning', { type: 'reasoning', payload: { text: 'Planning. ' } });
		source.emit('tool_call', {
			payload: { arguments: '{}', name: 'read_file' },
			type: 'tool_call'
		});
		source.emit('tool_result', {
			payload: { name: 'read_file', result: 'ok' },
			type: 'tool_result'
		});
		source.emit('text', { payload: 'Done', type: 'text' });
		source.emit('done', { payload: { content: 'Done' }, type: 'done' });

		expect(agentStore.reasoning).toEqual(['Planning. ']);
		expect(agentStore.response).toBe('Done');
		expect(agentStore.events.map((event) => event.type)).toEqual([
			'reasoning',
			'tool_call',
			'tool_result',
			'text',
			'done'
		]);
		expect(agentStore.isExecuting).toBe(false);

		await agentStore.cancel();
		// El host lo decide `serviceBase('agent')` (127.0.0.1 fuera del navegador,
		// el host actual en modo directo), así que se compara contra el endpoint
		// real del store en vez de fijar uno.
		expect(fetchMock).toHaveBeenCalledWith(`${agentStore.config.endpoint}/agent_execute/job-1`, {
			method: 'DELETE'
		});

		expect(await agentStore.restoreActiveJob()).toBe(true);
		expect(agentStore.currentJobId).toBe('job-2');
		expect(agentStore.isExecuting).toBe(true);
	});

	it('ignores the browser error event when the stream closes', async () => {
		// El navegador entrega el error NATIVO (Event sin `data`) por el mismo
		// listener que el evento SSE "error": parsearlo marcaba un falso
		// "Evento SSE inválido" al cerrarse la conexión.
		const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url.endsWith('/agent_execute')) {
				return new Response(JSON.stringify({ job_id: 'job-9', status: 'running' }), {
					status: 202
				});
			}
			throw new Error(`Unexpected request: ${url}`);
		});
		vi.stubGlobal('fetch', fetchMock);
		vi.stubGlobal('EventSource', FakeEventSource);

		await agentStore.execute('tarea');
		const source = FakeEventSource.instances[0];

		source.emitRaw('error', new Event('error'));

		expect(agentStore.lastError).not.toBe('Evento SSE invalido');
	});
});	describe('agentStore timeline scoping', () => {
	it('liga la timeline a la conversación que lanzó la ejecución', () => {
		agentStore.beginAgencyTimeline('conv-1');

		expect(agentStore.agencyConversationId).toBe('conv-1');
		expect(agentStore.isExecuting).toBe(true);
		expect(agentStore.agencyRuns).toEqual([]);

		// Otra ejecución en otra conversación sustituye la timeline activa: la
		// anterior deja de mostrarse (no se podía ver el mismo bloque en todos
		// los chats).
		agentStore.beginAgencyTimeline('conv-2');
		expect(agentStore.agencyConversationId).toBe('conv-2');
	});

	it('al cerrar la ejecución no queda ningún agente en curso', () => {
		agentStore.beginAgencyTimeline('conv-1');
		agentStore.applyChatAgencyEvent({
			job_id: 'job-1',
			payload: { agent: 'Analista', subtask: 'Investigar', task_id: 's1' },
			type: 'agent_start'
		});
		expect(agentStore.agencyRuns[0].status).toBe('running');

		agentStore.finishAgencyTimeline();

		expect(agentStore.isExecuting).toBe(false);
		expect(agentStore.agencyCurrentAgent).toBeNull();
		expect(agentStore.agencyRuns.every((run) => run.status === 'done')).toBe(true);
	});
});
	describe('agentStore AGENCY timeline', () => {
	it('builds the plan, streams each agent into its own card and keeps the synthesis separate', () => {
		agentStore.resetAgencyTimeline();
		// `response` persiste entre ejecuciones (lo limpia el chat al abrir turno).
		const baseline = agentStore.response;

		agentStore.applyChatAgencyEvent({
			job_id: 'job-1',
			payload: {
				steps: [
					{ id: 's1', agent: 'Analista', subtask: 'Investigar' },
					{ id: 's2', agent: 'Redactor', subtask: 'Redactar', depends_on: ['s1'] }
				]
			},
			type: 'plan'
		});
		expect(agentStore.agencyPlan.map((step) => step.id)).toEqual(['s1', 's2']);
		expect(agentStore.agencyPlan[1].agent_name).toBe('Redactor');

		agentStore.applyChatAgencyEvent({
			job_id: 'job-1',
			payload: { agent: 'Analista', subtask: 'Investigar', task_id: 's1' },
			type: 'agent_start'
		});
		agentStore.applyChatAgencyEvent({
			job_id: 'job-1',
			meta: { agent: 'Analista', role: 'specialist', task_id: 's1' },
			payload: { text: 'hallazgo' },
			type: 'text'
		});

		const run = agentStore.agencyRuns.find((item) => item.id === 's1');
		expect(run?.status).toBe('running');
		expect(run?.text).toBe('hallazgo');
		// El texto del subagente no contamina la respuesta final.
		expect(agentStore.response).toBe(baseline);

		agentStore.applyChatAgencyEvent({
			job_id: 'job-1',
			payload: { agent: 'Analista', success: true, task_id: 's1' },
			type: 'agent_end'
		});
		expect(agentStore.agencyRuns.find((item) => item.id === 's1')?.status).toBe('done');

		agentStore.applyChatAgencyEvent({
			job_id: 'job-1',
			meta: { role: 'supervisor' },
			payload: { text: 'síntesis final' },
			type: 'text'
		});
		expect(agentStore.agencySupervisor).toBe('síntesis final');
		expect(agentStore.response).toBe(`${baseline}síntesis final`);
	});

	it('does not finish the job when a single step fails', () => {
		agentStore.resetAgencyTimeline();
		agentStore.applyChatAgencyEvent({
			job_id: 'job-1',
			payload: { agent: 'Analista', subtask: 'Investigar', task_id: 's1' },
			type: 'agent_start'
		});

		agentStore.applyChatAgencyEvent({
			job_id: 'job-1',
			payload: { agent: 'Analista', error: 'boom' },
			type: 'error'
		});
		expect(agentStore.isExecuting).toBe(true);
		expect(agentStore.agencyRuns.find((item) => item.id === 's1')?.status).toBe('error');

		agentStore.applyChatAgencyEvent({ job_id: 'job-1', payload: {}, type: 'done' });
		expect(agentStore.isExecuting).toBe(false);
	});
});
