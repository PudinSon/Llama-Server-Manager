<script lang="ts">
	import { CheckCircle2, Circle, Loader2, XCircle } from '@lucide/svelte';
	import { agentStore } from '$lib/stores/agentStore.svelte';

	/**
	 * Plan REAL de la ejecución agencial.
	 *
	 * `agencyPlan` lo construye el reductor único con los pasos que el backend
	 * emite en `agency_selected`/`plan` (`{id, agent, subtask, depends_on, phase,
	 * artifact}`) y su estado lo mueven `step_start`/`agent_start`/`agent_end`/
	 * `step_end`. Antes este componente leía `plan.steps[].status` y
	 * `plan.total`: campos que el backend NUNCA manda, así que la lista salía
	 * vacía y el contador siempre en 0/0.
	 */
	let steps = $derived(agentStore.agencyPlan);
	let totalSteps = $derived(steps.length);
	let completed = $derived(steps.filter((step) => step.status === 'done').length);
	let active = $derived(steps.filter((step) => step.status === 'running').length);
	let progress = $derived(totalSteps > 0 ? Math.round((completed / totalSteps) * 100) : 0);
	let incomplete = $derived(agentStore.agencyIncomplete);
	let denials = $derived(agentStore.agencyDenials);

	function getStatusIcon(status: string) {
		switch (status) {
			case 'done':
				return CheckCircle2;
			case 'running':
				return Loader2;
			case 'error':
				return XCircle;
			default:
				return Circle;
		}
	}

	function getStatusClasses(status: string): string {
		switch (status) {
			case 'done':
				return 'text-emerald-500';
			case 'running':
				return 'text-primary animate-spin';
			case 'error':
				return 'text-destructive';
			default:
				return 'text-muted-foreground/50';
		}
	}

	function stepLabel(index: number): string {
		const step = steps[index];

		return step.subtask || step.agent_name || `Paso ${index + 1}`;
	}
</script>

<div class="space-y-3.5">
	<div class="flex items-center justify-between gap-3">
		<span class="text-xs font-semibold text-foreground">Plan de ejecución</span>
		{#if totalSteps > 0}
			<span class="shrink-0 font-mono text-[10px] tabular-nums text-muted-foreground">
				{completed}/{totalSteps} completados{#if active > 0}
					· {active} activo{/if}
			</span>
		{/if}
	</div>
	{#if agentStore.agencyGoal}
		<p
			class="break-words border-l-2 border-primary/30 pl-2.5 text-[11px] leading-relaxed text-muted-foreground"
		>
			{agentStore.agencyGoal}
		</p>
	{/if}

	{#if totalSteps > 0}
		<div class="h-1.5 overflow-hidden rounded-full bg-muted">
			<div
				class="h-full rounded-full bg-primary transition-all duration-500"
				style:width="{progress}%"
			></div>
		</div>
	{/if}

	{#if incomplete}
		<p
			class="rounded border border-destructive/20 bg-destructive/5 px-2.5 py-2 text-xs leading-relaxed text-destructive"
		>
			<span class="font-semibold">Ejecución incompleta</span>
			<span class="text-destructive/85">{incomplete}</span>
		</p>
	{/if}

	{#if steps.length > 0}
		<ol class="divide-y divide-border/50 overflow-hidden rounded border border-border/60">
			{#each steps as step, i (step.id)}
				{@const Icon = getStatusIcon(step.status)}
				{@const phases = step.phases ?? (step.phase ? step.phase.split('+') : [])}
				<li class="flex min-w-0 items-start gap-2.5 px-3 py-2.5">
					<span
						class="mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full bg-muted/70 font-mono text-[10px] text-muted-foreground"
					>
						{#if step.status === 'done'}
							<Icon class="h-3 w-3 {getStatusClasses(step.status).replace('animate-spin', '')}" />
						{:else if step.status === 'running' || step.status === 'error'}
							<Icon class="h-3 w-3 {getStatusClasses(step.status)}" />
						{:else}
							{i + 1}
						{/if}
					</span>
					<div class="min-w-0 flex-1">
						<p class="break-words text-xs leading-relaxed text-foreground/90" title={stepLabel(i)}>
							{stepLabel(i)}
						</p>
						<div class="mt-1.5 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
							{#if step.agent_name}
								<span class="max-w-full truncate text-[10px] font-medium text-muted-foreground"
									>{step.agent_name}</span
								>
							{/if}
							{#each phases as phase (phase)}
								<span
									class="rounded-sm bg-muted px-1.5 py-0.5 text-[9px] font-medium text-muted-foreground"
									>{phase}</span
								>
							{/each}
							{#if step.depends_on?.length}
								<span class="text-[10px] text-muted-foreground"
									>Después de {step.depends_on.map((id) => `#${id}`).join(', ')}</span
								>
							{/if}
							{#if step.artifact}
								<span class="max-w-full truncate font-mono text-[10px] text-muted-foreground"
									>{step.artifact}</span
								>
							{/if}
							<span
								class="text-[10px] font-medium {getStatusClasses(step.status).replace(
									'animate-spin',
									''
								)}"
							>
								{step.status === 'done'
									? 'Completado'
									: step.status === 'running'
										? 'En curso'
										: step.status === 'error'
											? 'Error'
											: 'Pendiente'}
							</span>
						</div>
					</div>
				</li>
			{/each}
		</ol>
	{:else}
		<div
			class="flex items-center gap-2 rounded border border-dashed border-border/70 px-3 py-4 text-xs text-muted-foreground"
		>
			<Circle class="h-3.5 w-3.5 shrink-0" />
			<p>Sin ejecución agencial activa</p>
		</div>
	{/if}

	{#if denials.length > 0}
		<div class="space-y-2 border-t border-border/50 pt-3">
			<span class="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
				Herramientas denegadas
			</span>
			{#each denials as denial, i (denial.agent + denial.name + i)}
				<p
					class="break-words rounded border border-amber-500/20 bg-amber-500/5 px-2.5 py-2 text-[11px] leading-relaxed text-muted-foreground"
				>
					<span class="font-medium text-foreground">{denial.agent}</span>
					intentó <code class="rounded-sm bg-muted px-1 py-0.5">{denial.name}</code>:
					{denial.reason || 'la herramienta no está permitida para este agente'}
				</p>
			{/each}
		</div>
	{/if}
</div>
