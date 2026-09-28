<script lang="ts">
	/**
	 * Monitor flotante de SOLO LECTURA (esquina inferior derecha).
	 *
	 * Muestra métricas del sistema (CPU/RAM/disco/GPU/temperatura) y el estado
	 * del daemon y de los servicios del Manager. No tiene ningún control de
	 * gestión: crear/parar servicios, agentes, skills o tareas vive en sus
	 * propios modales del sidebar.
	 *
	 * Datos:
	 *  - métricas  -> `agentStore.sensors` (`GET /agent_sensors`, puerto 15000).
	 *  - servicios -> `GET /api/status` del web manager (puerto 12000, prefijo
	 *    `/api/mgr` detrás de nginx).
	 */
	import { Activity, ChevronDown, ChevronUp } from '@lucide/svelte';
	import { serviceBase } from '$lib/api';
	import { agentStore } from '$lib/stores/agentStore.svelte';

	const POLL_MS = 5000;

	type SensorMap = Record<string, unknown>;

	let collapsed = $state(true);
	let managerStatus = $state<Record<string, { running?: boolean; port?: number }> | null>(null);
	let managerReachable = $state(true);
	let timer: ReturnType<typeof setInterval> | undefined;

	const sensors = $derived((agentStore.sensors ?? {}) as SensorMap);

	function clamp(value: unknown): number {
		const n = Number(value);
		return Math.max(0, Math.min(100, Number.isFinite(n) ? n : 0));
	}

	function gb(value: unknown): string {
		const n = Number(value);
		return Number.isFinite(n) ? n.toFixed(1) : '?';
	}

	const cpu = $derived(clamp((sensors.cpu_percent as number) ?? (sensors.cpu as number) ?? 0));
	const memory = $derived(
		clamp(
			(sensors.memory as { percent?: number } | undefined)?.percent ??
				(sensors.memory_percent as number) ??
				0
		)
	);
	const memoryDetail = $derived(
		`${gb((sensors.memory as { used_gb?: number } | undefined)?.used_gb)} / ${gb(
			(sensors.memory as { total_gb?: number } | undefined)?.total_gb
		)} GB`
	);
	const disk = $derived(
		clamp(
			(sensors.disk as { percent?: number } | undefined)?.percent ??
				(sensors.disk_percent as number) ??
				0
		)
	);
	const diskDetail = $derived(
		`${gb((sensors.disk as { used_gb?: number } | undefined)?.used_gb)} / ${gb(
			(sensors.disk as { total_gb?: number } | undefined)?.total_gb
		)} GB`
	);
	const gpus = $derived(
		Array.isArray(sensors.gpu) ? (sensors.gpu as Array<Record<string, number | string>>) : []
	);
	const gpu = $derived(clamp(gpus[0]?.gpu_utilization));
	const gpuDetail = $derived(
		gpus.length
			? `VRAM ${gb((Number(gpus[0]?.memory_used_mb) || 0) / 1024)} / ${gb((Number(gpus[0]?.memory_total_mb) || 0) / 1024)} GB`
			: 'sin GPU NVIDIA'
	);
	const temperature = $derived(
		Number(
			Object.values((sensors.temperatures as Record<string, Array<{ current?: number }>>) ?? {})
				.flat()
				.find((entry) => Number.isFinite(Number(entry?.current)))?.current ?? 0
		)
	);

	const metrics = $derived<Array<{ label: string; value: number; detail?: string }>>([
		{ label: 'CPU', value: cpu },
		{ label: 'RAM', value: memory, detail: memoryDetail },
		{ label: 'Disco', value: disk, detail: diskDetail },
		{ label: 'GPU', value: gpu, detail: gpuDetail }
	]);

	// `agentStore.status` normaliza el daemon en `mode`: DAEMON / PAUSED / GENERAL.
	const agentMode = $derived(agentStore.status?.mode ?? 'GENERAL');
	const daemonRunning = $derived(agentMode === 'DAEMON');
	const daemonPaused = $derived(agentMode === 'PAUSED');
	const daemonLabel = $derived(daemonPaused ? 'pausado' : daemonRunning ? 'activo' : 'detenido');

	const endpoints = $derived(
		managerStatus
			? Object.entries(managerStatus)
					.filter(([, value]) => value && typeof value === 'object')
					.map(([name, value]) => ({ name, running: Boolean(value.running), port: value.port }))
			: []
	);

	async function refresh(): Promise<void> {
		await agentStore.fetchSensors();
		try {
			const res = await fetch(`${serviceBase('manager')}/api/status`);
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			managerStatus = await res.json();
			managerReachable = true;
		} catch {
			managerStatus = null;
			managerReachable = false;
		}
	}

	function stop(): void {
		if (timer) {
			clearInterval(timer);
			timer = undefined;
		}
	}

	function start(): void {
		stop();
		timer = setInterval(() => void refresh(), POLL_MS);
	}

	$effect(() => {
		if (collapsed) {
			stop();
			return;
		}
		void refresh();
		start();
		return () => stop();
	});
