<script lang="ts">
	import { agentStore } from '$lib/stores/agentStore.svelte';
	import { Wifi, WifiOff } from '@lucide/svelte';

	let mode = $derived(agentStore.status?.mode ?? 'UNKNOWN');
	let confidence = $derived(agentStore.status?.confidence ?? 0);
	let task = $derived(agentStore.status?.task ?? 'Ninguna');
	let confidencePercent = $derived(Math.round(confidence * 100));

	let confidenceColor = $derived.by(() => {
		if (confidencePercent >= 75) return 'text-emerald-500';
		if (confidencePercent >= 50) return 'text-amber-500';
		return 'text-muted-foreground';
	});

	let modeBadge = $derived.by(() => {
		switch (mode) {
			case 'DAEMON':
				return 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400';
			case 'PAUSED':
				return 'bg-amber-500/15 text-amber-600 dark:text-amber-400';
			case 'GENERAL':
				return 'bg-muted text-muted-foreground';
			default:
				return 'bg-blue-500/15 text-blue-600 dark:text-blue-400';
		}
	});
</script>

{#if agentStore.status}
	<div class="space-y-3">
		<div class="flex items-center justify-between">
			<span class="text-xs font-medium uppercase tracking-wider text-muted-foreground">
				Estado
			</span>
			<div class="flex items-center gap-1.5">
				{#if agentStore.isConnected === true}
					<Wifi class="h-3 w-3 text-emerald-500" />
				{:else if agentStore.isConnected === false}
					<WifiOff class="h-3 w-3 text-destructive" />
				{/if}
				<span
					class="inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-semibold {modeBadge}"
				>
					{mode}
				</span>
			</div>
		</div>

		<div class="space-y-2">
			<div class="flex items-center justify-between text-sm">
				<span class="text-muted-foreground">Confianza</span>
				<span class="font-mono font-medium {confidenceColor}">
					{confidencePercent}%
				</span>
			</div>
			<div class="h-1.5 overflow-hidden rounded-full bg-muted">
				<div
					class="h-full rounded-full transition-all duration-500 ease-out {confidencePercent >= 75
						? 'bg-emerald-500'
						: confidencePercent >= 50
							? 'bg-amber-500'
							: 'bg-muted-foreground/40'}"
					style:width="{confidencePercent}%"
				></div>
			</div>
		</div>

		<div class="space-y-1">
			<span class="text-xs font-medium uppercase tracking-wider text-muted-foreground">
				Tarea
			</span>
			<p class="text-sm leading-relaxed text-foreground/80 break-words">
				{task}
			</p>
		</div>

		{#if agentStore.lastError}
			<p class="line-clamp-2 text-[11px] text-destructive" title={agentStore.lastError}>
				{agentStore.lastError}
			</p>
		{/if}
	</div>
{/if}
