import { writable } from 'svelte/store';

/**
 * Store global para control de modales abiertas en la aplicación.
 * Permite abrir modales desde cualquier componente (sidebar, chat, etc.)
 */
export const modalStore = writable({
	rag: false,
	workspace: false,
	telegram: false,
	agent: false,
	jobs: false,
	skills: false,
	tasks: false
});

export function openRAGModal() {
	modalStore.update(s => ({ ...s, rag: true }));
}

export function closeRAGModal() {
	modalStore.update(s => ({ ...s, rag: false }));
}

export function openWorkspaceModal() {
	modalStore.update(s => ({ ...s, workspace: true }));
}

export function closeWorkspaceModal() {
	modalStore.update(s => ({ ...s, workspace: false }));
}

export function openTelegramModal() {
	modalStore.update(s => ({ ...s, telegram: true }));
}

export function closeTelegramModal() {
	modalStore.update(s => ({ ...s, telegram: false }));
}

export function openAgentModal() {
	modalStore.update(s => ({ ...s, agent: true }));
}

export function closeAgentModal() {
	modalStore.update(s => ({ ...s, agent: false }));
}

export function openJobsModal() {
	modalStore.update(s => ({ ...s, jobs: true }));
}

export function closeJobsModal() {
	modalStore.update(s => ({ ...s, jobs: false }));
}

export function openSkillsModal() {
	modalStore.update(s => ({ ...s, skills: true }));
}

export function closeSkillsModal() {
	modalStore.update(s => ({ ...s, skills: false }));
}

export function openTasksModal() {
	modalStore.update(s => ({ ...s, tasks: true }));
}

export function closeTasksModal() {
	modalStore.update(s => ({ ...s, tasks: false }));
}
