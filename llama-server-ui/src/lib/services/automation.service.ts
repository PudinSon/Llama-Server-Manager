/**
 * Cliente de la API del **servicio de automatización** (`tools/automation_service.py`).
 *
 * El servicio escucha en el puerto 15001 y es el que ejecuta los dispatchers, la
 * cola y las aprobaciones. La UI solo consulta y configura; no ejecuta nada.
 *
 * El proceso lo arranca el **Manager** (`tools/automation_process.py`) y muere
 * con él: sin manager no hay modelo cargado y los dispatchers no tendrían nada
 * que ejecutar. Corre aparte de la web view, así que cerrar la UI no lo detiene;
 * si aquí no responde, el Manager no está corriendo o el servicio está
 * desactivado en su configuración avanzada.
 *
 * La resolución de la URL la hace `serviceUrl('automation', ...)`, así que
 * funciona igual en modo directo (otro puerto del mismo host) que detrás de un
 * proxy. El servicio responde con cabeceras CORS, de modo que el fetch
 * cross-origin desde el WebUI de llama.cpp funciona sin proxy.
 */
import { apiJson, serviceUrl } from '$lib/api';

export type AutomationTaskStatus =
	| 'pending'
	| 'running'
	| 'awaiting_approval'
	| 'done'
	| 'failed'
	| 'cancelled'
	| string;

export interface AutomationTask {
	id: number;
	source: string;
	source_ref?: string | null;
	wake_prompt?: string | null;
	agent_template?: string | null;
	priority: number;
	/** Intentos ya consumidos (0 = aún no se ha reintentado). */
	attempts: number;
	/** Momento (epoch) a partir del cual se puede reclamar; null = ya. */
	next_attempt_at?: number | null;
	status: AutomationTaskStatus;
	worker_id?: string | null;
	created_at: number;
	started_at?: number | null;
	finished_at?: number | null;
	result?: unknown;
	error?: string | null;
	approval_token?: string | null;
}

export interface AutomationQueueStats {
	pending: number;
	running: number;
	awaiting_approval: number;
	done: number;
	failed: number;
	cancelled: number;
	total?: number;
}

export interface AutomationDispatcherStatus {
	running: boolean;
	/** Pausa del **ejecutor** (los workers no reclaman tareas nuevas). */
	paused?: boolean;
	paused_at?: number | null;
	paused_reason?: string;
	backlog?: {
		ready: number;
		waiting_backoff: number;
		awaiting_approval: number;
		total: number;
	};
	worker_id: string;
	num_workers: number;
	require_approval: boolean;
	current?: { id: number; source: string } | null;
	last_result?: { id: number; status: string; error?: string | null } | null;
	processed: number;
	failed: number;
	retried: number;
	max_retries: number;
	retry_base_seconds: number;
	poll_interval: number;
	queue: AutomationQueueStats;
	workspace?: string;
	llm_url?: string;
}

export interface AutomationImapStatus {
	enabled?: boolean;
	connected?: boolean;
	folder?: string;
	last_poll?: { at?: number; found?: number; enqueued?: number } | null;
	last_error?: string | null;
	[key: string]: unknown;
}

export interface AutomationImapConfig {
	enabled: boolean;
	host: string;
	port: number;
	user: string;
	has_password: boolean;
	use_ssl: boolean;
	folder: string;
	search: string;
	poll_interval: number;
	mark_seen: boolean;
	max_per_poll: number;
	wake_prompt: string;
	agent_template: string;
	configured: boolean;
	running: boolean;
}

export interface AutomationImapConfigInput {
	enabled: boolean;
	host: string;
	port: number;
	user: string;
	password?: string;
	use_ssl: boolean;
	folder: string;
	search: string;
	poll_interval: number;
	mark_seen: boolean;
	max_per_poll: number;
	wake_prompt: string;
	agent_template: string;
}

export interface AutomationGmailConfig {
	enabled: boolean;
	connected: boolean;
	address: string;
	client_id: string;
	has_client_secret: boolean;
	redirect_uri: string;
	search: string;
	poll_interval: number;
	mark_seen: boolean;
	max_per_poll: number;
	wake_prompt: string;
	agent_template: string;
	running?: boolean;
	last_error?: string;
	last_poll?: { at?: number; found?: number; enqueued?: number } | null;
}

