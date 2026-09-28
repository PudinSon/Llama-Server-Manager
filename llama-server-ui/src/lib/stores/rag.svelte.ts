import { writable, derived } from 'svelte/store';
import { browser } from '$app/environment';
import { serviceBase } from '$lib/api';
import { toast } from 'svelte-sonner';

/**
 * Modelo de documento RAG alineado con la API de rag_web_server.py.
 * GET /api/docs/list devuelve:
 * { documents: [{ fuente: string, chunks: [{ chunk: number, texto: string }] }] }
 */
export interface RAGDocument {
	id: string;
	filename: string;
	path: string;
	size: number;
	chunks: number;
	chunkTexts: string[];
	created_at: string;
	indexed: boolean;
}

export interface RAGState {
	documents: RAGDocument[];
	loading: boolean;
	uploading: boolean;
	error: string | null;
	activeLibrary: string | null;
	libraries: string[];
	docCount: number;
	researchMode: boolean;
}

// Tras nginx el servicio vive en `/rag`, así que la base ya incluye el prefijo.
const API = `${serviceBase('rag')}/api`;
const RESEARCH_MODE_KEY = 'llama-ui-rag-research-mode';

const RESEARCH_PROMPT = `Eres un Molly profesora universitaria experta en investigación y redacción académica.
 Reglas:
    - Basa cada afirmación únicamente en fragmentos recuperados por RAG. No inventes datos, fuentes ni referencias; declara cualquier carencia de evidencia.
    - Primero usa execute_tool con {"tool_name":"list_libraries","arguments":{}} para conocer la biblioteca activa. Después usa rag_search con una consulta precisa y n_results entre 8 y 30.
    - Si necesitas una herramienta desconocida, usa query_tools. Las herramientas se ejecutan siempre con execute_tool(tool_name, arguments); sin parámetros, arguments debe ser {}.
    - Redacta con claridad,limpieza y tono objetivo,nunca menciones los # de fragmentos usados solo los documentos en las Referencias. Para informes usa: Resumen, Introducción, Desarrollo/Metodología, Resultados, Discusión, Conclusiones y Referencias. En humanidades puede usarse Introducción, Desarrollo y Conclusión.
	- No uses conocimiento externo. No generes gráficos sin datos recuperados.

    Flujo: identifica documentos relevantes; recupera evidencia; contrasta fragmentos; separa hechos de límites; responde con síntesis y referencias explícitas.`;

function readResearchMode(): boolean {
	if (typeof localStorage === 'undefined') return false;
	return localStorage.getItem(RESEARCH_MODE_KEY) === 'true';
}

