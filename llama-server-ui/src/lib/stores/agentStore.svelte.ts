/**
 * agentStore - Reactive agent state management using Svelte 5 runes
 *
 * Manages agent daemon status, memory, skills, and plan via MCP tool calls.
 * Uses $state for reactivity and persists endpoint config to localStorage.
 *
 * Replaces the legacy writable-based agentStore.ts.
 */

import { browser } from '$app/environment';
import { apiToken, serviceBase } from '$lib/api';
import { get } from 'svelte/store';
import { toast } from 'svelte-sonner';
import { ragResearchMode } from '$lib/stores/rag.svelte';

export interface AgentStatus {
	mode: string;
	confidence: number;
	task: string;
	memory: any[];
	skills: any[];
	plan: any;
}

export interface AgentConfig {
	endpoint: string;
	agentMode: boolean;
	selectionMode: 'manual' | 'automatic' | 'hybrid';
	executionMode: 'parallel' | 'sequential';
	daemonMode: boolean;
	autoRefresh: boolean;
	refreshIntervalMs: number;
	maxRetries: number;
	showToasts: boolean;
}

export type AgentEventType =
	| 'text'
	| 'reasoning'
	| 'tool_call'
	| 'native_tool_call'
	| 'tool_result'
	| 'step_start'
	| 'step_end'
	| 'plan'
	| 'error'
	| 'done';

export interface AgentEvent {
	type: AgentEventType | string;
	data?: unknown;
	job_id?: string;
	status?: string;
	result?: unknown;
	[key: string]: unknown;
}

export interface AgencyAgent {
	aliases?: string[];
	category: string;
	description: string;
	emoji: string;
	color: string;
	name: string;
	display_name_es?: string;
	path: string;
	vibe: string;
}

/** Agente del catálogo con su markdown completo (frontmatter + instrucciones). */
export interface AgencyAgentDetail extends AgencyAgent {
	content?: string;
}

export interface AgencyPlanStep {
	id: string;
	subtask: string;
	agent_name: string;
	depends_on?: string[];
}

export type AgencyRunStatus = 'running' | 'done' | 'error';

export interface AgencyRun {
	id: string;
	agent: string;
	subtask: string;
	status: AgencyRunStatus;
	text: string;
	reasoning: string;
	error?: string;
	durationMs?: number;
	startedAt: number;
}

const AGENT_CONFIG_KEY = 'agentStoreConfig';

function loadSelectedAgents(): string[] {
	if (!browser) return [];
	try {
		const stored = JSON.parse(localStorage.getItem('selectedAgencyAgents') || '[]');
		return Array.isArray(stored)
			? stored.filter((name): name is string => typeof name === 'string')
			: [];
	} catch {
		return [];
	}
}

/** Hosts que solo tienen sentido en el equipo donde corre el navegador. */
const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '::1', '0.0.0.0']);

/**
 * Endpoint utilizable del servicio de agentes.
 *
 * Un endpoint guardado apuntando a loopback (`http://127.0.0.1:15000`) no sirve
 * cuando la UI se abre desde otra máquina (LAN, tailscale o nginx): apuntaría
 * al navegador del usuario y todas las llamadas fallarían. En ese caso se
 * descarta y manda `api.ts` (`serviceBase('agent')`), que resuelve host actual o
 * proxy. En localhost el valor guardado se respeta tal cual.
 */
function usableEndpoint(stored?: string | null): string {
	const candidate = (stored ?? '').trim();
	if (!candidate || !browser) return candidate || serviceBase('agent');
	try {
		const host = new URL(candidate).hostname.toLowerCase();
		const localPage = LOOPBACK_HOSTS.has(window.location.hostname.toLowerCase());
		if (LOOPBACK_HOSTS.has(host) && !localPage) return serviceBase('agent');
	} catch {
		// Endpoint relativo (`/api/...`) o inválido: se usa tal cual.
	}
	return candidate;
}

function getDefaultEndpoint(): string {
	if (!browser) return serviceBase('agent');
	try {
		const stored = localStorage.getItem(AGENT_CONFIG_KEY);
		if (stored) {
			const parsed = JSON.parse(stored);
			if (parsed?.endpoint?.trim()) return usableEndpoint(parsed.endpoint);
		}
	} catch {
		// ignore parse errors
	}
	return serviceBase('agent');
}

function loadAgentConfig(): AgentConfig {
	const endpoint = getDefaultEndpoint();
	if (!browser) {
		return {
			agentMode: true,
			selectionMode: 'manual',
			executionMode: 'sequential',
			daemonMode: false,
			autoRefresh: true,
			endpoint,
			maxRetries: 2,
			refreshIntervalMs: 5000,
			showToasts: false
		};
	}
	try {
		const stored = localStorage.getItem(AGENT_CONFIG_KEY);
		if (stored) {
			const parsed = JSON.parse(stored);			return {
				agentMode: parsed.agentMode ?? true,
				selectionMode: ['automatic', 'hybrid'].includes(parsed.selectionMode)
					? parsed.selectionMode
					: 'manual',
				executionMode: 'sequential',
				daemonMode: parsed.daemonMode ?? false,
				autoRefresh: parsed.autoRefresh ?? true,
				endpoint: usableEndpoint(parsed.endpoint) || endpoint,
				maxRetries: parsed.maxRetries ?? 2,
				refreshIntervalMs: parsed.refreshIntervalMs ?? 5000,
				showToasts: parsed.showToasts ?? false
			};
		}
	} catch {
		// ignore
	}
	return {
		agentMode: true,
		selectionMode: 'manual',
		executionMode: 'sequential',
		daemonMode: false,
		autoRefresh: true,
		endpoint,
		maxRetries: 2,
		refreshIntervalMs: 5000,
		showToasts: false
	};
}

