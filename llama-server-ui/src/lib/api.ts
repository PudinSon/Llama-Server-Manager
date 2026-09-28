/**
 * Resolución central de las URLs de los servicios del Manager.
 *
 * Dos modos, decididos en tiempo de ejecución:
 *
 *  1. **Directo** (UI abierta en `http://IP:8081`, `localhost`, etc.): cada
 *     servicio usa su propio puerto en el MISMO host del navegador —
 *     `http://192.168.1.10:19000/api/workspace/list`. Nunca `localhost`, porque
 *     eso apuntaría a la máquina del usuario y no al servidor.
 *
 *  2. **Proxy** (dominio detrás de nginx, puerto 80/443): rutas relativas —
 *     `/api/workspace/list`, `/rag/...`, `/piper/...` — para no romper HTTPS
 *     (mixed content) ni depender de puertos publicados.
 *
 * Se puede forzar el modo con `localStorage.setItem('llama-api-mode', 'proxy'|'direct')`.
 */
import { browser } from '$app/environment';

export type ServiceName =
	| 'agent'
	| 'automation'
	| 'chat'
	| 'config'
	| 'manager'
	| 'piper'
	| 'rag'
	| 'workspace';

export const SERVICE_PORTS: Record<ServiceName, number> = {
	agent: 15000,
	automation: 15001,
	chat: 8081,
	config: 5001,
	manager: 12000,
	piper: 8880,
	rag: 25000,
	workspace: 19000
};

/**
 * Prefijo público con el que nginx expone cada servicio.
 * Vacío = las rutas del servicio ya son únicas (`/api/config`, `/api/workspace`,
 * `/api/jobs`, `/daemon_tasks`, ...), así que se usan tal cual.
 */
const PROXY_PREFIXES: Record<ServiceName, string> = {
	agent: '',
	automation: '',
	chat: '',
	config: '',
	manager: '/api/mgr',
	piper: '/piper',
	rag: '/rag',
	workspace: ''
};

const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '::1', '0.0.0.0']);
const DIRECT_PORT_HINTS = new Set(['8081', '5173']);

const API_MODE_KEY = 'llama-api-mode';
const API_TOKEN_KEY = 'llama-api-token';

export type ApiMode = 'direct' | 'proxy';

/** Modo efectivo: override manual > heurística por host/puerto. */
export function apiMode(): ApiMode {
	if (!browser) return 'proxy';
	try {
		const forced = localStorage.getItem(API_MODE_KEY);
		if (forced === 'direct' || forced === 'proxy') return forced;
	} catch {
		// localStorage no disponible
	}
	const { hostname, port } = window.location;
	if (LOOPBACK_HOSTS.has(hostname)) return 'direct';
	if (port && DIRECT_PORT_HINTS.has(port)) return 'direct';
	return 'proxy';
}

/** Fuerza el modo de resolución (null para volver a la heurística). */
export function setApiMode(mode: ApiMode | null): void {
	if (!browser) return;
	try {
		if (mode) localStorage.setItem(API_MODE_KEY, mode);
		else localStorage.removeItem(API_MODE_KEY);
	} catch {
		// ignore
	}
}

/** Token opcional para `@requires_auth` (ver tools/auth.py). */
export function apiToken(): string {
	if (!browser) return '';
	try {
		return localStorage.getItem(API_TOKEN_KEY) || '';
	} catch {
		return '';
	}
}

export function setApiToken(token: string): void {
	if (!browser) return;
	try {
		if (token) localStorage.setItem(API_TOKEN_KEY, token);
		else localStorage.removeItem(API_TOKEN_KEY);
	} catch {
		// ignore
	}
}

/** Base (sin barra final) para un servicio, según el modo activo. */
export function serviceBase(name: ServiceName): string {
	if (!browser) {
		return `http://127.0.0.1:${SERVICE_PORTS[name]}`;
	}
	if (apiMode() === 'proxy') {
		return PROXY_PREFIXES[name];
	}
	const { protocol, hostname } = window.location;
	return `${protocol}//${hostname}:${SERVICE_PORTS[name]}`;
}

function joinPath(path: string): string {
	const clean = path.startsWith('/') ? path : `/${path}`;
	return clean;
}

/** URL absoluta/relativa para un endpoint de un servicio. */
export function serviceUrl(name: ServiceName, path: string): string {
	return `${serviceBase(name)}${joinPath(path)}`;
}

/**
 * URL de WebSocket para un servicio (`/ws/call` de Piper).
 * En modo proxy usa `wss://dominio/ws/call` (nginx: `location /ws/`).
 */
export function serviceWsUrl(name: ServiceName, path: string): string {
	if (!browser) {
		return `ws://127.0.0.1:${SERVICE_PORTS[name]}${joinPath(path)}`;
	}
	if (apiMode() === 'proxy') {
		const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
		return `${scheme}://${window.location.host}${joinPath(path)}`;
	}
	const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
	return `${scheme}://${window.location.hostname}:${SERVICE_PORTS[name]}${joinPath(path)}`;
}

/** fetch() a un servicio, añadiendo el token (si lo hay) y sin cachear en GET. */
export function apiFetch(
	name: ServiceName,
	path: string,
	init: RequestInit = {}
): Promise<Response> {
	const url = serviceUrl(name, path);
	const headers = new Headers(init.headers);
	const token = apiToken();
	if (token) headers.set('Authorization', `Bearer ${token}`);
	return fetch(url, { ...init, headers });
}

/** Igual que apiFetch pero devuelve JSON y lanza si la respuesta no es ok. */
export async function apiJson<T = unknown>(
	name: ServiceName,
	path: string,
	init: RequestInit = {}
): Promise<T> {
	const res = await apiFetch(name, path, init);
	if (!res.ok) {
		let detail = `HTTP ${res.status}`;
		try {
			const body = await res.json();
			detail = body?.error || detail;
		} catch {
			// sin cuerpo JSON
		}
		throw new Error(detail);
	}
	return (await res.json()) as T;
}

/** Subida multipart (subidas de archivos al workspace o al RAG). */
export function apiUpload(
	name: ServiceName,
	path: string,
	form: FormData,
	init: RequestInit = {}
): Promise<Response> {
	return apiFetch(name, path, { ...init, method: 'POST', body: form });
}