export interface AutomationGmailConfigInput {
	enabled: boolean;
	search: string;
	poll_interval: number;
	mark_seen: boolean;
	max_per_poll: number;
	wake_prompt: string;
	agent_template: string;
}

export interface AutomationWebhook {
	name: string;
	enabled: boolean;
	source: string;
	/** El token no se devuelve nunca; solo si existe. */
	has_token: boolean;
	allow_anonymous: boolean;
	requires_approval: boolean | null;
	wake_prompt?: string | null;
	agent_template?: string | null;
	priority?: number;
	dedupe?: boolean;
}

export interface AutomationWebhooksStatus {
	total: number;
	active: number;
	enqueued_total: number;
	rejected_total: number;
	last_event?: { at: number; webhook: string; task_id: number; remote_addr?: string } | null;
	webhooks: AutomationWebhook[];
}

export interface AutomationStatus {
	dispatcher: AutomationDispatcherStatus;
	imap: AutomationImapStatus;
	gmail: AutomationGmailConfig;
	webhooks?: AutomationWebhooksStatus;
}

export interface AutomationAuditEntry {
	id: number;
	at: number;
	action: string;
	task_id?: number | null;
	actor?: string | null;
	detail?: unknown;
}

export interface NewAutomationTask {
	source?: string;
	source_ref?: string;
	wake_prompt?: string;
	agent_template?: string;
	priority?: number;
	requires_approval?: boolean;
	payload?: unknown;
}

/** Estado del dispatcher + listener IMAP. */
export function getAutomationStatus(): Promise<AutomationStatus> {
	return apiJson<AutomationStatus>('automation', '/automation/status');
}

export function getAutomationImapConfig(): Promise<{ config: AutomationImapConfig }> {
	return apiJson<{ config: AutomationImapConfig }>('automation', '/automation/imap/config');
}

export function saveAutomationImapConfig(
	input: AutomationImapConfigInput
): Promise<{ config: AutomationImapConfig }> {
	return apiJson<{ config: AutomationImapConfig }>('automation', '/automation/imap/config', {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(input)
	});
}

export function testAutomationImapConnection(): Promise<{
	success: boolean;
	folder?: string;
	error?: string;
}> {
	return apiJson('automation', '/automation/imap/test', { method: 'POST' });
}

export function getAutomationGmailConfig(): Promise<{ config: AutomationGmailConfig }> {
	return apiJson<{ config: AutomationGmailConfig }>('automation', '/automation/gmail/config');
}

export function saveAutomationGmailConfig(
	input: AutomationGmailConfigInput
): Promise<{ config: AutomationGmailConfig }> {
	return apiJson<{ config: AutomationGmailConfig }>('automation', '/automation/gmail/config', {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(input)
	});
}

export function beginGoogleMailLink(input: {
	client_id: string;
	client_secret?: string;
}): Promise<{ authorization_url: string; redirect_uri: string }> {
	return apiJson<{ authorization_url: string; redirect_uri: string }>(
		'automation',
		'/automation/gmail/oauth/start',
		{
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(input)
		}
	);
}

export function disconnectGoogleMail(): Promise<{ config: AutomationGmailConfig }> {
	return apiJson<{ config: AutomationGmailConfig }>('automation', '/automation/gmail/disconnect', {
		method: 'POST'
	});
}

export function pollAutomationGmail(): Promise<{ found: number; enqueued: number }> {
	return apiJson<{ found: number; enqueued: number }>('automation', '/automation/gmail/poll', {
		method: 'POST'
	});
}

/** Tareas de la cola (filtros opcionales). */
export async function listAutomationTasks(
	options: {
		status?: string;
		source?: string;
		limit?: number;
		offset?: number;
	} = {}
): Promise<{ tasks: AutomationTask[]; stats: AutomationQueueStats }> {
	const params = new URLSearchParams();
	if (options.status) params.set('status', options.status);
	if (options.source) params.set('source', options.source);
	params.set('limit', String(options.limit ?? 100));
	params.set('offset', String(options.offset ?? 0));
	return apiJson<{ tasks: AutomationTask[]; stats: AutomationQueueStats }>(
		'automation',
		`/automation/tasks?${params.toString()}`
	);
}

export function getAutomationTask(taskId: number): Promise<{ task: AutomationTask }> {
	return apiJson<{ task: AutomationTask }>('automation', `/automation/tasks/${taskId}`);
}

