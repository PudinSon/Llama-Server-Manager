<script lang="ts">
	import { agentStore } from '$lib/stores/agentStore.svelte';
	import { Brain, Puzzle, Search } from '@lucide/svelte';

	interface SkillItem {
		name: string | undefined;
		description: string | undefined;
		confidence: number | undefined;
	}

	let query = $state('');
	let skills = $derived((agentStore.skills ?? []) as SkillItem[]);
	let filtered = $derived(
		query.trim()
			? skills.filter(
					(s) =>
						(s.name ?? '').toLowerCase().includes(query.toLowerCase()) ||
						(s.description ?? '').toLowerCase().includes(query.toLowerCase())
				)
			: skills
	);

	function getConfidenceColor(conf: number): string {
		if (conf >= 0.75) return 'bg-emerald-500/20 text-emerald-600 dark:text-emerald-400';
		if (conf >= 0.5) return 'bg-amber-500/20 text-amber-600 dark:text-amber-400';
		return 'bg-muted text-muted-foreground';
	}
</script>

<div class="space-y-3">
	<div class="flex items-center justify-between">
		<span class="text-xs font-medium uppercase tracking-wider text-muted-foreground">
			Skills
		</span>
		<span class="text-[10px] tabular-nums text-muted-foreground">{filtered.length}</span>
	</div>

	{#if skills.length > 3}
		<div class="relative">
			<Search class="absolute left-2 top-1/2 h-3 w-3 -translate-y-1/2 text-muted-foreground" />
			<input
				type="text"
				placeholder="Filtrar..."
				class="w-full rounded-md border border-border/50 bg-muted/50 py-1 pl-6 pr-2 text-[11px] placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
				bind:value={query}
			/>
		</div>
	{/if}

	{#if filtered.length > 0}
		<div class="space-y-1.5">
			{#each filtered as skill}
				<div
					class="flex items-start gap-2.5 rounded-lg border border-border/50 bg-muted/30 p-2.5 transition-colors hover:bg-muted/50"
				>
					<div class="mt-0.5 shrink-0">
						{#if skill.confidence !== undefined && skill.confidence >= 0.7}
							<Brain class="h-3.5 w-3.5 text-emerald-500" />
						{:else}
							<Puzzle class="h-3.5 w-3.5 text-muted-foreground" />
						{/if}
					</div>
					<div class="min-w-0 flex-1">
						<div class="flex items-center gap-2">
							<span class="text-xs font-medium text-foreground/80">
								{skill.name || 'Sin nombre'}
							</span>
							{#if skill.confidence !== undefined}
								<span
									class="inline-flex shrink-0 items-center rounded-full px-1.5 py-0.5 text-[9px] font-semibold {getConfidenceColor(
										skill.confidence
									)}"
								>
									{Math.round(skill.confidence * 100)}%
								</span>
							{/if}
						</div>
						{#if skill.description}
							<p class="mt-0.5 text-[11px] leading-snug text-muted-foreground line-clamp-2">
								{skill.description}
							</p>
						{/if}
					</div>
				</div>
			{/each}
		</div>
	{:else}
		<div class="flex flex-col items-center justify-center py-4 text-center">
			<p class="text-xs text-muted-foreground">
				{query ? 'Sin coincidencias' : 'Sin skills disponibles'}
			</p>
		</div>
	{/if}
</div>
