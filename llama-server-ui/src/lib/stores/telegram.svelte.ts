import { writable, derived } from 'svelte/store';
import { browser } from '$app/environment';
import { serviceBase } from '$lib/api';
import { toast } from 'svelte-sonner';

export interface TelegramConfig {
	enabled: boolean;
	token?: string;
	pairing_required: boolean;
	allowed_users: number[];
	status: 'running' | 'stopped' | 'error';
	active_users: number;
}

export interface TelegramState {
	config: TelegramConfig | null;
	loading: boolean;
	error: string | null;
}

const API = `${serviceBase('config')}/api/telegram`;

function createTelegramStore() {
	const { subscribe, set, update } = writable<TelegramState>({
		config: null,
		loading: false,
		error: null,
	});

	return {
		subscribe,

		async fetchConfig() {
			if (!browser) return;
			update(s => ({ ...s, loading: true }));
			try {
				const res = await fetch(API);
				if (!res.ok) {
					throw new Error(`HTTP ${res.status}`);
				}
				const data = await res.json();
				set({
					config: data,
					loading: false,
					error: null,
				});
			} catch (err) {
				const msg = err instanceof Error ? err.message : 'Error de conexión con el Manager';
				update(s => ({ ...s, loading: false, error: msg }));
				toast.error(`Telegram: ${msg}`);
			}
		},

		async enableTelegram(token: string, pairingRequired: boolean = true) {
			if (!browser || !token.trim()) return null;
			update(s => ({ ...s, loading: true }));
			try {
				const res = await fetch(API, {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({
						token,
						pairing_required: pairingRequired,
					}),
				});
				if (!res.ok) throw new Error(`HTTP ${res.status}`);
				const data = await res.json();
				set({
					config: data,
					loading: false,
					error: null,
				});
				toast.success('Telegram habilitado');
				return data;
			} catch (err) {
				const msg = err instanceof Error ? err.message : 'Error';
				update(s => ({ ...s, error: msg, loading: false }));
				toast.error(`Error: ${msg}`);
				return null;
			}
		},

		async disableTelegram() {
			if (!browser) return;
			update(s => ({ ...s, loading: true }));
			try {
				const res = await fetch(API, {
					method: 'DELETE',
				});
				if (!res.ok) throw new Error(`HTTP ${res.status}`);
				set({
					config: null,
					loading: false,
					error: null,
				});
				toast.success('Telegram deshabilitado');
			} catch (err) {
				const msg = err instanceof Error ? err.message : 'Error';
				update(s => ({ ...s, error: msg, loading: false }));
				toast.error(`Error: ${msg}`);
			}
		},

		 async requestPairingCode(code: string) {
			if (!browser) return null;
			try {
				const res = await fetch(`${API}/pairing`, {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ code: code.trim().toUpperCase() }),
				});
				if (!res.ok) throw new Error(`HTTP ${res.status}`);
				const data = await res.json();
				if (data.error) throw new Error(data.error);
				toast.success(`ID ${data.user_id} autorizado`);
				return data.user_id;
			} catch (err) {
				toast.error(`Error: ${err instanceof Error ? err.message : 'Error'}`);
				return null;
			}
		},
	};
}

export const telegramStore = createTelegramStore();
export const telegramEnabled = derived(telegramStore, $store => $store.config?.enabled || false);
export const telegramStatus = derived(telegramStore, $store => $store.config?.status || 'stopped');
