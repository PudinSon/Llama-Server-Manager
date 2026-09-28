<script lang="ts">
	import { RotateCcw } from '@lucide/svelte';
	import { agentStore } from '$lib/stores/agentStore.svelte';
	import { toast } from 'svelte-sonner';

	let checkpoints = $derived(agentStore.checkpoints);

	async function restore(id: string | undefined) {
		if (!id || !confirm('¿Restaurar este checkpoint?')) return;
		if (await agentStore.rollback(id)) toast.success('Checkpoint restaurado');
	}
</script>

<div class="space-y-3">
	<div class="flex items-center justify-between">
		<span class="text-xs font-medium uppercase tracking-wider text-muted-foreground"
			>Checkpoints</span
		>
		<span class="text-[10px] tabular-nums text-muted-foreground">{checkpoints.length}</span>
	</div>
	{#if checkpoints.length > 0}
		<div class="space-y-1.5">
			{#each checkpoints.slice(0, 5) as checkpoint}
				<div class="flex items-center gap-2 rounded-lg border border-border/50 bg-muted/30 p-2">
					<div class="min-w-0 flex-1">
						<p class="truncate text-xs font-medium">
							{checkpoint.description || checkpoint.id || 'Checkpoint'}
						</p>
						{#if checkpoint.created_at}<p class="text-[10px] text-muted-foreground">
								{checkpoint.created_at}
							</p>{/if}
					</div>
					<button
						class="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
						title="Restaurar"
						aria-label="Restaurar checkpoint"
						onclick={() => restore(checkpoint.id)}
					>
						<RotateCcw class="h-3.5 w-3.5" />
					</button>
				</div>
			{/each}
		</div>
	{:else}
		<p class="py-3 text-center text-xs text-muted-foreground">Sin checkpoints disponibles</p>
	{/if}
</div>
