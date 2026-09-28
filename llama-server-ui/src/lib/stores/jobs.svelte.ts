/**
 * Consola = visor de salida + jobs (no hay TTY interactiva).
 *
 * Habla con el servidor de agente (`tools/dev_agent.py`, puerto 15000):
 *   GET  /api/jobs                 → lista
 *   POST /api/jobs/start           → arranca {command, shell, cwd, venv, label}
 *   GET  /api/jobs/<id>/output     → últimas líneas
 *   POST /api/jobs/<id>/kill       → detiene el árbol de procesos
 *   GET  /api/jobs/<id>/stream     → SSE con las líneas nuevas
 */
import { browser } from '$app/environment';
import { derived, get, writable } from 'svelte/store';
import { serviceUrl } from '$lib/api';
import { toast } from 'svelte-sonner';

const API = `${serviceUrl('agent', '/api/jobs')}`;

export type JobStatus = 'starting' | 'running' | 'exited' | 'error' | 'killed';

export interface JobInfo {
	command: string;
	cwd: string;
	elapsed: number;
	ended_at: string | null;
	exit_code: number | null;
	job_id: string;
	label: string;
	log_path: string;
	pid: number | null;
	shell: string;
	started_at: string;
	status: JobStatus;
	error?: string | null;
}

export interface JobsState {
	error: string | null;
	jobs: JobInfo[];
	loading: boolean;
	output: string[];
	outputLines: number;
	selectedId: string | null;
	streaming: boolean;
}

export interface StartJobOptions {
	cwd?: string;
	label?: string;
	shell?: string;
	venv?: string;
}

let eventSource: EventSource | null = null;

function createJobsStore() {
	const { subscribe, set, update } = writable<JobsState>({
		error: null,
		jobs: [],
		loading: false,
		output: [],
		outputLines: 0,
		selectedId: null,
		streaming: false
	});

	async function loadOutput(jobId: string, tail = 500): Promise<void> {
		if (!browser || !jobId) return;
		try {
			const res = await fetch(`${API}/${jobId}/output?tail=${tail}`);
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			const data = await res.json();
			update(s => ({
				...s,
				output: data.lines || [],
				outputLines: data.total_lines ?? (data.lines || []).length
			}));
		} catch (err) {
			update(s => ({ ...s, error: err instanceof Error ? err.message : 'Error' }));
		}
	}

	function stopStream(): void {
		if (eventSource) {
			eventSource.close();
			eventSource = null;
		}
		update(s => ({ ...s, streaming: false }));
	}

	function startStream(jobId: string): void {
		if (!browser || !jobId) return;
		stopStream();
		try {
			const source = new EventSource(`${API}/${jobId}/stream`);
			eventSource = source;
			update(s => ({ ...s, streaming: true }));
			source.onmessage = event => {
				try {
					const payload = JSON.parse(event.data);
					if (typeof payload.line !== 'string') return;
					update(s => ({ ...s, output: [...s.output, payload.line as string] }));
				} catch {
					// línea no JSON: se ignora
				}
			};
			source.addEventListener('end', () => {
				stopStream();
				void loadJobs();
			});
			source.onerror = () => stopStream();
		} catch {
			update(s => ({ ...s, streaming: false }));
		}
	}

	async function loadJobs(): Promise<void> {
		if (!browser) return;
		update(s => ({ ...s, loading: true }));
		try {
			const res = await fetch(API);
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			const data = await res.json();
			const jobs: JobInfo[] = data.jobs || [];
			update(s => ({
				...s,
				error: null,
				jobs: jobs.slice().reverse(),
				loading: false,
				selectedId: s.selectedId ?? jobs.at(-1)?.job_id ?? null
			}));
		} catch (err) {
			const msg = err instanceof Error ? err.message : 'Error';
			update(s => ({ ...s, error: msg, loading: false }));
		}
	}

	return {
		subscribe,

		loadJobs,
		loadOutput,
		startStream,
		stopStream,

		async startJob(command: string, options: StartJobOptions = {}): Promise<string | null> {
			if (!browser || !command.trim()) return null;
			try {
				const res = await fetch(`${API}/start`, {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ command, ...options })
				});
				if (!res.ok) {
					const body = await res.json().catch(() => ({}));
					throw new Error(body.error || `HTTP ${res.status}`);
				}
				const data = await res.json();
				const jobId: string = data.job_id;
				update(s => ({ ...s, selectedId: jobId }));
				await loadJobs();
				void loadOutput(jobId);
				toast.success(`Job lanzado (${jobId})`);
				return jobId;
			} catch (err) {
				toast.error(`No se pudo lanzar: ${err instanceof Error ? err.message : 'Error'}`);
				return null;
			}
		},

		async killJob(jobId: string): Promise<void> {
			if (!browser || !jobId) return;
			try {
				const res = await fetch(`${API}/${jobId}/kill`, { method: 'POST' });
				if (!res.ok) throw new Error(`HTTP ${res.status}`);
				toast.success('Job detenido');
				await loadJobs();
			} catch (err) {
				toast.error(`No se pudo detener: ${err instanceof Error ? err.message : 'Error'}`);
			}
		},

		async selectJob(jobId: string): Promise<void> {
			update(s => ({ ...s, selectedId: jobId }));
			await loadOutput(jobId);
		},

		async refreshSelected(): Promise<void> {
			const current = get(jobsStore).selectedId;
			await loadJobs();
			if (current) await loadOutput(current);
		},

		/**
		 * Limpia de verdad: pide al backend que borre del registro (memoria y
		 * disco) los jobs terminados y recarga la lista. El filtrado local solo no
		 * servía de nada: `list()` mezcla los metadatos en disco, así que la lista
		 * se repoblaba al instante.
		 */
		async clearFinished(): Promise<void> {
			stopStream();
			try {
				const res = await fetch(`${API}/clear`, {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ include_failed: true })
				});
				if (!res.ok) throw new Error(`HTTP ${res.status}`);
			} catch (err) {
				toast.error(`No se pudo limpiar: ${err instanceof Error ? err.message : 'Error'}`);
			}
			update((s) => ({ ...s, output: [], outputLines: 0, selectedId: null }));
			await loadJobs();
			// `loadJobs` reselecciona el último job (o el primero vivo); traemos ya
			// su salida para que el visor no quede vacío.
			const next = get(jobsStore).selectedId;
			if (next) await loadOutput(next);
		}
	};
}

export const jobsStore = createJobsStore();

export const activeJobs = derived(jobsStore, store =>
	store.jobs.filter(job => job.status === 'running' || job.status === 'starting')
);
