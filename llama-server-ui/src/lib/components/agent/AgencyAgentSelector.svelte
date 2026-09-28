<script lang="ts">
	import { Search, Users } from '@lucide/svelte';
	import { agentStore } from '$lib/stores/agentStore.svelte';
	import { onMount } from 'svelte';

	let query = $state('');
	let agents = $derived(agentStore.agencyAgents);
	let selected = $derived(new Set(agentStore.selectedAgencyAgents));
	let filtered = $derived(
		agents.filter((agent) => {
			const queryText = query.trim().toLowerCase();
			const matchesQuery =
				!queryText ||
				[
					agent.name,
					agent.display_name_es ?? '',
					...(agent.aliases ?? []),
					agent.category,
					agent.description,
					agent.vibe
				].some((value) =>
					value.toLowerCase().includes(queryText)
				);
			const matchesFilter =
				agentStore.selectionFilter === 'all' ||
				(agentStore.selectionFilter === 'selected'
					? selected.has(agent.name)
					: !selected.has(agent.name));

			return matchesQuery && matchesFilter;
		})
	);

	onMount(() => {
		if (agents.length === 0) void agentStore.fetchAgencyAgents();
	});

	function toggle(name: string) {
		const names = [...selected];
		if (selected.has(name)) {
			agentStore.setSelectedAgencyAgents(names.filter((item) => item !== name));
		} else if (names.length < 3) {
			agentStore.setSelectedAgencyAgents([...names, name]);
		}
	}
</script>

<div class="space-y-3">
	<div class="flex items-center justify-between">
		<span class="text-xs font-medium uppercase tracking-wider text-muted-foreground"
			>Agentes especializados</span
		>
		<span class="text-[10px] tabular-nums text-muted-foreground">{selected.size}/3</span>
		<button
			type="button"
			class="text-[10px] text-muted-foreground hover:text-foreground disabled:opacity-40"
			disabled={selected.size === 0}
			onclick={() => agentStore.clearSelectedAgencyAgents()}
		>
			Limpiar
		</button>
	</div>
	<select
		class="w-full rounded-md border border-border/50 bg-muted/50 px-2 py-1.5 text-xs"
		value={agentStore.selectionFilter}
		onchange={(event) =>
			agentStore.setSelectionFilter(event.currentTarget.value as 'all' | 'selected' | 'available')}
	>
		<option value="all">Todos los agentes</option>
		<option value="selected">Solo seleccionados</option>
		<option value="available">No seleccionados</option>
	</select>
	<div class="relative">
		<Search class="absolute left-2 top-1/2 h-3 w-3 -translate-y-1/2 text-muted-foreground" />
		<input
			class="w-full rounded-md border border-border/50 bg-muted/50 py-1.5 pl-7 pr-2 text-xs"
			placeholder="Buscar agentes"
			bind:value={query}
		/>
	</div>
	{#if filtered.length > 0}
		<div class="max-h-52 space-y-1 overflow-y-auto">
			{#each filtered as agent}
				<label
					class="flex cursor-pointer items-start gap-2 rounded-md border border-border/40 p-2 hover:bg-accent/50"
				>
					<input
						type="checkbox"
						class="mt-0.5"
						checked={selected.has(agent.name)}
						disabled={!selected.has(agent.name) && selected.size >= 3}
						onchange={() => toggle(agent.name)}
					/>
					<div class="min-w-0 flex-1">
						<div class="flex items-center gap-1.5 text-xs font-medium">
							<span>{agent.emoji}</span><span class="truncate">{agent.display_name_es ?? agent.name}</span>
						</div>
						<p class="mt-0.5 line-clamp-2 text-[10px] text-muted-foreground">{agent.description}</p>
					</div>
				</label>
			{/each}
		</div>
	{:else}
		<div class="flex items-center justify-center gap-2 py-4 text-xs text-muted-foreground">
			<Users class="h-3.5 w-3.5" /> Sin agentes disponibles
		</div>
	{/if}
</div>