/** Encola una tarea nueva (también sirve para probar un dispatcher). */
export function enqueueAutomationTask(
	task: NewAutomationTask
): Promise<{ task_id: number; task: AutomationTask }> {
	return apiJson<{ task_id: number; task: AutomationTask }>('automation', '/automation/tasks', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(task)
	});
}

/** Ejecuta un ciclo de reparto inmediato (una tarea). */
export function dispatchAutomationOnce(): Promise<{ task: AutomationTask | null }> {
	return apiJson<{ task: AutomationTask | null }>('automation', '/automation/dispatch', {
		method: 'POST'
	});
}

/** Devuelve a la cola una tarea fallida o cancelada. */
export function retryAutomationTask(
	taskId: number,
	delaySeconds = 0
): Promise<{ success: boolean; task: AutomationTask | null }> {
	return apiJson<{ success: boolean; task: AutomationTask | null }>(
		'automation',
		`/automation/retry/${taskId}`,
		{
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ delay_seconds: delaySeconds })
		}
	);
}

export function approveAutomationTask(token: string): Promise<{ task_id: number }> {
	return apiJson<{ task_id: number }>(
		'automation',
		`/automation/approve/${encodeURIComponent(token)}`,
		{
			method: 'POST'
		}
	);
}

export function rejectAutomationTask(token: string, reason = ''): Promise<{ task_id: number }> {
	return apiJson<{ task_id: number }>(
		'automation',
		`/automation/reject/${encodeURIComponent(token)}`,
		{
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ reason })
		}
	);
}

/** Historial de auditoría (encolar, aprobar, fallar, reintentar…). */
export function listAutomationAudit(options: { limit?: number; taskId?: number } = {}): Promise<{
	entries: AutomationAuditEntry[];
}> {
	const params = new URLSearchParams();
	params.set('limit', String(options.limit ?? 100));
	if (options.taskId != null) params.set('task_id', String(options.taskId));
	return apiJson<{ entries: AutomationAuditEntry[] }>('automation', `/automation/audit?${params}`);
}

export function getAutomationImapStatus(): Promise<AutomationImapStatus> {
	return apiJson<AutomationImapStatus>('automation', '/automation/imap/status');
}

/** Fuerza un sondeo del buzón ahora mismo. */
export function pollAutomationImapNow(): Promise<Record<string, unknown>> {
	return apiJson<Record<string, unknown>>('automation', '/automation/imap/poll', {
		method: 'POST'
	});
}

// ---------------------------------------------------------------- webhooks

/** Registro de webhooks (los secretos nunca llegan al cliente). */
export function listAutomationWebhooks(): Promise<AutomationWebhooksStatus> {
	return apiJson<AutomationWebhooksStatus>('automation', '/automation/webhooks');
}

export interface WebhookInput {
	name: string;
	enabled?: boolean;
	wake_prompt?: string;
	agent_template?: string;
	requires_approval?: boolean;
	allow_anonymous?: boolean;
	priority?: number;
	dedupe?: boolean;
}

/**
 * Crea o actualiza un webhook (`name` existente = actualizar).
 *
 * `token` solo llega en la respuesta de **creación**: el secreto se entrega una
 * única vez y después solo se puede recuperar rotándolo.
 */
export function saveAutomationWebhook(input: WebhookInput): Promise<{
	webhook: AutomationWebhook;
	url: string;
	token?: string;
}> {
	return apiJson<{ webhook: AutomationWebhook; url: string; token?: string }>(
		'automation',
		'/automation/webhooks',
		{
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(input)
		}
	);
}

export function deleteAutomationWebhook(name: string): Promise<{ deleted: string }> {
	return apiJson<{ deleted: string }>(
		'automation',
		`/automation/webhooks/${encodeURIComponent(name)}`,
		{ method: 'DELETE' }
	);
}

/** Rota el token. El nuevo valor solo se devuelve en esta respuesta. */
export function rotateAutomationWebhookToken(
	name: string
): Promise<{ name: string; token: string }> {
	return apiJson<{ name: string; token: string }>(
		'automation',
		`/automation/webhooks/${encodeURIComponent(name)}/token`,
		{ method: 'POST' }
	);
}

/** URL absoluta que debe llamar el sistema externo (para copiar/pegar). */
export function webhookIngestUrl(name: string): string {
	return serviceUrl('automation', `/webhook/${encodeURIComponent(name)}`);
}

