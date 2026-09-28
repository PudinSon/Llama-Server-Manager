<script lang="ts">
	import { agentStore } from '$lib/stores/agentStore.svelte';
	import { ChevronLeft, ChevronRight } from '@lucide/svelte';

	interface MemoryItem {
		task: string | undefined;
		outcome: string | undefined;
		timestamp: number | undefined;
	}

	let limit = $state(5);
	let offset = $state(0);
	let memories = $derived((agentStore.memory ?? []) as MemoryItem[]);
	let paginated = $derived(memories.slice(offset, offset + limit));
	let hasMore = $derived(offset + limit < memories.length);

	function formatTimestamp(ts: number | undefined | null): string {
		if (!ts) return '';
		try {
			const date = new Date(ts * 1000);
			return date.toLocaleString([], {
				month: 'short',
				day: 'numeric',
				hour: '2-digit',
				minute: '2-digit'
			});
		} catch {
			return '';
		}
	}
</script>

<div class="space-y-3">
	<div class="flex items-center justify-between">
		<span class="text-xs font-medium uppercase tracking-wider text-muted-foreground">
			Memoria Episódica
		</span>
		{#if memories.length > limit}
			<div class="flex items-center gap-1">
				<button
					class="inline-flex h-5 w-5 items-center justify-center rounded text-muted-foreground hover:bg-accent disabled:opacity-30"
					disabled={offset === 0}
					onclick={() => (offset = Math.max(0, offset - limit))}
				>
					<ChevronLeft class="h-3 w-3" />
				</button>
				<span class="text-[10px] tabular-nums text-muted-foreground"
					>{offset + 1}-{Math.min(offset + limit, memories.length)} / {memories.length}</span
				>
				<button
					class="inline-flex h-5 w-5 items-center justify-center rounded text-muted-foreground hover:bg-accent disabled:opacity-30"
					disabled={!hasMore}
					onclick={() => (offset += limit)}
				>
					<ChevronRight class="h-3 w-3" />
				</button>
			</div>
		{/if}
	</div>

	{#if paginated.length > 0}
		<div class="space-y-2">
			{#each paginated as memory}
				<div class="rounded-lg border border-border/50 bg-muted/30 p-2.5 transition-colors hover:bg-muted/50">
					<div class="flex items-start justify-between gap-2">
						<p class="text-xs font-medium leading-snug text-foreground/80 line-clamp-2">
							{memory.task || 'Sin descripción'}
						</p>
						{#if memory.timestamp}
							<span class="shrink-0 text-[10px] tabular-nums text-muted-foreground">
								{formatTimestamp(memory.timestamp)}
							</span>
						{/if}
					</div>
					{#if memory.outcome}
						<p class="mt-1 text-[11px] leading-snug text-muted-foreground line-clamp-2">
							{memory.outcome}
						</p>
					{/if}
				</div>
			{/each}
		</div>
	{:else}
		<div class="flex flex-col items-center justify-center py-6 text-center">
			<p class="text-xs text-muted-foreground">Sin memorias registradas</p>
		</div>
	{/if}
</div>