function createRAGStore() {
	const { subscribe, set, update } = writable<RAGState>({
		documents: [],
		loading: false,
		uploading: false,
		error: null,
		activeLibrary: null,
		libraries: [],
		docCount: 0
		, researchMode: readResearchMode()
	});

	function mapDocs(raw: Array<{ fuente?: string; chunks?: Array<unknown> }>): RAGDocument[] {
		if (!Array.isArray(raw)) return [];
		return raw
			.filter((d) => d && typeof d === 'object')
			.map((d) => {
				const fuente = String(d.fuente ?? '');
				const chunks = Array.isArray(d.chunks) ? d.chunks : [];
				const filename = fuente.split(/[/\\]/).pop() || fuente;
				return {
					id: fuente,
					filename,
					path: fuente,
					size: 0,
					chunks: chunks.length,
					chunkTexts: chunks.map((chunk) => {
						if (chunk && typeof chunk === 'object' && 'texto' in chunk) {
							return String((chunk as { texto?: unknown }).texto ?? '');
						}
						return String(chunk ?? '');
					}),
					created_at: '',
					indexed: chunks.length > 0
				};
			});
	}

	return {
		subscribe,

		toggleResearchMode() {
			let enabled = false;
			update((state) => {
				enabled = !state.researchMode;
				return { ...state, researchMode: enabled };
			});
			if (browser) localStorage.setItem(RESEARCH_MODE_KEY, String(enabled));
			return enabled;
		},

		getResearchPrompt() {
			let activeLibrary: string | null = null;
			const unsubscribe = ragStore.subscribe((state) => (activeLibrary = state.activeLibrary));
			unsubscribe();
			return `[RAG_RESEARCH_MODE]\nBiblioteca activa: ${activeLibrary || 'ninguna'}\n\n${RESEARCH_PROMPT}`;
		},

		async fetchDocuments() {
			if (!browser) return;
			update(s => ({ ...s, loading: true, error: null }));
			try {
				const res = await fetch(`${API}/docs/list`);
				if (!res.ok) throw new Error(`HTTP ${res.status}`);
				const data = await res.json();
				const documents = mapDocs(data.documents || []);
				update(s => ({
					...s,
					documents,
					loading: false,
					docCount: documents.length
				}));
			} catch (err) {
				const msg = err instanceof Error ? err.message : 'Error';
				update(s => ({ ...s, error: msg, loading: false }));
				toast.error(`Error cargando documentos: ${msg}`);
			}
		},

		async uploadDocument(file: File | File[], collectionName: string = 'default') {
			const files = Array.isArray(file) ? file : [file];
			if (!browser || files.length === 0) return null;
			update(s => ({ ...s, uploading: true, error: null }));
			try {
				const formData = new FormData();
				for (const selectedFile of files) {
					formData.append('files', selectedFile);
				}
				formData.append('library', collectionName);

				const res = await fetch(`${API}/index`, {
					method: 'POST',
					body: formData,
				});
				if (!res.ok) throw new Error(`HTTP ${res.status}`);
				const data = await res.json();

				if (!data.success) {
					throw new Error(data.error || 'Error desconocido');
				}

				await this.fetchDocuments();
				update(s => ({ ...s, uploading: false }));
				toast.success(`${data.indexed ?? files.length} documento(s) indexado(s)`);
				return data;
			} catch (err) {
				const msg = err instanceof Error ? err.message : 'Error';
				update(s => ({ ...s, error: msg, uploading: false }));
				toast.error(`Error subiendo: ${msg}`);
				return null;
			}
		},

		async deleteDocument(docId: string) {
			if (!browser) return;
			try {
				const res = await fetch(`${API}/docs/delete`, {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ fuente: docId }),
				});
				if (!res.ok) throw new Error(`HTTP ${res.status}`);
				const data = await res.json();

				if (!data.success) {
					throw new Error(data.error || 'Error al eliminar');
				}

				update(s => ({
					...s,
					documents: s.documents.filter(d => d.id !== docId),
					docCount: Math.max(0, s.docCount - 1)
				}));
				toast.success('Documento eliminado');
			} catch (err) {
				const msg = err instanceof Error ? err.message : 'Error';
				toast.error(`Error eliminando: ${msg}`);
			}
		},

		async searchDocuments(query: string, topK: number = 5) {
			if (!browser || !query.trim()) return [];
			try {
				const res = await fetch(`${API}/search`, {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ query, top_k: topK }),
				});
				if (!res.ok) throw new Error(`HTTP ${res.status}`);
				const data = await res.json();
				return data.results || [];
			} catch (err) {
				toast.error('Error en búsqueda');
				return [];
			}
		},

		async fetchLibraries() {
			if (!browser) return [];
			try {
				const res = await fetch(`${API}/libraries`);
				if (!res.ok) throw new Error(`HTTP ${res.status}`);
				const data = await res.json();
				const libraries = data.libraries || [];
				update(s => ({ ...s, libraries }));
				return libraries;
			} catch (err) {
				console.error('[RAG] Error cargando bibliotecas:', err);
				return [];
			}
		},

		async createLibrary(name: string) {
			if (!browser || !name.trim()) return false;
			try {
				const res = await fetch(`${API}/libraries`, {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ name: name.trim() }),
				});
				if (!res.ok) throw new Error(`HTTP ${res.status}`);
				const data = await res.json();
				if (data.success) {
					await this.fetchLibraries();
					toast.success(`Biblioteca "${name}" creada`);
					return true;
				}
				throw new Error(data.error || 'Error al crear');
			} catch (err) {
				const msg = err instanceof Error ? err.message : 'Error';
				toast.error(`Error creando biblioteca: ${msg}`);
				return false;
			}
		},

		async deleteLibrary(name: string) {
			if (!browser) return false;
			try {
				const res = await fetch(`${API}/libraries`, {
					method: 'DELETE',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ name }),
				});
				if (!res.ok) throw new Error(`HTTP ${res.status}`);
				const data = await res.json();
				if (data.success) {
					await this.fetchLibraries();
					update(s => ({
						...s,
						activeLibrary: s.activeLibrary === name ? null : s.activeLibrary
					}));
					toast.success(`Biblioteca "${name}" eliminada`);
					return true;
				}
				throw new Error(data.error || 'Error al eliminar');
			} catch (err) {
				const msg = err instanceof Error ? err.message : 'Error';
				toast.error(`Error eliminando biblioteca: ${msg}`);
				return false;
			}
		},

		async fetchActive() {
			if (!browser) return null;
			try {
				const res = await fetch(`${API}/active`);
				if (!res.ok) throw new Error(`HTTP ${res.status}`);
				const data = await res.json();
				update(s => ({ ...s, activeLibrary: data.active || null }));
				return data.active || null;
			} catch (err) {
				console.error('[RAG] Error obteniendo activa:', err);
				return null;
			}
		},

		async setActive(name: string) {
			if (!browser) return false;
			try {
				const res = await fetch(`${API}/active`, {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ name }),
				});
				if (!res.ok) throw new Error(`HTTP ${res.status}`);
				const data = await res.json();
				if (data.success) {
					update(s => ({ ...s, activeLibrary: name }));
					await this.fetchDocuments();
					return true;
				}
				throw new Error(data.error || 'Error al activar');
			} catch (err) {
				const msg = err instanceof Error ? err.message : 'Error';
				toast.error(`Error activando: ${msg}`);
				return false;
			}
		},

		async fetchDocCount() {
			if (!browser) return 0;
			try {
				const res = await fetch(`${API}/docs/count`);
				if (!res.ok) throw new Error(`HTTP ${res.status}`);
				const data = await res.json();
				update(s => ({ ...s, docCount: data.count || 0 }));
				return data.count || 0;
			} catch {
				return 0;
			}
		}
	};
}

export const ragStore = createRAGStore();
export const documentList = derived(ragStore, $store => $store.documents);
export const ragLoading = derived(ragStore, $store => $store.loading);
export const ragActiveLibrary = derived(ragStore, $store => $store.activeLibrary);
export const ragLibraries = derived(ragStore, $store => $store.libraries);
export const ragDocCount = derived(ragStore, $store => $store.docCount);
export const ragResearchMode = derived(ragStore, $store => $store.researchMode);