// ------------------------------------------- FileWatch y Cron (mismo CRUD)

export interface AutomationFileWatch {
	name: string;
	enabled: boolean;
	/** Carpeta vigilada (absoluta o relativa al workspace). */
	path: string;
	pattern: string;
	ignore?: string;
	recursive: boolean;
	event: 'created' | 'modified' | 'any' | string;
	stable_seconds: number;
	max_per_poll: number;
	source: string;
	wake_prompt?: string | null;
	agent_template?: string | null;
	requires_approval: boolean | null;
	priority: number;
	dedupe: boolean;
	last_run?: number | null;
	last_status?: string | null;
}

export interface AutomationCron {
	name: string;
	enabled: boolean;
	/** Expresión cron de 5 campos (`0 8 * * *`) o vacía si se usa el intervalo. */
	schedule: string;
	every_seconds: number;
	source: string;
	wake_prompt?: string | null;
	agent_template?: string | null;
	requires_approval: boolean | null;
	priority: number;
	dedupe: boolean;
	last_run?: number | null;
	last_status?: string | null;
	/** Calculado por el servicio, en formato `YYYY-MM-DD HH:MM`. */
	next_run?: string | null;
}

export interface RegistryListing<T> {
	total: number;
	active: number;
	enqueued_total: number;
	running?: boolean;
	tick_seconds?: number;
	items: T[];
}

type RegistryKind = 'filewatches' | 'crons';

const REGISTRY_KEYS: Record<RegistryKind, string> = { filewatches: 'filewatches', crons: 'crons' };

function registryJson<T>(kind: RegistryKind, suffix = '', init: RequestInit = {}): Promise<T> {
	return apiJson<T>('automation', `/automation/${kind}${suffix}`, init);
}

function jsonBody(payload: unknown): RequestInit {
	return {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(payload)
	};
}

async function listRegistry<T extends { name: string }>(
	kind: RegistryKind
): Promise<RegistryListing<T>> {
	const raw = await registryJson<Record<string, unknown>>(kind);
	return {
		total: Number(raw.total ?? 0),
		active: Number(raw.active ?? 0),
		enqueued_total: Number(raw.enqueued_total ?? 0),
		running: raw.running as boolean | undefined,
		tick_seconds: raw.tick_seconds as number | undefined,
		items: (raw[REGISTRY_KEYS[kind]] ?? []) as T[]
	};
}

function saveRegistryEntry<T>(kind: RegistryKind, input: { name: string } & object): Promise<T> {
	return registryJson<T>(kind, '', jsonBody(input));
}

function deleteRegistryEntry(kind: RegistryKind, name: string): Promise<{ deleted: string }> {
	return registryJson<{ deleted: string }>(kind, `/${encodeURIComponent(name)}`, {
		method: 'DELETE'
	});
}

export type FileWatchInput = Omit<Partial<AutomationFileWatch>, 'last_run' | 'last_status'> & {
	name: string;
};
export type CronInput = Omit<Partial<AutomationCron>, 'last_run' | 'last_status' | 'next_run'> & {
	name: string;
};

export function listFileWatches(): Promise<RegistryListing<AutomationFileWatch>> {
	return listRegistry<AutomationFileWatch>('filewatches');
}

export function saveFileWatch(input: FileWatchInput): Promise<{ item: AutomationFileWatch }> {
	return saveRegistryEntry<{ item: AutomationFileWatch }>('filewatches', input);
}

export function deleteFileWatch(name: string): Promise<{ deleted: string }> {
	return deleteRegistryEntry('filewatches', name);
}

/** Fuerza un escaneo de la carpeta ahora mismo. */
export function scanFileWatch(name: string): Promise<{ enqueued: number }> {
	return registryJson<{ enqueued: number }>('filewatches', `/${encodeURIComponent(name)}/scan`, {
		method: 'POST'
	});
}

export function listCrons(): Promise<RegistryListing<AutomationCron>> {
	return listRegistry<AutomationCron>('crons');
}

export function saveCron(input: CronInput): Promise<{ item: AutomationCron }> {
	return saveRegistryEntry<{ item: AutomationCron }>('crons', input);
}

export function deleteCron(name: string): Promise<{ deleted: string }> {
	return deleteRegistryEntry('crons', name);
}

