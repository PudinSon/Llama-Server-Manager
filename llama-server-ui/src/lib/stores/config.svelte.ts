import { writable, derived } from 'svelte/store';
import { browser } from '$app/environment';
import { serviceBase } from '$lib/api';
import { toast } from 'svelte-sonner';

export interface ConfigState {
	workspacePath: string;
	globalMode: boolean;
	workspaceHistory: string[];
	loading: boolean;
	error: string | null;
}

// Resuelto según el modo activo (puerto directo o ruta relativa tras nginx).
const API = `${serviceBase('config')}/api`;

function createConfigStore() {
	const { subscribe, set, update } = writable<ConfigState>({
		workspacePath: '',
		globalMode: false,
		workspaceHistory: [],
		loading: false,
		error: null,
	});

	return {
		subscribe,

		async fetchConfig() {
			if (!browser) return;
			update(s => ({ ...s, loading: true }));
			try {
				const res = await fetch(`${API}/config`);
				if (!res.ok) throw new Error(`HTTP ${res.status}`);
				const data = await res.json();
				set({
					workspacePath: data.workspace_path || '',
					globalMode: data.global_mode || false,
					workspaceHistory: data.workspace_history || [],
					loading: false,
					error: null,
				});
			} catch (err) {
				const msg = err instanceof Error ? err.message : 'Error';
				update(s => ({ ...s, error: msg, loading: false }));
			}
		},

		async updateWorkspace(path: string) {
			if (!browser) return;
			update(s => ({ ...s, loading: true }));
			try {
				const res = await fetch(`${API}/config`, {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ workspace_path: path }),
				});
				if (!res.ok) throw new Error(`HTTP ${res.status}`);
				const data = await res.json();
				const config = data.config || data;
				set({
					workspacePath: config.workspace_path || '',
					globalMode: config.global_mode || false,
					workspaceHistory: config.workspace_history || [],
					loading: false,
					error: null,
				});
				toast.success('Workspace actualizado');
			} catch (err) {
				const msg = err instanceof Error ? err.message : 'Error';
				update(s => ({ ...s, error: msg, loading: false }));
				toast.error(`Error: ${msg}`);
			}
		},

		async toggleGlobalMode(enabled: boolean) {
			if (!browser) return;
			update(s => ({ ...s, loading: true }));
			try {
				const res = await fetch(`${API}/config`, {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ global_mode: enabled }),
				});
				if (!res.ok) throw new Error(`HTTP ${res.status}`);
				const data = await res.json();
				const config = data.config || data;
				set({
					workspacePath: config.workspace_path || '',
					globalMode: config.global_mode || false,
					workspaceHistory: config.workspace_history || [],
					loading: false,
					error: null,
				});
				toast.success(`Modo global ${enabled ? 'activado' : 'desactivado'}`);
			} catch (err) {
				const msg = err instanceof Error ? err.message : 'Error';
				update(s => ({ ...s, error: msg, loading: false }));
				toast.error(`Error: ${msg}`);
			}
		},

		/**
		 * Resuelve una carpeta a partir de su nombre usando el backend.
		 * El navegador por seguridad no expone rutas absolutas al elegir una
		 * carpeta, así que enviamos el nombre + un contexto (workspace actual
		 * o padre) y el servidor busca coincidencias.
		 */
		async resolveWorkspaceFolder(folderName: string, basePath: string): Promise<{ path: string } | null> {
			if (!browser) return null;
			try {
				const res = await fetch(`${API}/resolve_workspace`, {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ folder_name: folderName, base_path: basePath }),
				});
				if (!res.ok) {
					const errBody = await res.json().catch(() => ({}));
					toast.error(errBody.error || `No se pudo resolver la carpeta (HTTP ${res.status})`);
					return null;
				}
				const data = await res.json();
				if (!data.path) {
					toast.error(
						`No se encontró la carpeta "${folderName}". Pega la ruta manualmente debajo.`
					);
					return null;
				}
				return { path: data.path };
			} catch (err) {
				toast.error(`Error resolviendo carpeta: ${err instanceof Error ? err.message : 'Error'}`);
				return null;
			}
		},
	};
}

export const configStore = createConfigStore();
export const currentWorkspace = derived(configStore, $store => $store.workspacePath);
export const isGlobalMode = derived(configStore, $store => $store.globalMode);
export const configLoading = derived(configStore, $store => $store.loading);