</script>

<div
	class="bg-background/95 border-border/60 fixed right-4 top-4 z-40 w-48 rounded-xl border shadow-lg backdrop-blur"
>
	<div class="flex items-center justify-between gap-2 px-3 py-2">
		<div class="flex items-center gap-2">
			<Activity class="text-muted-foreground h-3.5 w-3.5" />
			<span class="text-muted-foreground text-[11px] font-medium tracking-wider uppercase">
				Monitor
			</span>
		</div>
		<button
			type="button"
			class="text-muted-foreground hover:text-foreground"
			aria-label={collapsed ? 'Expandir monitor' : 'Contraer monitor'}
			onclick={() => (collapsed = !collapsed)}
		>
			{#if collapsed}
				<ChevronUp class="h-3.5 w-3.5" />
			{:else}
				<ChevronDown class="h-3.5 w-3.5" />
			{/if}
		</button>
	</div>

	{#if !collapsed}
		<div class="space-y-3 px-3 pb-3">
			<div class="space-y-2">
				{#each metrics as metric (metric.label)}
					<div>
						<div class="flex justify-between text-[11px]">
							<span class="text-muted-foreground">{metric.label}</span>
							<span class="font-mono">{Math.round(metric.value)}%</span>
						</div>
						<div class="bg-muted mt-1 h-1.5 overflow-hidden rounded-full">
							<div
								class="bg-primary h-full rounded-full transition-all"
								style:width="{metric.value}%"
							></div>
						</div>
						{#if metric.detail}
							<p class="text-muted-foreground mt-0.5 text-[10px]">{metric.detail}</p>
						{/if}
					</div>
				{/each}
				{#if temperature > 0}
					<div class="flex justify-between text-[10px]">
						<span class="text-muted-foreground">Temperatura</span>
						<span class="font-mono">{Math.round(temperature)} °C</span>
					</div>
				{/if}
			</div>

			<div class="border-border/50 border-t pt-2">
				<div class="flex items-center justify-between text-[11px]">
					<span class="text-muted-foreground">Daemon</span>
					<span class="flex items-center gap-1.5">
						<span
							class="h-1.5 w-1.5 rounded-full {daemonRunning
								? 'bg-emerald-500'
								: daemonPaused
									? 'bg-amber-500'
									: 'bg-muted-foreground'}"
						></span>
						{daemonLabel}
					</span>
				</div>

				{#if endpoints.length}
					<div class="mt-2 flex flex-wrap gap-1">
						{#each endpoints as endpoint (endpoint.name)}
							<span
								class="rounded border px-1.5 py-0.5 text-[10px] {endpoint.running
									? 'border-emerald-500/40 text-emerald-500'
									: 'border-border/60 text-muted-foreground'}"
								title={`${endpoint.name}${endpoint.port ? ` · puerto ${endpoint.port}` : ''}`}
							>
								{endpoint.name}
							</span>
						{/each}
					</div>
				{:else if !managerReachable}
					<p class="text-muted-foreground mt-2 text-[10px]">
						Servicios: sin conexión con el Manager (12000)
					</p>
				{/if}
			</div>
		</div>
	{/if}
</div>