/** Dispara una rutina ahora mismo, sin esperar al horario. */
export function runCron(name: string): Promise<{ task_id: number }> {
	return registryJson<{ task_id: number }>('crons', `/${encodeURIComponent(name)}/run`, {
		method: 'POST'
	});
}

// ------------------------------------------------ Panel unificado (pausa)

/** Los cuatro dispatchers (fuentes de eventos) del servicio. */
export type DispatcherKind = 'webhook' | 'filewatch' | 'cron' | 'imap' | string;

export interface AutomationDispatcherCard {
	kind: DispatcherKind;
	label: string;
	/** Pausa global persistente: detiene la generación automática de tareas. */
	paused: boolean;
	at?: number | null;
	reason?: string;
	running: boolean;
	configured: boolean;
	total: number;
	enqueued_total: number;
	rejected_total: number;
	last_event?: { at?: number; task_id?: number } | null;
	status: Record<string, unknown>;
}

export interface AutomationHistoryEntry {
	id: number;
	at: number;
	action: string;
	task_id?: number | null;
	actor?: string | null;
	/** Tipo de dispatcher deducido del actor (`cron:diario` → `cron`). */
	kind?: string;
	detail?: unknown;
}

/** Estado, contadores y pausa de los cuatro dispatchers. */
export function listDispatchers(): Promise<{
	dispatchers: AutomationDispatcherCard[];
}> {
	return apiJson<{ dispatchers: AutomationDispatcherCard[] }>(
		'automation',
		'/automation/dispatchers'
	);
}

/** Pausa un dispatcher (estado persistente: sobrevive a un reinicio). */
export function pauseDispatcher(
	kind: DispatcherKind,
	reason = ''
): Promise<{ dispatcher: AutomationDispatcherCard }> {
	return apiJson<{ dispatcher: AutomationDispatcherCard }>(
		'automation',
		`/automation/dispatchers/${encodeURIComponent(kind)}/pause`,
		{
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ reason })
		}
	);
}

export function resumeDispatcher(
	kind: DispatcherKind
): Promise<{ dispatcher: AutomationDispatcherCard }> {
	return apiJson<{ dispatcher: AutomationDispatcherCard }>(
		'automation',
		`/automation/dispatchers/${encodeURIComponent(kind)}/resume`,
		{ method: 'POST' }
	);
}

/**
 * Estado del **ejecutor** de la cola (el pool de workers).
 *
 * `backlog` es lo que se queda represado al pausarlo: las listas para reclamar,
 * las que esperan su backoff de reintento y las que esperan aprobación humana.
 */
export interface AutomationExecutorStatus {
	paused: boolean;
	paused_at?: number | null;
	paused_reason?: string;
	running: boolean;
	worker_id?: string;
	num_workers: number;
	current?: { id: number; source: string } | null;
	processed: number;
	failed: number;
	retried: number;
	max_retries: number;
	backlog: {
		ready: number;
		waiting_backoff: number;
		awaiting_approval: number;
		total: number;
	};
	queue: AutomationQueueStats;
}

export function getExecutorStatus(): Promise<{ executor: AutomationExecutorStatus }> {
	return apiJson<{ executor: AutomationExecutorStatus }>('automation', '/automation/executor');
}

/** Pausa el ejecutor: los workers terminan lo que tienen y dejan de reclamar. */
export function pauseExecutor(reason = ''): Promise<{ executor: AutomationExecutorStatus }> {
	return apiJson<{ executor: AutomationExecutorStatus }>(
		'automation',
		'/automation/executor/pause',
		{
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ reason })
		}
	);
}

/** Reanuda el ejecutor y saca el backlog al instante (despierta a los workers). */
export function resumeExecutor(): Promise<{ executor: AutomationExecutorStatus }> {
	return apiJson<{ executor: AutomationExecutorStatus }>(
		'automation',
		'/automation/executor/resume',
		{
			method: 'POST'
		}
	);
}

/** Historial unificado de eventos generados por los dispatchers. */
export function listDispatcherHistory(options: { kind?: string; limit?: number } = {}): Promise<{
	entries: AutomationHistoryEntry[];
}> {
	const params = new URLSearchParams();
	params.set('limit', String(options.limit ?? 100));
	if (options.kind) params.set('kind', options.kind);
	return apiJson<{ entries: AutomationHistoryEntry[] }>(
		'automation',
		`/automation/dispatchers/history?${params}`
	);
}
