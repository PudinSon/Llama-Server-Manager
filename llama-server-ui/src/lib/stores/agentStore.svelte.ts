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
import { toast } from 'svelte-sonner';

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

/**
 * Tipos que emite el stream del job agencial (`tools/agent_core.py`).
 * Un evento desconocido no revienta el reductor (el tipo admite `string`),
 * pero esta lista es el contrato que el panel SÍ sabe pintar.
 */
export type AgentEventType =
	| 'text'
	| 'reasoning'
	| 'tool_call'
	| 'native_tool_call'
	| 'tool_result'
	| 'tool_denied'
	| 'step_start'
	| 'step_end'
	| 'agency_selected'
	| 'plan'
	| 'agent_start'
	| 'agent_end'
	| 'handoff'
	| 'supervisor'
	| 'usage'
	| 'progress'
	| 'incomplete'
	| 'error'
	| 'done';

export interface AgentEvent {
	type: AgentEventType | string;
	data?: unknown;
	payload?: unknown;
	job_id?: string;
	/** Papel y agente del frame: `{ role: 'specialist', agent: 'Analista' }`. */
	meta?: Record<string, unknown>;
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

/**
 * Estado de un paso del plan. Lo mueve el reducer con los eventos REALES del
 * backend (`step_start`/`agent_start` lo ponen en marcha, `agent_end`/
 * `step_end` lo cierran), no con un campo `status` que nadie emite.
 */
export type AgencyPlanStatus = 'pending' | 'running' | 'done' | 'error';

export interface AgencyPlanStep {
	id: string;
	subtask: string;
	agent_name: string;
	depends_on?: string[];
	phases?: string[];
	/** Fase del pipeline (`pipeline_templates`): setup, core, tests... */
	phase?: string;
	/** Artefacto que produce el paso (si lo declara). */
	artifact?: string | null;
	status: AgencyPlanStatus;
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
	/** Herramientas que la whitelist del agente le denegó (`tool_denied`). */
	denied?: string[];
	durationMs?: number;
	startedAt: number;
}

/** Herramienta denegada por la policy del agente (`tool_denied`). */
export interface AgencyDenial {
	agent: string;
	name: string;
	reason: string;
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
			const parsed = JSON.parse(stored);
			return {
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
	const headers: Record<string, string> = {
		...(options.headers as Record<string, string> | undefined)
	};
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
	let isLoading = $state(false);
	let lastError = $state<string | null>(null);
	let lastRefresh = $state<number>(0);
	let isConnected = $state<boolean | null>(null);
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
	let agencyGoal = $state('');
	let agencyPlanRevision = $state(0);
	let agencyRuns = $state<AgencyRun[]>([]);
	let agencySupervisor = $state('');
	let agencyCurrentAgent = $state<string | null>(null);
	let agencyUsage = $state<{
		promptTokens: number;
		completionTokens: number;
		totalTokens: number;
		promptMs: number;
		completionMs: number;
	} | null>(null);
	let agencyProgress = $state<{ processed: number; total: number; cache: number } | null>(null);
	/**
	 * Motivo del último `incomplete` del backend: el plan se cerró sin que todos
	 * los pasos produjeran trabajo (paso cortado por la guarda, artefacto que no
	 * apareció o recuperación agotada). Es un aviso, no el fin del job: el
	 * stream sigue y el `done` sin meta lo cierra.
	 */
	let agencyIncomplete = $state<string | null>(null);
	/** Herramientas denegadas por la whitelist del agente, en orden de llegada. */
	let agencyDenials = $state<AgencyDenial[]>([]);
	// Conversación que lanzó la ejecución agencial. Sin esto la timeline se
	// pintaba en TODOS los chats: el estado vive en el store global, que
	// sobrevive al cambio de conversación.
	let agencyConversationId = $state<string | null>(null);
	// Este store NO abre el stream del job: el dueño del `EventSource` es el
	// chat que lanzó la tarea (`chatStore.consumeAgencyJob` sobre el transporte
	// `$lib/utils/agency-stream`), y de ahí sale cada frame hacia el reductor
	// `applyChatAgencyEvent`. Antes el panel abría además su propia conexión con
	// su propia lista de tipos y su propia reconexión: el mismo job se veía
	// distinto según quién escuchara, y al reclamarlo el chat había que tirar la
	// conexión del panel y reconstruir la timeline desde cero.

	function resetAgencyTimeline(conversationId: string | null = null) {
		agencyPlan = [];
		agencyGoal = '';
		agencyPlanRevision = 0;
		agencyRuns = [];
		agencySupervisor = '';
		agencyCurrentAgent = null;
		agencyUsage = null;
		agencyProgress = null;
		agencyIncomplete = null;
		agencyDenials = [];
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

	/**
	 * (Eliminado) Segundo reductor, `applyEvent`.
	 *
	 * El panel tenía su propio switch (texto, razonamiento, plan y cierre) y el
	 * chat tenía otro (`applyChatAgencyEvent`): el mismo backend se veía distinto
	 * según quién escuchara, y este se quedaba corto (sin `usage`, sin
	 * `incomplete`, sin `tool_denied` y sin estados por paso del plan). Ahora hay
	 * UN reductor —`applyChatAgencyEvent`— y los dos caminos pasan por él.
	 */

	/**
	 * Modelo ÚNICO del plan. `agency_selected` y `plan` traen los mismos pasos
	 * (el segundo es la replanificación), así que los dos pasan por aquí.
	 *
	 * Los estados se conservan entre replanificaciones por id de paso: si la
	 * recuperación reescribe el plan, lo ya terminado sigue terminado y solo el
	 * paso correctivo (`1R1`) queda pendiente.
	 */
	const planFromSteps = (raw: unknown): AgencyPlanStep[] => {
		if (!Array.isArray(raw)) return [];

		return raw.map((step, index) => {
			const data = (step && typeof step === 'object' ? step : {}) as Record<string, unknown>;

			return {
				agent_name: String(data.agent_name ?? data.agent ?? ''),
				artifact: typeof data.artifact === 'string' ? data.artifact : null,
				depends_on: Array.isArray(data.depends_on) ? (data.depends_on as string[]) : [],
				id: String(data.id ?? `agency_${index}`),
				phases: Array.isArray(data.phases) ? (data.phases as string[]) : undefined,
				phase: typeof data.phase === 'string' ? data.phase : undefined,
				status: 'pending' as AgencyPlanStatus,
				subtask: String(data.subtask ?? '')
			};
		});
	};

	const applyPlan = (raw: unknown): void => {
		const next = planFromSteps(raw);

		if (!next.length) return;

		const previous = new Map(agencyPlan.map((step) => [step.id, step.status]));

		agencyPlan = next.map((step) => ({ ...step, status: previous.get(step.id) ?? step.status }));
		agencyPlanRevision += 1;
	};

	/**
	 * Paso del plan al que pertenece un evento. `step_start` trae `idx` (1-based)
	 * y `agent_start`/`agent_end` traen `task_id`; si falta, se cae al agente que
	 * esté en marcha para no perder el estado del paso.
	 */
	const planStepIdFor = (payload: Record<string, any>, agent: string): string | undefined => {
		const explicit = payload.task_id ?? payload.id;

		if (typeof explicit === 'string' && explicit) return explicit;

		const idx = Number(payload.idx ?? 0);

		if (idx > 0 && agencyPlan[idx - 1]) return agencyPlan[idx - 1].id;

		return agencyPlan.find((step) => step.agent_name === agent && step.status === 'running')?.id;
	};

	const setPlanStepStatus = (
		payload: Record<string, any>,
		agent: string,
		stepStatus: AgencyPlanStatus
	): void => {
		const id = planStepIdFor(payload, agent);

		if (!id) return;

		const index = agencyPlan.findIndex((step) => step.id === id);

		if (index < 0 || agencyPlan[index].status === stepStatus) return;

		const steps = [...agencyPlan];

		steps[index] = { ...steps[index], status: stepStatus };
		agencyPlan = steps;
	};

	function applyChatAgencyEvent(event: AgentEvent) {
		events = [...events, event];
		if (event.type !== 'done' && event.type !== 'error') isExecuting = true;
		const value = event.data ?? event.payload;
		const valueObj = (value && typeof value === 'object' ? value : {}) as Record<string, any>;
		const meta = (event.meta ?? {}) as Record<string, any>;
		const agentName = typeof meta.agent === 'string' ? meta.agent : '';
		const textValue = typeof value === 'string' ? value : String(valueObj.text ?? '');

		switch (event.type) {
			case 'agency_selected': {
				if (typeof valueObj.task === 'string') agencyGoal = valueObj.task;
				status = {
					...(status ?? { confidence: 0, memory: [], plan: null, skills: [], task: '' }),
					confidence: 1,
					mode: config.daemonMode ? 'DAEMON' : 'AGENCY',
					task: ''
				};
				applyPlan(valueObj.steps);
				break;
			}
			case 'plan': {
				if (typeof valueObj.task === 'string') agencyGoal = valueObj.task;
				// Replanificación (recuperación de un paso fallido) o plan inicial:
				// `applyPlan` conserva el estado de los pasos que ya estaban hechos.
				applyPlan(valueObj.steps);
				break;
			}
			case 'step_start':
				currentStep = String(valueObj.subtask ?? valueObj.agent ?? '');
				setPlanStepStatus(valueObj, agentName, 'running');
				break;
			case 'step_end': {
				// El paso terminó: `result` si produjo trabajo, `error` si no. Es el
				// cierre de la fila del plan (y no existía como caso: el plan se
				// quedaba en "pendiente" toda la ejecución).
				const agent = String(valueObj.agent ?? agentName ?? '');
				setPlanStepStatus(valueObj, agent, valueObj.error ? 'error' : 'done');
				break;
			}
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
				setPlanStepStatus(valueObj, agent, 'running');
				break;
			}
			case 'agent_end': {
				const agent = String(valueObj.agent ?? agentName ?? 'agente');
				const success = valueObj.success !== false;
				const taskId = String(valueObj.task_id ?? '');
				const index = agencyRuns.findIndex(
					(run) =>
						(taskId && run.id === taskId) ||
						(!taskId && run.agent === agent && run.status === 'running')
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
				setPlanStepStatus(valueObj, agent, success ? 'done' : 'error');
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
			case 'incomplete': {
				// El backend cierra la ejecución avisando de que no todos los pasos
				// produjeron trabajo (paso cortado por la guarda, artefacto que no
				// apareció, recuperación agotada). Es un AVISO: el `done` sin meta
				// es lo que cierra el stream. Este evento no tenía case en NINGÚN
				// switch, así que el usuario no veía nada.
				agencyIncomplete = String(valueObj.reason ?? 'Plan incompleto');
				if (typeof valueObj.agent === 'string' || agentName) {
					setPlanStepStatus(valueObj, String(valueObj.agent ?? agentName), 'error');
				}
				break;
			}
			case 'tool_denied': {
				// La whitelist del agente (`tool_policy`, backend) le negó una
				// herramienta: el paso NO la ejecutó. Se marca en la tarjeta del
				// agente que la pidió y queda el registro para el panel.
				const deniedAgent = String(valueObj.agent ?? agentName ?? 'agente');
				const deniedTool = String(valueObj.name ?? 'herramienta');

				agencyDenials = [
					...agencyDenials,
					{ agent: deniedAgent, name: deniedTool, reason: String(valueObj.reason ?? '') }
				];
				const deniedIndex = agencyRuns.findIndex(
					(run) => run.agent === deniedAgent && run.status === 'running'
				);

				if (deniedIndex >= 0) {
					const runs = [...agencyRuns];

					runs[deniedIndex] = {
						...runs[deniedIndex],
						denied: [...(runs[deniedIndex].denied ?? []), deniedTool]
					};
					agencyRuns = runs;
				}
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
				// El panel antiguo rellenaba la respuesta con el contenido del
				// `done` cuando no había llegado texto. El chat recibe los `text`,
				// pero el panel no debe quedarse vacío si el backend solo manda el
				// resultado final.
				if (event.type === 'done' && !response) {
					const finalValue = valueObj.result ?? valueObj;

					if (typeof finalValue === 'string') response = finalValue;
					else if (finalValue && typeof finalValue === 'object') {
						const content = (finalValue as { content?: unknown }).content;

						if (typeof content === 'string') response = content;
					}
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

	/**
	 * El stream del job NO se abre aquí.
	 *
	 * El panel era un segundo dueño del `EventSource`: abría su propia conexión
	 * con su propia lista de tipos (sin `usage`, sin `incomplete`, sin
	 * `tool_denied`), su propia reconexión sin reanudar por índice y su propio
	 * lanzamiento (`agent_execute`) que nadie usaba desde la UI. El mismo job se
	 * veía distinto según quién escuchara y, al pasar el relevo al chat, había
	 * que cerrar la conexión del panel y reconstruir la timeline desde cero.
	 *
	 * Ahora el dueño es el chat que lanza la tarea
	 * (`chatStore.consumeAgencyJob`, sobre `$lib/utils/agency-stream`) y cada
	 * frame llega aquí por `applyChatAgencyEvent`, el reductor único.
	 */

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
			}>(
				config.endpoint,
				conversationId
					? `/agent_status?conversation_id=${encodeURIComponent(conversationId)}`
					: '/agent_status'
			);
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
				conversationId
					? `/agent_plan?conversation_id=${encodeURIComponent(conversationId)}`
					: '/agent_plan'
			);
			if (result && typeof result === 'object') {
				// El plan VIVO es `agencyPlan` (lo alimenta el reductor con los
				// eventos del stream). Este endpoint es de solo lectura: devuelve el
				// plan persistido sin pisar la timeline en curso.
				lastError = null;
				return result.active_plan ?? null;
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
		const autoAgents = config.selectionMode !== 'manual' ? agencyAgents.slice(0, 12) : [];
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
		get agencyGoal() {
			return agencyGoal;
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
		get agencyIncomplete() {
			return agencyIncomplete;
		},
		get agencyDenials() {
			return agencyDenials;
		},
		get config() {
			return config;
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
		fetchCheckpoints,
		fetchDaemonTasks,
		fetchAgencyAgents,
		fetchAgentDetail,
		createAgent,
		saveAgent,
		deleteAgent,
		fetchSensors,
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
