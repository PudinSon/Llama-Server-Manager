/**
 * Explicit store initialization, run once and shared by every caller.
 *
 * Order matters: migrations run first because they rename and rewrite
 * localStorage keys, so every store that reads localStorage initializes
 * only after they complete. Constructors and module-level side effects
 * stay empty so import order can no longer change startup behavior.
 *
 * The returned promise resolves once the persisted state is in memory, which
 * route loads await before reading settings: they run ahead of the root layout
 * script. The conversation list loads in the background, awaited by the chat
 * page that renders it.
 */

// direct imports, not via the barrel, to avoid circular deps
import { conversationsStore } from './conversations/index.svelte';
import { permissionsStore } from './permissions.svelte';
import { settingsStore } from './settings/index.svelte';
import { toolsStore } from './tools.svelte';
import { versionStore } from './version.svelte';
import { browser } from '$app/environment';
import { MigrationService } from '$lib/services/migration.service';
import { ScheduledChatsService } from '$lib/services/scheduled-chats.service';
import { tabsStore } from './tabs.svelte';

let startup: Promise<void> | null = null;

export function initStores(): Promise<void> {
	if (!browser) return Promise.resolve();

	startup ??= (async () => {
		await MigrationService.runAllMigrations();

		settingsStore.initialize();
		permissionsStore.initialize();
		toolsStore.initialize();
		void versionStore.initialize();
		void conversationsStore.initialize().then(() => {
			tabsStore.init(conversationsStore.conversations.map((conversation) => conversation.id));
			// Las tareas programadas corren en el backend aunque esta ventana
			// estuviera cerrada; aquí se incorporan sus chats al historial nativo.
			ScheduledChatsService.startAutoSync();
		});
	})();

	return startup;
}
