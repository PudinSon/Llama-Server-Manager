import { writable, derived } from 'svelte/store';
import { browser } from '$app/environment';
// Todas las llamadas pasan por `api.ts`: resuelve host/modo (localhost, LAN,
// tailscale o nginx) y añade el token si está configurado.
import { apiFetch } from '$lib/api';
import { ScheduledChatsService, type SchedulerStatus } from '$lib/services/scheduled-chats.service';
import { toast } from 'svelte-sonner';

export interface ScheduledTask {
	id: string;
	description: string;
	when: string;
	repeat?: string;
	status: 'pending' | 'completed' | 'failed';
	created_at: string;
	last_executed?: string;
	next_execution: string;
	result?: any;
	/** Conversación que generó el scheduler; permite abrirla desde el modal. */
	last_chat_id?: string;
	last_status?: 'pending' | 'completed' | 'failed';
}

export interface TaskState {
	tasks: ScheduledTask[];
	loading: boolean;
	error: string | null;
	/** Estado del scheduler del backend (corre aunque la webview esté cerrada). */
	scheduler: SchedulerStatus | null;
}

/** Cada cuánto se consultan las tareas (para anunciar las que ya se ejecutaron). */
const POLL_MS = 20000;

function createTaskStore() {
	const { subscribe, set, update } = writable<TaskState>({
		tasks: [],
		loading: false,
		error: null,
		scheduler: null,
	});

	/** Ejecuciones ya anunciadas, con clave `${id}:${last_executed}`. */
	const announced = new Set<string>();
	let timer: ReturnType<typeof setInterval> | undefined;

	/**
	 * Avisa de una tarea ya ejecutada.
	 *
	 * Antes se inyectaba el resultado como mensaje sintético en la conversación
	 * activa; ahora el backend guarda cada ejecución como una conversación propia
	 * y basta con sincronizarla al historial, así que solo queda el aviso.
	 */
	async function announce(task: ScheduledTask): Promise<void> {
		const stamp = `${task.id}:${task.last_executed ?? task.result ?? ''}`;
		if (announced.has(stamp)) return;
		announced.add(stamp);

		const failed = task.status === 'failed';
		if (failed) toast.error(`Tarea programada fallida: ${task.description}`);
		else toast.success(`Tarea programada completada: ${task.description}`);

		// Publica el chat de la ejecución en el historial nativo cuanto antes.
		void ScheduledChatsService.sync();
	}

	/**
	 * Estado del scheduler del backend: si está vivo, si hay modelo cargado y
	 * cuándo toca la próxima tarea. Es la pieza que ejecuta las tareas aunque el
	 * daemon y esta ventana estén parados.
	 */
	async function fetchSchedulerStatus(): Promise<SchedulerStatus | null> {
		if (!browser) return null;
		const scheduler = await ScheduledChatsService.fetchStatus();
		update(s => ({ ...s, scheduler }));
		return scheduler;
	}

	/** Sondeo periódico para detectar tareas ejecutadas con la UI abierta. */
	function startPolling(): void {
		if (!browser || timer) return;
		timer = setInterval(() => void fetchTasks(), POLL_MS);
	}

	function stopPolling(): void {
		if (timer) {
			clearInterval(timer);
			timer = undefined;
		}
	}

	/**
	 * Lee las tareas programadas del daemon y anuncia las que ya se ejecutaron.
	 *
	 * Es una función con nombre (no un método del objeto) para que el sondeo
	 * periódico pueda llamarla sin depender del objeto que se está creando.
	 */
	async function fetchTasks(): Promise<void> {
		if (!browser) return;
		update(s => ({ ...s, loading: true }));
		try {
			const res = await apiFetch('agent', '/daemon_tasks/list');
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			const data = await res.json();
			const tasks: ScheduledTask[] = data.tasks || [];
			update(s => ({
				...s,
				tasks,
				loading: false,
				error: null,
			}));
			void fetchSchedulerStatus();
			// Las tareas ya ejecutadas se anuncian en la conversación activa.
			for (const task of tasks) {
				if (task.status === 'completed' || task.status === 'failed') void announce(task);
			}
		} catch (err) {
			const msg = err instanceof Error ? err.message : 'Error';
			update(s => ({ ...s, error: msg, loading: false }));
			toast.error(`Error cargando tareas: ${msg}`);
		}
	}

	return {
		subscribe,
		fetchTasks,
		fetchSchedulerStatus,

		/** Lanza una tarea programada ahora mismo (sin esperar su hora). */
		async runTaskNow(taskId: string) {
			if (!browser) return false;
			try {
				const res = await apiFetch('agent', `/scheduler/run/${encodeURIComponent(taskId)}`, {
					method: 'POST'
				});
				if (!res.ok) throw new Error(`HTTP ${res.status}`);
				toast.success('Tarea lanzada');
				void fetchSchedulerStatus();
				return true;
			} catch (err) {
				toast.error(
					`No se pudo lanzar: ${err instanceof Error ? err.message : 'error'} (¿modelo cargado?)`
				);
				return false;
			}
		},

		async createTask(description: string, when: string, repeat?: string) {
			if (!browser || !description.trim()) return null;
			try {
				const res = await apiFetch('agent', '/daemon_tasks/create', {
					body: JSON.stringify({ description, repeat, when }),
					headers: { 'Content-Type': 'application/json' },
					method: 'POST'
				});
				if (!res.ok) throw new Error(`HTTP ${res.status}`);
				const data = await res.json();
				update(s => ({
					...s,
					tasks: [...s.tasks, data.task],
				}));
				toast.success(`Tarea programada: ${description}`);
				return data.task;
			} catch (err) {
				toast.error(`Error creando tarea: ${err instanceof Error ? err.message : 'Error'}`);
				return null;
			}
		},

		async deleteTask(taskId: string) {
			if (!browser) return;
			try {
				const res = await apiFetch('agent', `/daemon_tasks/delete/${taskId}`, {
					method: 'DELETE'
				});
				if (!res.ok) throw new Error(`HTTP ${res.status}`);
				update(s => ({
					...s,
					tasks: s.tasks.filter(t => t.id !== taskId),
				}));
				toast.success('Tarea eliminada');
			} catch (err) {
				toast.error(`Error: ${err instanceof Error ? err.message : 'Error'}`);
			}
		},

		async updateTask(taskId: string, updates: Partial<ScheduledTask>) {
			if (!browser) return null;
			try {
				const res = await apiFetch('agent', `/daemon_tasks/update/${taskId}`, {
					body: JSON.stringify(updates),
					headers: { 'Content-Type': 'application/json' },
					method: 'POST'
				});
				if (!res.ok) throw new Error(`HTTP ${res.status}`);
				const data = await res.json();
				update(s => ({
					...s,
					tasks: s.tasks.map(t => t.id === taskId ? data.task : t),
				}));
				toast.success('Tarea actualizada');
				return data.task;
			} catch (err) {
				toast.error(`Error: ${err instanceof Error ? err.message : 'Error'}`);
				return null;
			}
		},

		/** Empieza a consultar tareas periódicamente para anunciar las ejecutadas. */
		startPolling,
		stopPolling
	};
}

export const taskStore = createTaskStore();

export const allTasks = derived(taskStore, $store => $store.tasks);
export const schedulerStatus = derived(taskStore, $store => $store.scheduler);
export const pendingTasks = derived(taskStore, $store => $store.tasks.filter(t => t.status === 'pending'));
export const tasksLoading = derived(taskStore, $store => $store.loading);
