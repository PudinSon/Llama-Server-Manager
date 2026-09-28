<script lang="ts">
	import { Bot, Loader2 } from '@lucide/svelte';
	import { agentStore } from '$lib/stores/agentStore.svelte';

	let active = $derived(agentStore.config.agentMode);
	let isConnected = $derived(agentStore.isConnected);
	let agentCount = $derived(agentStore.selectedAgencyAgents.length);
	let isExecuting = $derived(agentStore.isExecuting);
</script>

<button
	type="button"
	class="relative inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-border/50 transition-all hover:bg-accent"
	class:bg-primary={active}
	class:text-primary-foreground={active}
	class:ring-2={active && isExecuting}
	class:ring-emerald-500={active && isExecuting}
	title={active ? 'Desactivar modo agente' : 'Activar modo agente'}
	aria-label={active ? 'Desactivar modo agente' : 'Activar modo agente'}
	aria-pressed={active}
	onclick={() => agentStore.toggleAgentMode()}
>
	{#if isExecuting}
		<Loader2 class="h-4 w-4 animate-spin" />
	{:else}
		<Bot class="h-4 w-4" />
	{/if}

	{#if active}
		<!-- Connection status dot -->
		<span
			class="absolute -top-0.5 -right-0.5 h-2.5 w-2.5 rounded-full border border-background"
			class:bg-emerald-500={isConnected === true}
			class:bg-red-500={isConnected === false}
			class:bg-muted-foreground={isConnected === null}
		></span>

		{#if agentCount > 0}
			<!-- Agent count badge -->
			<span
				class="absolute -bottom-1 -right-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-primary px-1 text-[9px] font-bold text-primary-foreground"
			>
				{agentCount}
			</span>
		{/if}
	{/if}
</button>
