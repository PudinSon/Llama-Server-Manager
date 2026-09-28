<script lang="ts">
	import { Activity } from '@lucide/svelte';
	import { agentStore } from '$lib/stores/agentStore.svelte';

	let sensors = $derived(agentStore.sensors ?? {});
	let cpu = $derived(Number(sensors.cpu_percent ?? sensors.cpu ?? 0));
	let memory = $derived(
		Number(
			(sensors.memory as { percent: unknown | undefined} | undefined)?.percent ??
			(sensors.memory_percent ?? 0)
		)
	);
	let disk = $derived(
		Number((sensors.disk as { percent: unknown | undefined} | undefined)?.percent ?? sensors.disk_percent ?? 0)
	);
	let gpu = $derived(
		Number(
			(sensors.gpu as Array<{ gpu_utilization: unknown | undefined}> | undefined)?.[0]?.gpu_utilization ?? 0
		)
	);
	let temperature = $derived(
		Number(
			Object.values((sensors.temperatures as Record<string, Array<{ current: unknown | undefined}>> | undefined) ?? {})
				.flat()
				.find((entry) => Number.isFinite(Number(entry.current)))?.current ?? 0
		)
	);
	let metrics = $derived<[string, number][]>([
		['CPU', cpu],
		['RAM', memory],
		['Disco', disk],
		['GPU', gpu],
		['Temp', temperature]
	]);

	function clamp(value: number) {
		return Math.max(0, Math.min(100, Number.isFinite(value) ? value : 0));
	}
</script>

<div class="space-y-3">
	<div class="flex items-center justify-between">
		<span class="text-xs font-medium uppercase tracking-wider text-muted-foreground">Sensores</span>
		<Activity class="h-3.5 w-3.5 text-muted-foreground" />
	</div>
	<div class="grid grid-cols-2 gap-2">
		{#each metrics as [label, value]}
			<div class="rounded-lg border border-border/50 bg-muted/30 p-2.5">
				<div class="flex justify-between text-[11px]">
					<span class="text-muted-foreground">{label}</span>
					<span class="font-mono">{Math.round(clamp(value))}%</span>
				</div>
				<div class="mt-1.5 h-1.5 overflow-hidden rounded-full bg-muted">
					<div
						class="h-full rounded-full bg-primary transition-all"
						style:width="{clamp(value)}%"
					></div>
				</div>
			</div>
		{/each}
	</div>
</div>
