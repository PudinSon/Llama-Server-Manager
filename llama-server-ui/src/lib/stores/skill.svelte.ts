import { writable, derived } from 'svelte/store';
import { browser } from '$app/environment';
import { serviceBase } from '$lib/api';
import { toast } from 'svelte-sonner';

export interface Skill {
	name: string;
	path: string;
	description?: string;
	tags?: string[];
	confidence?: number;
	scope: string;
	active?: boolean;
	/** Contenido completo del SKILL.md (frontmatter + cuerpo). */
	content?: string;
	version?: string;
	auto_generated?: boolean;
}

export interface SkillState {
	skills: Skill[];
	activeSkills: Set<string>;
	loading: boolean;
	error: string | null;
}

const API = `${serviceBase('agent')}/api/skills`;

function createSkillStore() {
	const { subscribe, set, update } = writable<SkillState>({
		skills: [],
		activeSkills: new Set(),
		loading: false,
		error: null,
	});

	return {
		subscribe,

		async fetchSkills() {
			if (!browser) return;
			update(s => ({ ...s, loading: true }));
			try {
				const res = await fetch(API);
				if (!res.ok) throw new Error(`HTTP ${res.status}`);
				const data = await res.json();
				set({
					skills: data.skills || [],
					activeSkills: new Set(data.active || []),
					loading: false,
					error: null,
				});
			} catch (err) {
				const msg = err instanceof Error ? err.message : 'Error';
				update(s => ({ ...s, error: msg, loading: false }));
				toast.error(`Error cargando skills: ${msg}`);
			}
		},

		async searchSkills(query: string, topK: number = 5) {
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
				toast.error('Error en búsqueda de skills');
				return [];
			}
		},

		/**
		 * Guarda la edición de un skill (SKILL.md completo). El backend reescribe
		 * el archivo, refresca el índice y devuelve la lista actualizada.
		 */
		async updateSkill(skillName: string, content: string, scope?: string): Promise<boolean> {
			if (!browser) return false;
			try {
				const res = await fetch(`${API}/update`, {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ name: skillName, content, scope }),
				});
				const data = await res.json();
				if (!res.ok || !data.success) {
					throw new Error(data.error || `HTTP ${res.status}`);
				}
				if (Array.isArray(data.skills)) {
					set({
						skills: data.skills,
						activeSkills: new Set(data.active || []),
						loading: false,
						error: null,
					});
				} else {
					await this.fetchSkills();
				}
				toast.success(`Skill "${skillName}" guardado`);
				return true;
			} catch (err) {
				const msg = err instanceof Error ? err.message : 'Error';
				update(s => ({ ...s, error: msg }));
				toast.error(`No se pudo guardar: ${msg}`);
				return false;
			}
		},

		async activateSkill(skillName: string) {
			if (!browser) return;
			try {
				const res = await fetch(`${API}/activate`, {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ skill_name: skillName }),
				});
				if (!res.ok) throw new Error(`HTTP ${res.status}`);
				update(s => ({
					...s,
					activeSkills: new Set([...s.activeSkills, skillName]),
				}));
				toast.success(`Skill "${skillName}" activado`);
			} catch (err) {
				toast.error(`Error activando skill: ${err instanceof Error ? err.message : 'Error'}`);
			}
		},

		async deactivateSkill(skillName: string) {
			if (!browser) return;
			try {
				const res = await fetch(`${API}/deactivate`, {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ skill_name: skillName }),
				});
				if (!res.ok) throw new Error(`HTTP ${res.status}`);
				update(s => {
					const newActive = new Set(s.activeSkills);
					newActive.delete(skillName);
					return { ...s, activeSkills: newActive };
				});
				toast.success(`Skill "${skillName}" desactivado`);
			} catch (err) {
				toast.error(`Error desactivando skill: ${err instanceof Error ? err.message : 'Error'}`);
			}
		},
	};
}

export const skillStore = createSkillStore();

export const allSkills = derived(skillStore, $store => $store.skills);
export const activeSkillList = derived(skillStore, $store => Array.from($store.activeSkills));
export const skillsLoading = derived(skillStore, $store => $store.loading);
