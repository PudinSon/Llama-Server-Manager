<script lang="ts">
	import { Pause, Play, Square } from '@lucide/svelte';
	import { agentStore } from '$lib/stores/agentStore.svelte';

	let daemon = $derived(agentStore.status?.mode);
	let running = $derived(daemon === 'DAEMON' || daemon === 'PAUSED');
	let paused = $derived(daemon === 'PAUSED');

</script>

<div class="space-y-3">
	<div class="flex items-center justify-between">
		<span class="text-xs font-medium uppercase tracking-wider text-muted-foreground">Daemon</span>
		<span class="text-[10px] font-semibold uppercase text-muted-foreground"
			>{daemon || 'stopped'}</span
		>
	</div>
	{#if !running}
		<div class="flex items-center justify-between gap-2">
			<span class="text-xs text-muted-foreground">Se inicia con el siguiente mensaje del chat.</span>
			<button
				class="inline-flex h-8 shrink-0 items-center justify-center gap-1 rounded-md border border-border/50 px-2 text-xs hover:bg-accent"
				title="Activar daemon para el siguiente mensaje"
				onclick={() => agentStore.toggleDaemonMode()}><Play class="h-3.5 w-3.5" /> Activar</button
			>
		</div>
	{:else}
		<div class="flex gap-2">
			<button
				class="inline-flex h-8 flex-1 items-center justify-center gap-1 rounded-md border border-border/50 text-xs hover:bg-accent"
				onclick={() => (paused ? agentStore.resume() : agentStore.pause())}
				>{#if paused}<Play class="h-3.5 w-3.5" /> Reanudar{:else}<Pause class="h-3.5 w-3.5" /> Pausar{/if}</button
			>
			<button
				class="inline-flex h-8 w-8 items-center justify-center rounded-md border border-destructive/40 text-destructive hover:bg-destructive/10"
				title="Detener daemon"
				aria-label="Detener daemon"
				onclick={() => agentStore.stopDaemon()}><Square class="h-3.5 w-3.5" /></button
			>
		</div>
	{/if}
</div>
