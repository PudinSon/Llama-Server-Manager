<script lang="ts">
	import { agentStore } from '$lib/stores/agentStore.svelte';
	import { Bot } from '@lucide/svelte';

	let mode = $derived(agentStore.status?.mode ?? '—');
	let isDaemon = $derived(mode === 'DAEMON');
</script>

<button
	class="inline-flex items-center gap-1.5 rounded-full border border-border/50 bg-background/80 px-2.5 py-1 text-[11px] font-medium backdrop-blur-sm transition-colors hover:bg-accent"
	class:text-emerald-600={isDaemon}
	class:dark:text-emerald-400={isDaemon}
	class:text-muted-foreground={!isDaemon}
	title={agentStore.lastError || `Modo: ${mode}`}
	onclick={() => agentStore.togglePanel()}
	aria-label="Abrir panel del agente"
>
	<Bot class="h-3 w-3" />
	<span class="max-w-[80px] truncate">{mode}</span>
	<span
		class="h-1.5 w-1.5 rounded-full {agentStore.isConnected === true
			? 'bg-emerald-500'
			: agentStore.isConnected === false
				? 'bg-destructive'
				: 'bg-muted-foreground/50'}"
	></span>
</button>