function persistAgentConfig(config: AgentConfig) {
	if (!browser) return;
	try {
		localStorage.setItem(AGENT_CONFIG_KEY, JSON.stringify(config));
	} catch {
		// ignore
	}
}

async function requestAgent<T>(
	endpoint: string,
	path: string,
	options: RequestInit = {}
): Promise<T> {
	const headers: Record<string, string> = { ...(options.headers as Record<string, string> | undefined) };
	if (options.body) headers['Content-Type'] = 'application/json';
	// Token opcional de `api.ts` (@requires_auth en tools/auth.py).
	const token = apiToken();
	if (token) headers['Authorization'] = `Bearer ${token}`;

	const response = await fetch(`${endpoint.replace(/\/$/, '')}${path}`, {
		...options,
		headers
	});

	if (!response.ok) {
		let detail = `${response.status} ${response.statusText}`;
		try {
			const body = (await response.json()) as { error?: string };
			if (body.error) detail = body.error;
		} catch {
			// Keep the HTTP status when the server does not return JSON.
		}
		throw new Error(`Agent HTTP error: ${detail}`);
	}

	return (await response.json()) as T;
}

function createAgentStore() {
	let status = $state<AgentStatus | null>(null);
	let memory = $state<any[]>([]);
	let skills = $state<any[]>([]);
	let plan = $state<any | null>(null);
	let isLoading = $state(false);
	let lastError = $state<string | null>(null);
	let lastRefresh = $state<number>(0);
	let isConnected = $state<boolean | null>(null);
	let currentJobId = $state<string | null>(null);
	let events = $state<AgentEvent[]>([]);
	let response = $state('');
	let reasoning = $state<string[]>([]);
	let currentStep = $state('');
	let isExecuting = $state(false);
	let isPanelOpen = $state(false);
	let sensors = $state<Record<string, unknown> | null>(null);
	let checkpoints = $state<any[]>([]);
	let daemonTasks = $state<Record<string, any[]>>({
		completed: [],
		failed: [],
		lessons: [],
		tasks: []
	});
	let agencyAgents = $state<AgencyAgent[]>([]);
	let selectedAgencyAgents = $state<string[]>(loadSelectedAgents());
	let selectionFilter = $state<'all' | 'selected' | 'available'>('all');
	// --- Timeline agencial (modo AGENCY): plan, agentes trabajando, síntesis ---
	let agencyPlan = $state<AgencyPlanStep[]>([]);
	let agencyPlanRevision = $state(0);
	let agencyRuns = $state<AgencyRun[]>([]);
	let agencySupervisor = $state('');
	let agencyCurrentAgent = $state<string | null>(null);
	let agencyUsage = $state<{ promptTokens: number; completionTokens: number; totalTokens: number; promptMs: number; completionMs: number } | null>(null);
	let agencyProgress = $state<{ processed: number; total: number; cache: number } | null>(null);
	// Conversación que lanzó la ejecución agencial. Sin esto la timeline se
	// pintaba en TODOS los chats: el estado vive en el store global, que
	// sobrevive al cambio de conversación.
	let agencyConversationId = $state<string | null>(null);
	let eventSource: EventSource | null = null;
	let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
	let reconnectAttempt = 0;

	function closeEventSource() {
		eventSource?.close();
		eventSource = null;
	}

	function resetAgencyTimeline(conversationId: string | null = null) {
		agencyPlan = [];
		agencyPlanRevision = 0;
		agencyRuns = [];
		agencySupervisor = '';
		agencyCurrentAgent = null;
		agencyUsage = null;
		agencyProgress = null;
		agencyConversationId = conversationId;
	}

	/**
	 * Arranca la timeline de una ejecución agencial en una conversación concreta.
	 * La ejecución pertenece al chat que la lanzó: al cambiar de conversación la
	 * timeline deja de ser la activa (y por tanto no se muestra en otro chat).
	 */
	function beginAgencyTimeline(conversationId: string | null) {
		resetAgencyTimeline(conversationId);
		isExecuting = true;
	}

	/**
	 * Cierra la ejecución agencial (fin, cancelación o error de red) y marca como
	 * terminado cualquier agente que siguiera en "running".
	 */
	function finishAgencyTimeline() {
		isExecuting = false;
		agencyCurrentAgent = null;
		agencyRuns = agencyRuns.map((run) =>
			run.status === 'running'
				? { ...run, status: 'done' as AgencyRunStatus, durationMs: Date.now() - run.startedAt }
				: run
		);
	}

	function resetExecution() {
		closeEventSource();
		if (reconnectTimer) clearTimeout(reconnectTimer);
		reconnectTimer = null;
		reconnectAttempt = 0;
		currentJobId = null;
		currentJobConversationId = null;
		events = [];
		response = '';
		reasoning = [];
		currentStep = '';
		isExecuting = false;
		resetAgencyTimeline();
	}

	function applyEvent(event: AgentEvent) {
		events = [...events, event];
		const value = event.data ?? event.payload;
		const text = typeof value === 'string' ? value : '';

		switch (event.type) {
			case 'text':
				response += text || String((value as { text?: unknown } | undefined)?.text ?? '');
				break;
			case 'reasoning':
				reasoning = [
					...reasoning,
					text || String((value as { text?: unknown } | undefined)?.text ?? '')
				];
				break;
			case 'step_start':
				currentStep =
					text ||
					String(
						(value as { step?: unknown; subtask?: unknown; agent?: unknown } | undefined)?.step ??
							(value as { subtask?: unknown } | undefined)?.subtask ??
							(value as { agent?: unknown } | undefined)?.agent ??
							''
					);
				break;
			case 'plan':
				plan = value;
				break;
			case 'done':
				if (!response) {
					const finalResult = (value as { result?: unknown } | undefined)?.result;
					if (typeof finalResult === 'string') response = finalResult;
					else if (finalResult && typeof finalResult === 'object') {
						const content = (finalResult as { content?: unknown }).content;
						if (typeof content === 'string') response = content;
					}
				}
				isExecuting = false;
				closeEventSource();
				break;
			case 'error':
				lastError =
					text || String((value as { error?: unknown } | undefined)?.error ?? 'Agent error');
				isExecuting = false;
				closeEventSource();
				break;
		}
	}

	function applyChatAgencyEvent(event: AgentEvent) {
		if (event.job_id) currentJobId = event.job_id;
		events = [...events, event];
		if (event.type !== 'done' && event.type !== 'error') isExecuting = true;
		const value = event.data ?? event.payload;
		const valueObj = (value && typeof value === 'object' ? value : {}) as Record<string, any>;
		const meta = (event.meta ?? {}) as Record<string, any>;
		const agentName = typeof meta.agent === 'string' ? meta.agent : '';
		const textValue =
			typeof value === 'string' ? value : String(valueObj.text ?? '');

		switch (event.type) {
			case 'agency_selected': {
				status = {
					...(status ?? { confidence: 0, memory: [], plan: null, skills: [], task: '' }),
					confidence: 1,
					mode: config.daemonMode ? 'DAEMON' : 'AGENCY',
					task: ''
				};
				const steps = Array.isArray(valueObj.steps) ? valueObj.steps : [];
				if (steps.length) {
					agencyPlan = steps.map((step: any, index: number) => ({
						id: String(step?.id ?? `agency_${index}`),
						subtask: String(step?.subtask ?? ''),
						agent_name: String(step?.agent_name ?? step?.agent ?? ''),
						depends_on: Array.isArray(step?.depends_on) ? step.depends_on : []
					}));
					agencyPlanRevision += 1;
				}
				break;
			}
			case 'plan': {
				plan = value;
				const steps = Array.isArray(valueObj.steps) ? valueObj.steps : [];
				if (steps.length) {
					agencyPlan = steps.map((step: any, index: number) => ({
						id: String(step?.id ?? `agency_${index}`),
						subtask: String(step?.subtask ?? ''),
						agent_name: String(step?.agent_name ?? step?.agent ?? ''),
						depends_on: Array.isArray(step?.depends_on) ? step.depends_on : []
					}));
					agencyPlanRevision += 1;
				}
				break;
			}
			case 'step_start':
				currentStep = String(valueObj.subtask ?? valueObj.agent ?? '');
				break;
			case 'agent_start': {
				const agent = String(valueObj.agent ?? agentName ?? 'agente');
				const subtask = String(valueObj.subtask ?? '');
				agencyCurrentAgent = agent;
				agencyRuns = [
					...agencyRuns,
					{
						id: String(valueObj.task_id ?? `${agent}-${agencyRuns.length}`),
						agent,
						subtask,
						status: 'running',
						text: '',
						reasoning: '',
						startedAt: Date.now()
					}
				];
				break;
			}
			case 'agent_end': {
				const agent = String(valueObj.agent ?? agentName ?? 'agente');
				const success = valueObj.success !== false;
				const taskId = String(valueObj.task_id ?? '');
				const index = agencyRuns.findIndex(
					(run) => (taskId && run.id === taskId) || (!taskId && run.agent === agent && run.status === 'running')
				);
				if (index >= 0) {
					const runs = [...agencyRuns];
					runs[index] = {
						...runs[index],
						status: success ? 'done' : 'error',
						error: success ? undefined : String(valueObj.error ?? 'Error del agente'),
						durationMs: Date.now() - runs[index].startedAt
					};
					agencyRuns = runs;
				}
				// FIX BUG SILENCIOSO: Verificar si hay más agentes activos antes de cerrar stream
				const activeRuns = agencyRuns.filter((r) => r.status === 'running');
				if (activeRuns.length === 0) {
					agencyCurrentAgent = null;
				} else {
					// Mantener stream visible para el siguiente agente
					agencyCurrentAgent = activeRuns[0].agent;
				}
				break;
			}
			case 'handoff':
				// Contexto transferido entre agentes: se refleja en la timeline.
				break;
			case 'supervisor': {
				if (textValue) agencySupervisor += textValue;
				break;
			}
			case 'text':
			case 'reasoning': {
				// Texto del supervisor → respuesta final; texto de un agente →
				// su tarjeta de timeline (streaming en tiempo real).
				const isSupervisor = meta.role === 'supervisor' || !agentName;
				if (isSupervisor) {
					// La voz del supervisor alimenta el mensaje final del chat y, en
					// paralelo, el panel de la timeline (estado del plan + síntesis).
					if (event.type === 'text') {
						response += textValue;
						agencySupervisor += textValue;
					} else if (textValue) {
						reasoning = [...reasoning, textValue];
					}
					break;
				}
				const index = agencyRuns.findIndex(
					(run) => run.agent === agentName && run.status === 'running'
				);
				if (index >= 0) {
					const runs = [...agencyRuns];
					runs[index] =
						event.type === 'text'
							? { ...runs[index], text: runs[index].text + textValue }
							: { ...runs[index], reasoning: runs[index].reasoning + textValue };
					agencyRuns = runs;
				} else if (textValue) {
					agencySupervisor += textValue;
				}
				break;
			}
			case 'usage': {
				// El uso de un subagente no debe pisar las métricas finales del job.
				if (meta.role === 'specialist') break;
				agencyUsage = {
					promptTokens: Number(valueObj.prompt_tokens ?? 0),
					completionTokens: Number(valueObj.completion_tokens ?? 0),
					totalTokens: Number(valueObj.total_tokens ?? 0),
					promptMs: Number(valueObj.prompt_ms ?? 0),
					completionMs: Number(valueObj.completion_ms ?? 0)
				};
				break;
			}
			case 'progress': {
				agencyProgress = {
					processed: Number(valueObj.processed ?? valueObj.n_past ?? 0),
					total: Number(valueObj.total ?? valueObj.prompt_tokens ?? 0),
					cache: Number(valueObj.cache ?? 0)
				};
				break;
			}
			case 'done':
			case 'error': {
				// Fallo puntual de un paso/subagente (lleva `agent` en el payload):
				// no finaliza el job, el supervisor re-planifica y sigue.
				if (event.type === 'error' && typeof valueObj.agent === 'string') {
					const taskId = String(valueObj.task_id ?? '');
					const agent = String(valueObj.agent);
					const index = agencyRuns.findIndex(
						(run) =>
							(run.status === 'running' && taskId && run.id === taskId) ||
							(run.status === 'running' && !taskId && run.agent === agent)
					);
					if (index >= 0) {
						const runs = [...agencyRuns];
						runs[index] = {
							...runs[index],
							status: 'error',
							error: String(valueObj.error ?? 'Error del agente'),
							durationMs: Date.now() - runs[index].startedAt
						};
						agencyRuns = runs;
					}
					break;
				}
				status = {
					...(status ?? { confidence: 0, memory: [], plan: null, skills: [], task: '' }),
					mode: event.type === 'error' ? 'ERROR' : 'GENERAL'
				};
				isExecuting = false;
				agencyCurrentAgent = null;
				const finished = agencyRuns.map((run) =>
					run.status === 'running'
						? {
								...run,
								status: (event.type === 'error' ? 'error' : 'done') as AgencyRunStatus,
								durationMs: Date.now() - run.startedAt
							}
						: run
				);
				agencyRuns = finished;
				if (event.type === 'error' && !lastError) {
					lastError = String(valueObj.error ?? 'Agent error');
				}
				break;
			}
		}
	}

	function connectToJob(jobId: string) {
		closeEventSource();
		const source = new EventSource(`${config.endpoint.replace(/\/$/, '')}/agent_events/${jobId}`);
		eventSource = source;
		source.onopen = () => {
			isConnected = true;
			reconnectAttempt = 0;
		};
		/**
		 * El navegador entrega el evento `error` NATIVO (caída o cierre del stream)
		 * por el mismo canal que un evento SSE llamado "error", y un `Event` pelado
		 * no trae `data`: parsearlo producía un falso "Evento SSE inválido" al
		 * cerrarse la conexión.
		 */
		const parseEvent = (message: MessageEvent) => {
			if (typeof message?.data !== 'string' || !message.data) return;
			try {
				applyEvent(JSON.parse(message.data) as AgentEvent);
			} catch {
				lastError = 'Evento SSE invalido';
			}
		};
		source.onmessage = parseEvent;
		for (const type of [
			'text',
			'reasoning',
			'tool_call',
			'native_tool_call',
			'tool_result',
			'step_start',
			'step_end',
			'plan',
			'agency_selected',
			'agent_start',
			'agent_end',
			'handoff',
			'supervisor',
					'progress',
			'error',
			'done'
		]) {
			source.addEventListener(type, (message) => parseEvent(message as MessageEvent));
		}
		source.onerror = () => {
			isConnected = false;
			closeEventSource();
			if (isExecuting && currentJobId === jobId) {
				const delay = Math.min(1000 * 2 ** reconnectAttempt++, 30000);
				reconnectTimer = setTimeout(() => connectToJob(jobId), delay);
			}
		};
	}

	async function execute(
		task: string,
		context?: unknown,
		agents = selectedAgencyAgents
	): Promise<string> {
		resetExecution();
		isLoading = true;
		isExecuting = true;
		try {
			// El modo investigación delega en el RAG real del chat (ragStore);
			// solo enviamos la bandera para que el backend lo sepa.
			const research = get(ragResearchMode);
			const result = await fetch(`${config.endpoint.replace(/\/$/, '')}/agent_execute`, {
				body: JSON.stringify({ agents, context, task, research }),
				headers: { 'Content-Type': 'application/json' },
				method: 'POST'
			});
			if (!result.ok) throw new Error(`Agent HTTP error: ${result.status}`);
			const data = (await result.json()) as { job_id?: string };
			if (!data.job_id) throw new Error('El agente no devolvio job_id');
			currentJobId = data.job_id;
			connectToJob(data.job_id);
			return data.job_id;
		} catch (error) {
			lastError = error instanceof Error ? error.message : String(error);
			isExecuting = false;
			throw error;
		} finally {
			isLoading = false;
		}
	}

	async function fetchAgencyAgents(query = ''): Promise<AgencyAgent[]> {
		try {
			const result = await requestAgent<{ agents?: AgencyAgent[] }>(
				config.endpoint,
				`/api/agents?query=${encodeURIComponent(query)}`
			);
			agencyAgents = result.agents ?? [];
			lastError = null;
			return agencyAgents;
		} catch (error) {
			lastError = error instanceof Error ? error.message : String(error);
			return [];
		}
	}

	// ------------------------------------------------------------------
	// CRUD del catálogo de agentes
	// ------------------------------------------------------------------
	// Todo el API de agentes pasa por `requestAgent`, que resuelve el endpoint
	// con `api.ts` (host actual o proxy) y añade el token si existe. Nada de
	// fetch sueltos en los componentes: así el mismo código funciona en
	// localhost y desde otra máquina de la red.

	/** Agente del catálogo con su markdown completo. */
	async function fetchAgentDetail(name: string): Promise<AgencyAgentDetail | null> {
		const result = await requestAgent<{ agent?: AgencyAgentDetail | null }>(
			config.endpoint,
			`/api/agents/${encodeURIComponent(name)}`
		);
		return result.agent ?? null;
	}

	/** Crea (o sobreescribe) un agente a partir de su markdown crudo. */
	async function createAgent(name: string, content: string): Promise<void> {
		await requestAgent<{ success?: boolean }>(config.endpoint, '/api/agents', {
			body: JSON.stringify({ content, name }),
			method: 'POST'
		});
	}

	/** Guarda la edición del markdown de un agente existente. */
	async function saveAgent(name: string, content: string): Promise<void> {
		await requestAgent<{ success?: boolean }>(
			config.endpoint,
			`/api/agents/${encodeURIComponent(name)}`,
			{ body: JSON.stringify({ content }), method: 'PUT' }
		);
	}

	async function deleteAgent(name: string): Promise<void> {
		await requestAgent<{ success?: boolean }>(
			config.endpoint,
			`/api/agents/${encodeURIComponent(name)}`,
			{ method: 'DELETE' }
		);
	}

	function setSelectedAgencyAgents(names: string[]) {
		selectedAgencyAgents = [...new Set(names)];
		if (browser) localStorage.setItem('selectedAgencyAgents', JSON.stringify(selectedAgencyAgents));
	}

	function clearSelectedAgencyAgents() {
		setSelectedAgencyAgents([]);
	}

	/**
	 * Conversaciones cuyo stream agencial ya sigue el chat nativo.
	 *
	 * Un job solo debe tener UNA conexión SSE por pestaña: cuando el chat se
	 * engancha a un job (envío nuevo o reanudación tras recargar), la marca evita
	 * que el panel de agentes abra la suya y duplique eventos.
	 */
	/**
	 * Conversación del job que sigue el EventSource propio del panel.
	 *
	 * El layout restaura jobs al montar y el chat se engancha después: este
	 * vínculo permite que `claimAgencyJob` suelte la conexión del panel cuando
	 * el chat pasa a ser el único consumidor del job.
	 */
	let currentJobConversationId: string | null = null;

	const claimedAgencyConversations = new Set<string>();

	/** Marca que el chat nativo sigue el job de esta conversación. */
	function claimAgencyJob(conversationId: string): void {
		claimedAgencyConversations.add(conversationId);

		if (currentJobConversationId !== conversationId) return;

		// El panel ya se había enganchado a este job (la restauración del layout
		// monta antes que la página): se suelta su EventSource y se limpia la
		// timeline para que la reconstruya el stream del chat desde el principio,
		// con un solo consumidor y sin eventos duplicados.
		if (reconnectTimer) clearTimeout(reconnectTimer);

		reconnectTimer = null;
		reconnectAttempt = 0;
		closeEventSource();
		currentJobId = null;
		currentJobConversationId = null;
		resetAgencyTimeline(conversationId);
	}

	/** True si el chat nativo ya sigue el job agencial de esta conversación. */
	function isAgencyJobClaimed(conversationId: string): boolean {
		return claimedAgencyConversations.has(conversationId);
	}

	/**
	 * Reengancha la timeline del panel a un job que sigue vivo en el backend.
	 *
	 * Los jobs lanzados desde el chat (modo agencia) los sigue el propio chat
	 * nativo, que alimenta esta timeline con `applyChatAgencyEvent` desde su
	 * stream SSE: abrir aquí una segunda conexión duplicaría eventos, así que
	 * esos jobs (reclamados con {@link claimAgencyJob}) se ignoran.
	 */
	async function restoreActiveJob(conversationId?: string | null): Promise<boolean> {
		if (isExecuting || currentJobId) return false;
		try {
			const result = await requestAgent<{
				jobs?: Array<{ conversation_id?: string | null; id?: string; status?: string }>;
			}>(
				config.endpoint,
				conversationId
					? `/agent_jobs?conversation_id=${encodeURIComponent(conversationId)}`
					: '/agent_jobs'
			);
			const activeJob = result.jobs?.find(
				(job) =>
					job.status === 'running' &&
					job.id &&
					!(typeof job.conversation_id === 'string' && isAgencyJobClaimed(job.conversation_id))
			);
			if (!activeJob?.id) return false;
			currentJobId = activeJob.id;
			currentJobConversationId =
				typeof activeJob.conversation_id === 'string' ? activeJob.conversation_id : null;
			isExecuting = true;
			connectToJob(activeJob.id);
			return true;
		} catch (error) {
			lastError = error instanceof Error ? error.message : String(error);
			return false;
		}
	}

	async function cancel(): Promise<void> {
		if (!currentJobId) return;
		await fetch(`${config.endpoint.replace(/\/$/, '')}/agent_execute/${currentJobId}`, {
			method: 'DELETE'
		});
		isExecuting = false;
		closeEventSource();
		currentJobId = null;
		currentJobConversationId = null;
	}

	let config = $state<AgentConfig>(loadAgentConfig());

	function updateConfig(patch: Partial<AgentConfig>) {
		config = { ...config, ...patch };
		persistAgentConfig(config);
	}

	function togglePanel() {
		isPanelOpen = !isPanelOpen;
	}

	function toggleAgentMode() {
		const nextMode = !config.agentMode;
		updateConfig({ agentMode: nextMode });
	}

	/**
	 * Sondeo global en segundo plano: mantiene el estado del agente vivo
	 * (conexión, status, memoria, plan) aunque el modal esté cerrado.
	 * Idempotente: se puede llamar varias veces.
	 */
	let pollingStarted = false;
	function ensurePolling() {
		if (pollingStarted || !browser) return;
		pollingStarted = true;
		void refreshAll();
		setInterval(() => {
			if (!config.autoRefresh) return;
			void refreshAll();
		}, config.refreshIntervalMs);
	}

	function toggleDaemonMode() {
		const daemonMode = !config.daemonMode;
		updateConfig({ agentMode: daemonMode || config.agentMode, daemonMode });
	}

	async function fetchStatus(conversationId?: string): Promise<AgentStatus | null> {
		try {
			const result = await requestAgent<{
				orchestrator?: { agents?: string[] };
				jobs?: { running?: number };
				active_job?: {
					id?: string;
					status?: string;
					task?: string;
					agents?: string[];
					parallel?: boolean;
				} | null;
				daemon?: { is_running?: boolean; is_paused?: boolean; objective?: string | null };
			}>(config.endpoint, conversationId ? `/agent_status?conversation_id=${encodeURIComponent(conversationId)}` : '/agent_status');
			const daemon = result.daemon;
			if (result && typeof result === 'object') {
				const newStatus: AgentStatus = {
					confidence: daemon?.is_running ? 1 : 0,
					memory: [],
					mode: daemon?.is_paused ? 'PAUSED' : daemon?.is_running ? 'DAEMON' : 'GENERAL',
					plan: null,
					skills: result.orchestrator?.agents ?? [],
					task:
						result.active_job?.task ??
						daemon?.objective ??
						`${result.jobs?.running ?? 0} tareas en ejecución`
				};
				status = newStatus;
				lastError = null;
				isConnected = true;
				lastRefresh = Date.now();
				return newStatus;
			}
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			lastError = msg;
			isConnected = false;
			if (config.showToasts) toast.error(`Agente: ${msg}`);
		}
		return null;
	}

	async function fetchMemory(limit: number = 10): Promise<any[]> {
		try {
			const result = await requestAgent<{ episodes?: any[] }>(
				config.endpoint,
				`/agent_memory?limit=${limit}&offset=0`
			);
			if (Array.isArray(result.episodes)) {
				memory = result.episodes;
				lastError = null;
				return result.episodes;
			}
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			lastError = msg;
			if (config.showToasts) toast.error(`Memoria: ${msg}`);
		}
		return [];
	}

	async function fetchSkills(query: string = ''): Promise<any[]> {
		try {
			const result = await requestAgent<{ skills?: any[] }>(
				config.endpoint,
				`/agent_skills?query=${encodeURIComponent(query)}`
			);
			if (Array.isArray(result.skills)) {
				skills = result.skills;
				lastError = null;
				return result.skills;
			}
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			lastError = msg;
			if (config.showToasts) toast.error(`Skills: ${msg}`);
		}
		return [];
	}

	async function fetchPlan(conversationId?: string): Promise<any | null> {
		try {
			const result = await requestAgent<{ active_plan?: any }>(
				config.endpoint,
				conversationId ? `/agent_plan?conversation_id=${encodeURIComponent(conversationId)}` : '/agent_plan'
			);
			if (result && typeof result === 'object') {
				plan = result.active_plan ?? null;
				lastError = null;
				return plan;
			}
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			lastError = msg;
			if (config.showToasts) toast.error(`Plan: ${msg}`);
		}
		return null;
	}

	async function fetchSensors(): Promise<Record<string, unknown> | null> {
		try {
			const result = await requestAgent<Record<string, unknown>>(config.endpoint, '/agent_sensors');
			sensors = result;
			lastError = null;
			return result;
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			lastError = msg;
			if (config.showToasts) toast.error(`Sensores: ${msg}`);
			return null;
		}
	}

	async function fetchCheckpoints(limit = 20): Promise<any[]> {
		try {
			const result = await requestAgent<{ checkpoints?: any[] }>(
				config.endpoint,
				`/agent_checkpoints?limit=${limit}`
			);
			checkpoints = result.checkpoints ?? [];
			lastError = null;
			return checkpoints;
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			lastError = msg;
			if (config.showToasts) toast.error(`Checkpoints: ${msg}`);
			return [];
		}
	}

	async function rollback(checkpointId: string): Promise<boolean> {
		try {
			await requestAgent(config.endpoint, '/agent_rollback', {
				body: JSON.stringify({ checkpoint_id: checkpointId }),
				method: 'POST'
			});
			await fetchCheckpoints();
			if (config.showToasts) toast.success('Checkpoint restaurado');
			return true;
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			lastError = msg;
			if (config.showToasts) toast.error(`Rollback: ${msg}`);
			return false;
		}
	}

	async function fetchDaemonTasks(): Promise<Record<string, any[]>> {
		try {
			const result = await requestAgent<Record<string, any[]>>(config.endpoint, '/daemon_tasks');
			daemonTasks = result;
			lastError = null;
			return result;
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			lastError = msg;
			return daemonTasks;
		}
	}

	async function startDaemon(objective: string): Promise<boolean> {
		try {
			await requestAgent(config.endpoint, '/daemon_start', {
				body: JSON.stringify({ objective }),
				method: 'POST'
			});
			await fetchStatus();
			return true;
		} catch (e) {
			lastError = e instanceof Error ? e.message : String(e);
			return false;
		}
	}

	async function stopDaemon(): Promise<boolean> {
		try {
			await requestAgent(config.endpoint, '/daemon_stop', { method: 'POST' });
			await fetchStatus();
			return true;
		} catch (e) {
			lastError = e instanceof Error ? e.message : String(e);
			return false;
		}
	}

	async function refreshAll(conversationId?: string): Promise<void> {
		if (isLoading) return;
		isLoading = true;
		try {
			await Promise.allSettled([
				fetchStatus(conversationId),
				fetchMemory(),
				fetchSkills(),
				fetchPlan(conversationId),
				fetchSensors(),
				fetchCheckpoints(),
				fetchDaemonTasks()
			]);
		} finally {
			isLoading = false;
		}
	}

	async function setGoal(text: string): Promise<boolean> {
		try {
			await requestAgent(config.endpoint, '/agent_goal', {
				body: JSON.stringify({ objective: text }),
				method: 'POST'
			});
			await fetchStatus();
			if (config.showToasts) toast.success('Objetivo establecido');
			return true;
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			lastError = msg;
			if (config.showToasts) toast.error(`Goal: ${msg}`);
			return false;
		}
	}

	async function pause(): Promise<boolean> {
		try {
			await requestAgent(config.endpoint, '/daemon_pause', { method: 'POST' });
			await fetchStatus();
			if (config.showToasts) toast.success('Daemon pausado');
			return true;
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			lastError = msg;
			if (config.showToasts) toast.error(`Pause: ${msg}`);
			return false;
		}
	}

	async function resume(): Promise<boolean> {
		try {
			await requestAgent(config.endpoint, '/daemon_resume', { method: 'POST' });
			await fetchStatus();
			if (config.showToasts) toast.success('Daemon reanudado');
			return true;
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			lastError = msg;
			if (config.showToasts) toast.error(`Resume: ${msg}`);
			return false;
		}
	}
	/**
	 * Bloque de contexto (RF2): describe agentes seleccionados y skills
	 * disponibles para inyectarlo en el system prompt del chat nativo.
	 * Devuelve null si el modo agente está apagado o nada está activo.
	 */
	function buildContextBlock(): string | null {
		if (!config.agentMode) return null;
		const selected = agencyAgents.filter((a) => selectedAgencyAgents.includes(a.name));
		const autoAgents =
			config.selectionMode !== 'manual'
				? agencyAgents.slice(0, 12)
				: [];
		const lines: string[] = [];
		if (selected.length || autoAgents.length) {
			lines.push('## Modo agente (AGENCY) activo');
			if (selected.length) {
				lines.push('Agentes seleccionados por el usuario — úsalos tal cual se piden:');
				for (const a of selected) {
					lines.push(`- **${a.name}** (${a.emoji} ${a.category}): ${a.description}`);
				}
			}
			if (autoAgents.length) {
				lines.push(
					`Modo de selección "${config.selectionMode}": elige y delega en los agentes más adecuados de este catálogo:`
				);
				for (const a of autoAgents) {
					lines.push(`- ${a.name} (${a.category}): ${a.description}`);
				}
			}
			lines.push(
				'Ejecución: ' +
					(config.executionMode === 'parallel' ? 'paralela' : 'secuencial') +
					'. Cuando la tarea lo requiera, delega explicitamente nombrando al agente.'
			);
		}
		// Solo llegan los skills ACTIVOS (el checkbox del modal es la fuente de
		// verdad: /agent_skills filtra por `active`).
		const skillList = (skills ?? []).slice(0, 20);
		if (skillList.length) {
			lines.push('## Skills activos (procedimientos aprendidos)');
			for (const s of skillList) {
				const name = s?.name ?? s?.title ?? s?.skill_id ?? '';
				const desc = s?.description ?? '';
				if (name) lines.push(`- ${name}${desc ? `: ${desc}` : ''}`);
			}
		}
		if (!lines.length) return null;
		return lines.join('\n');
	}

	return {
		applyChatAgencyEvent,
		beginAgencyTimeline,
		finishAgencyTimeline,
		resetAgencyTimeline,
		get agencyConversationId() {
			return agencyConversationId;
		},
		get agencyPlan() {
			return agencyPlan;
		},
		get agencyPlanRevision() {
			return agencyPlanRevision;
		},
		get agencyRuns() {
			return agencyRuns;
		},
		get agencySupervisor() {
			return agencySupervisor;
		},
		get agencyCurrentAgent() {
			return agencyCurrentAgent;
		},
		get agencyUsage() {
			return agencyUsage;
		},
		get agencyProgress() {
			return agencyProgress;
		},
		get config() {
			return config;
		},
		get currentJobId() {
			return currentJobId;
		},
		get events() {
			return events;
		},
		get response() {
			return response;
		},
		get reasoning() {
			return reasoning;
		},
		get currentStep() {
			return currentStep;
		},
		get isExecuting() {
			return isExecuting;
		},
		get isPanelOpen() {
			return isPanelOpen;
		},
		get sensors() {
			return sensors;
		},
		get checkpoints() {
			return checkpoints;
		},
		get daemonTasks() {
			return daemonTasks;
		},
		get agencyAgents() {
			return agencyAgents;
		},
		get selectedAgencyAgents() {
			return selectedAgencyAgents;
		},
		get selectionFilter() {
			return selectionFilter;
		},
		get isConnected() {
			return isConnected;
		},
		get isLoading() {
			return isLoading;
		},
		get lastError() {
			return lastError;
		},
		get lastRefresh() {
			return lastRefresh;
		},
		get memory() {
			return memory;
		},
		get plan() {
			return plan;
		},
		get skills() {
			return skills;
		},
		get status() {
			return status;
		},
		fetchMemory,
		fetchPlan,
		fetchSkills,
		fetchStatus,
		cancel,
		execute,
		fetchCheckpoints,
		fetchDaemonTasks,
		fetchAgencyAgents,
		fetchAgentDetail,
		createAgent,
		saveAgent,
		deleteAgent,
		fetchSensors,
		restoreActiveJob,
		claimAgencyJob,
		isAgencyJobClaimed,
		pause,
		refreshAll,
		resume,
		rollback,
		setGoal,
		setSelectedAgencyAgents,
		clearSelectedAgencyAgents,
		setSelectionFilter: (filter: 'all' | 'selected' | 'available') => {
			selectionFilter = filter;
		},
		startDaemon,
		stopDaemon,
		togglePanel,
		toggleAgentMode,
		toggleDaemonMode,
		ensurePolling,
		buildContextBlock,
		updateConfig
	};
}

export const agentStore = createAgentStore();
