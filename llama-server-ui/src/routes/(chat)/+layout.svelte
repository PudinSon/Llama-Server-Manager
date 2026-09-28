<script lang="ts">
	import { page } from '$app/state';
	import { ChatScreen, ChatTabs } from '$lib/components/app';
	import SystemMonitorWidget from '$lib/components/agent/SystemMonitorWidget.svelte';
	import { NEW_CHAT_TAB_ID } from '$lib/constants';
	import { agentStore } from '$lib/stores/agentStore.svelte';
	import { settingsStore, tabsStore } from '$lib/stores';
	import { taskStore } from '$lib/stores/task.svelte';

	let { children } = $props();

	let showCenteredEmpty = $derived(!page.params.id);
	let showTabs = $derived(
		Boolean(settingsStore.config.conversationTabs) &&
			(page.params.id || tabsStore.openTabs.some((id) => id !== NEW_CHAT_TAB_ID))
	);

	$effect(() => {
		const id = page.params.id ?? (page.route.id === '/(chat)' ? NEW_CHAT_TAB_ID : undefined);
		if (id && settingsStore.config.conversationTabs) tabsStore.syncWithRoute(id);
	});

	// Sondeo de tareas programadas: anuncia en la conversación las que el daemon
	// haya ejecutado, sin necesidad de abrir el modal de Tareas.
	$effect(() => {
		taskStore.startPolling();
		return () => taskStore.stopPolling();
	});

	// El agente queda activo en segundo plano sin necesidad de abrir su modal.
	$effect(() => {
		agentStore.ensurePolling();
		void agentStore.fetchAgencyAgents();
		void agentStore.restoreActiveJob();
	});
</script>

<SystemMonitorWidget />
<div class={showTabs ? 'md:[--chat-tabs-offset:1.25rem]' : ''}>
	{#if showTabs}
		<ChatTabs />
	{/if}
	<ChatScreen {showCenteredEmpty} />
</div>

{@render children?.()}
