<script lang="ts">
	import { skillStore, allSkills, activeSkillList, skillsLoading } from '$lib/stores/skill.svelte';
	import { BrainCircuit, Plus, RefreshCcw, Sparkles } from '@lucide/svelte';
	import { onMount } from 'svelte';

	let query = $state('');
	let filtered = $derived(
		query.trim()
			? $allSkills.filter(
					(s) =>
						(s.name ?? '').toLowerCase().includes(query.toLowerCase()) ||
						(s.description ?? '').toLowerCase().includes(query.toLowerCase())
			  )
			: $allSkills
	);

	function getConfidenceColor(conf: number | undefined): string {
		if ((conf ?? 0) >= 0.75) return 'bg-emerald-500/20 text-emerald-600 dark:text-emerald-400';
		if ((conf ?? 0) >= 0.5) return 'bg-amber-500/20 text-amber-600 dark:text-amber-400';
		return 'bg-muted text-muted-foreground';
	}

	async function toggleSkill(name: string | undefined) {
		if (!name) return;
		if ($activeSkillList.includes(name)) {
			await skillStore.deactivateSkill(name);
		} else {
			await skillStore.activateSkill(name);
		}
	}

	onMount(() => {
		void skillStore.fetchSkills();
	});
</script>

<div class="space-y-3">
	<div class="flex items-center justify-between">
		<span class="text-xs font-medium uppercase tracking-wider text-muted-foreground">
			Skills activos
		</span>
		<button
			type="button"
			onclick={() => void skillStore.fetchSkills()}
			class="inline-flex items-center gap-1 rounded-md border border-border/50 px-1.5 py-1 text-[10px] text-muted-foreground hover:bg-accent hover:text-foreground"
		>
			<RefreshCcw class="h-3 w-3" />
			Actualizar
		</button>
	</div>

	{#if $allSkills.length > 2}
		<div class="relative">
			<Sparkles class="absolute left-2 top-1/2 h-3 w-3 -translate-y-1/2 text-muted-foreground" />
			<input
				type="text"
				placeholder="Filtrar skills..."
				class="w-full rounded-md border border-border/50 bg-muted/50 py-1 pl-6 pr-2 text-[11px] placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
				bind:value={query}
			/>
		</div>
	{/if}

	{#if filtered.length > 0}
		<div class="space-y-1.5">
			{#each filtered as skill}
				<div class="flex items-start gap-2.5 rounded-lg border border-border/50 bg-muted/30 p-2.5">
					<div class="mt-0.5 shrink-0 rounded-md bg-primary/10 p-1 text-primary">
						<BrainCircuit class="h-3.5 w-3.5" />
					</div>
					<div class="min-w-0 flex-1">
						<div class="flex items-center gap-2">
							<span class="text-xs font-medium text-foreground/80">{skill.name || 'Sin nombre'}</span>
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
					<button
						type="button"
						onclick={() => toggleSkill(skill.name)}
						class="mt-0.5 inline-flex h-7 w-7 items-center justify-center rounded-md border border-border/50 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
						aria-label={$activeSkillList.includes(skill.name ?? '') ? 'Desactivar skill' : 'Activar skill'}
					>
						{#if $activeSkillList.includes(skill.name ?? '')}
							<Sparkles class="h-3.5 w-3.5" />
						{:else}
							<Plus class="h-3.5 w-3.5" />
						{/if}
					</button>
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
