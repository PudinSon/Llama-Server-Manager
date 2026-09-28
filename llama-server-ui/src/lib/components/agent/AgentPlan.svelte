<script lang="ts">
	import { agentStore } from '$lib/stores/agentStore.svelte';
	import { CheckCircle2, Circle, Loader2, XCircle } from '@lucide/svelte';

	interface PlanStep {
		status: string | undefined;
		task: string | undefined;
		description: string | undefined;
	}

	let plan = $derived(agentStore.plan as { steps: PlanStep[] | undefined; total: number | undefined} | null);
	let steps = $derived(plan?.steps ?? []);
	let totalSteps = $derived(plan?.total ?? 0);
	let completed = $derived(steps.filter((s) => s.status === 'completed').length);
	let progress = $derived(totalSteps > 0 ? Math.round((completed / totalSteps) * 100) : 0);

	function getStatusIcon(status: string | undefined) {
		switch (status) {
			case 'completed':
				return CheckCircle2;
			case 'in_progress':
				return Loader2;
			case 'failed':
				return XCircle;
			default:
				return Circle;
		}
	}

	function getStatusClasses(status: string | undefined): string {
		switch (status) {
			case 'completed':
				return 'text-emerald-500';
			case 'in_progress':
				return 'text-blue-500 animate-spin';
			case 'failed':
				return 'text-destructive';
			default:
				return 'text-muted-foreground/50';
		}
	}
</script>

<div class="space-y-3">
	<div class="flex items-center justify-between">
		<span class="text-xs font-medium uppercase tracking-wider text-muted-foreground">Plan</span>
		{#if totalSteps > 0}
			<span class="text-[10px] tabular-nums text-muted-foreground"
				>{completed}/{totalSteps} ({progress}%)</span
			>
		{/if}
	</div>

	{#if totalSteps > 0}
		<div class="h-1.5 overflow-hidden rounded-full bg-muted">
			<div
				class="h-full rounded-full bg-primary transition-all duration-500"
				style:width="{progress}%"
			></div>
		</div>
	{/if}

	{#if steps.length > 0}
		<div class="space-y-1">
			{#each steps as step, i}
				{@const Icon = getStatusIcon(step.status)}
				<div class="flex items-start gap-2 py-1">
					<div class="mt-0.5 shrink-0">
						<Icon class="h-3.5 w-3.5 {getStatusClasses(step.status)}" />
					</div>
					<div class="min-w-0 flex-1">
						<p class="text-xs leading-snug text-foreground/80">
							{step.task || step.description || `Paso ${i + 1}`}
						</p>
						{#if step.status}
							<span class="mt-0.5 inline-block text-[10px] capitalize text-muted-foreground">
								{step.status.replace('_', ' ')}
							</span>
						{/if}
					</div>
				</div>
			{/each}
		</div>
	{:else}
		<div class="flex flex-col items-center justify-center py-6 text-center">
			<p class="text-xs text-muted-foreground">Sin plan activo</p>
		</div>
	{/if}
</div>
