/**
 * ScheduledChatsService - publica en el historial nativo los chats que el
 * backend generó mientras la WebUI estaba cerrada.
 *
 * El scheduler de tareas programadas corre en el proceso `dev_agent` (no
 * depende del daemon ni de que esta ventana esté abierta) y guarda cada
 * ejecución como una conversación completa. El historial de chat vive en
 * IndexedDB del cliente, así que ese traslado solo puede hacerlo la WebUI:
 * este servicio consulta el backend y las importa como conversaciones
 * normales, de forma que aparecen en el historial y se abren como cualquier
 * otro chat.
 *
 * La importación es idempotente por dos vías: el backend usa ids deterministas
 * y aquí se recuerda qué ids ya se intentaron.
 */

import { browser } from '$app/environment';
import { apiFetch } from '$lib/api';
import { conversationsStore } from '$lib/stores/conversations/index.svelte';
import type { DatabaseConversation, DatabaseMessage } from '$lib/types/database';

export interface ScheduledChatSummary {
	created_at?: string;
	description?: string;
	duration_ms?: number;
	error?: string | null;
	id: string;
	message_count?: number;
	name?: string;
	status?: string;
	task_id?: string;
}

export interface SchedulerStatus {
	busy?: boolean;
	chats_dir?: string;
	due_task_ids?: string[];
	interval_seconds?: number;
	last_run?: {
		chat_id?: string;
		description?: string;
		duration_ms?: number;
		finished_at?: string;
		status?: string;
		task_id?: string;
	} | null;
	llm_url?: string;
	model_ready?: boolean;
	running?: boolean;
	task_count?: number;
	upcoming?: { description?: string; id?: string; next_execution?: string } | null;
	waiting_for_model?: { at?: string; reason?: string; task_ids?: string[] } | null;
}

export interface ScheduledChatDetail {
	id: string;
	conv: DatabaseConversation;
	messages: DatabaseMessage[];
}

export type ScheduledChatSession = { conv: DatabaseConversation; messages: DatabaseMessage[] };

/** Descarga el detalle de un chat concreto. Separado para poder testearlo. */
export async function fetchScheduledChatDetail(chatId: string): Promise<ScheduledChatDetail | null> {
	try {
		const response = await apiFetch('agent', `/daemon_chats/${encodeURIComponent(chatId)}`);
		if (!response.ok) return null;

		const payload = (await response.json()) as { chat?: ScheduledChatDetail };
		const chat = payload.chat;

		if (!chat?.conv?.id || !Array.isArray(chat.messages)) return null;

		return chat;
	} catch {
		return null;
	}
}

/**
 * Convierte los resúmenes del backend en sesiones importables.
 *
 * Lógica pura (sin estado): descarta lo ya visto y lo que no se pudo
 * descargar, para que un chat corrupto no bloquee al resto.
 */
export async function collectScheduledChats(
	summaries: ScheduledChatSummary[],
	seen: ReadonlySet<string>,
	fetchDetail: (chatId: string) => Promise<ScheduledChatDetail | null> = fetchScheduledChatDetail
): Promise<{ ids: string[]; records: ScheduledChatSession[] }> {
	const ids: string[] = [];
	const records: ScheduledChatSession[] = [];

	for (const summary of summaries) {
		if (!summary?.id || seen.has(summary.id)) continue;

		const chat = await fetchDetail(summary.id);

		if (!chat) continue;

		records.push({ conv: chat.conv, messages: chat.messages });
		ids.push(summary.id);
	}

	return { ids, records };
}

const SEEN_STORAGE_KEY = 'scheduledChatsImported';
const SEEN_LIMIT = 400;
const SYNC_INTERVAL_MS = 30_000;

function loadSeen(): Set<string> {
	try {
		const raw = JSON.parse(localStorage.getItem(SEEN_STORAGE_KEY) ?? '[]');
		return new Set(Array.isArray(raw) ? raw.filter((id): id is string => typeof id === 'string') : []);
	} catch {
		return new Set();
	}
}

function saveSeen(seen: Set<string>): void {
	try {
		const ids = [...seen].slice(-SEEN_LIMIT);
		localStorage.setItem(SEEN_STORAGE_KEY, JSON.stringify(ids));
	} catch {
		// Sin localStorage el import sigue siendo idempotente por id de conversación.
	}
}

let timer: ReturnType<typeof setInterval> | undefined;
let syncing = false;

export class ScheduledChatsService {
	/**
	 * Importa las conversaciones nuevas generadas por tareas programadas.
	 * @returns cuántas conversaciones se incorporaron al historial
	 */
	static async sync(): Promise<number> {
		if (!browser || syncing) return 0;

		syncing = true;
		try {
			// `apiFetch` resuelve host/modo y añade el token (api.ts).
			const response = await apiFetch('agent', '/daemon_chats?limit=50');
			if (!response.ok) return 0;

			const data = (await response.json()) as { chats?: ScheduledChatSummary[] };
			const seen = loadSeen();
			const { ids, records } = await collectScheduledChats(data.chats ?? [], seen);

			if (!records.length) return 0;

			const { imported } = await conversationsStore.importConversationsData(records);
			saveSeen(new Set([...seen, ...ids]));

			console.info(`[Tareas programadas] ${imported.length} chat(s) importado(s) al historial`);
			return imported.length;
		} catch {
			return 0;
		} finally {
			syncing = false;
		}
	}

	/** Estado del scheduler del backend (activo, modelo listo, próxima tarea). */
	static async fetchStatus(): Promise<SchedulerStatus | null> {
		if (!browser) return null;
		try {
			const response = await apiFetch('agent', '/scheduler/status');
			if (!response.ok) return null;
			return (await response.json()) as SchedulerStatus;
		} catch {
			return null;
		}
	}

	/** Sincroniza ahora y luego cada `SYNC_INTERVAL_MS`. */
	static startAutoSync(): void {
		if (!browser || timer) return;

		void this.sync();
		timer = setInterval(() => void this.sync(), SYNC_INTERVAL_MS);
	}

	static stopAutoSync(): void {
		if (timer) {
			clearInterval(timer);
			timer = undefined;
		}
	}
}
