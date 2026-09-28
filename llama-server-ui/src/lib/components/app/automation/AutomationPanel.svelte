<script lang="ts">
	import { Workflow } from '@lucide/svelte';
	import { toast } from 'svelte-sonner';
	import { Button } from '$lib/components/ui/button';
	import {
		approveAutomationTask,
		deleteAutomationWebhook,
		deleteCron,
		deleteFileWatch,
		dispatchAutomationOnce,
		enqueueAutomationTask,
		getAutomationStatus,
		getExecutorStatus,
		listAutomationAudit,
		listAutomationTasks,
		listAutomationWebhooks,
		listCrons,
		listDispatcherHistory,
		listDispatchers,
		listFileWatches,
		pauseDispatcher,
		pauseExecutor,
		pollAutomationImapNow,
		rejectAutomationTask,
		retryAutomationTask,
		resumeDispatcher,
		resumeExecutor,
		rotateAutomationWebhookToken,
		runCron,
		saveAutomationWebhook,
		saveCron,
		saveFileWatch,
		scanFileWatch,
		webhookIngestUrl,
		type AutomationAuditEntry,
		type AutomationCron,
		type AutomationDispatcherCard,
		type AutomationExecutorStatus,
		type AutomationFileWatch,
		type AutomationHistoryEntry,
		type AutomationImapStatus,
		type AutomationTask,
		type AutomationTaskStatus,
		type AutomationWebhook
	} from '$lib/services/automation.service';

	interface Props {
		class?: string;
	}

	let { class: className }: Props = $props();

	const REFRESH_MS = 5000;
	const STATUS_FILTERS: Array<{ value: string; label: string }> = [
		{ value: '', label: 'Todas' },
		{ value: 'pending', label: 'Pendientes' },
		{ value: 'running', label: 'En curso' },
		{ value: 'awaiting_approval', label: 'Aprobación' },
		{ value: 'done', label: 'Hechas' },
		{ value: 'failed', label: 'Fallidas' }
	];

	let tasks = $state<AutomationTask[]>([]);
	let audit = $state<AutomationAuditEntry[]>([]);
	let imap = $state<AutomationImapStatus | null>(null);
	let dispatcher = $state<Awaited<ReturnType<typeof getAutomationStatus>>['dispatcher'] | null>(null);

	let loading = $state(false);
	let connected = $state(false);
	let statusFilter = $state('');
	let tab = $state<'dispatchers' | 'tasks' | 'audit' | 'webhooks' | 'filewatch' | 'cron'>(
		'tasks'
	);
	let busyId = $state<number | null>(null);
	let newPrompt = $state('');
	let newSource = $state('manual');
	let newRequiresApproval = $state(false);

	let webhooks = $state<AutomationWebhook[]>([]);
	let filewatches = $state<AutomationFileWatch[]>([]);
	let crons = $state<AutomationCron[]>([]);
	let dispatchers = $state<AutomationDispatcherCard[]>([]);
	let executor = $state<AutomationExecutorStatus | null>(null);
	let history = $state<AutomationHistoryEntry[]>([]);
	let historyKind = $state('');
	let busyDispatcher = $state('');
	let busyExecutor = $state(false);
	let filewatchForm = $state({
		name: '',
		path: '',
		pattern: '*',
		ignore: '',
		recursive: false,
		event: 'created',
		stable_seconds: 2,
		max_per_poll: 20,
		wake_prompt: '',
		agent_template: '',
		requires_approval: false,
		dedupe: true,
		enabled: true
	});
	let cronForm = $state({
		name: '',
		schedule: '',
		every_seconds: 0,
		wake_prompt: '',
		agent_template: '',
		requires_approval: false,
		dedupe: true,
		enabled: true
	});
	let webhookSecret = $state<{ name: string; token: string } | null>(null);
	let webhookForm = $state({
		name: '',
		wake_prompt: '',
		agent_template: '',
		requires_approval: false,
		allow_anonymous: false,
		priority: 0,
		dedupe: true,
		enabled: true
	});

	let pendingApprovals = $derived(tasks.filter((task) => task.status === 'awaiting_approval'));
	let retryable = $derived(
		tasks.filter((task) => task.status === 'failed' || task.status === 'cancelled')
	);

	async function refresh() {
		loading = true;
		try {
			const [status, listing] = await Promise.all([
				getAutomationStatus(),
				listAutomationTasks({ status: statusFilter || undefined, limit: 100 })
			]);
			dispatcher = status.dispatcher;
			imap = status.imap ?? null;
			tasks = listing.tasks;
			connected = true;
			if (tab === 'audit') audit = (await listAutomationAudit({ limit: 100 })).entries;
		} catch (error) {
			connected = false;
			toast.error(
				`Servicio de automatización no disponible: ${
					error instanceof Error ? error.message : String(error)
				}`
			);
		} finally {
			loading = false;
		}
	}

	async function loadWebhooks() {
		try {
			webhooks = (await listAutomationWebhooks()).webhooks;
		} catch (error) {
			toast.error(error instanceof Error ? error.message : String(error));
		}
	}

	function resetWebhookForm() {
		webhookForm = {
			name: '',
			wake_prompt: '',
			agent_template: '',
			requires_approval: false,
			allow_anonymous: false,
			priority: 0,
			dedupe: true,
			enabled: true
		};
	}

	async function handleSaveWebhook() {
		const name = webhookForm.name.trim().toLowerCase();
		if (!name) return;
		try {
			const result = await saveAutomationWebhook({ ...webhookForm, name });
			if (result.token) webhookSecret = { name, token: result.token };
			toast.success(`Webhook «${name}» guardado`);
			resetWebhookForm();
			await loadWebhooks();
		} catch (error) {
			toast.error(error instanceof Error ? error.message : String(error));
		}
	}

	async function handleRotateWebhook(name: string) {
		try {
			const result = await rotateAutomationWebhookToken(name);
			webhookSecret = { name, token: result.token };
			toast.success(`Token de «${name}» rotado`);
			await loadWebhooks();
		} catch (error) {
			toast.error(error instanceof Error ? error.message : String(error));
		}
	}

	async function handleEditWebhook(webhook: AutomationWebhook) {
		webhookForm = {
			name: webhook.name,
			wake_prompt: webhook.wake_prompt ?? '',
			agent_template: webhook.agent_template ?? '',
			requires_approval: webhook.requires_approval === true,
			allow_anonymous: webhook.allow_anonymous,
			priority: webhook.priority ?? 0,
			dedupe: webhook.dedupe ?? true,
			enabled: webhook.enabled
		};
	}

	async function handleDeleteWebhook(name: string) {
		try {
			await deleteAutomationWebhook(name);
			if (webhookSecret?.name === name) webhookSecret = null;
			toast.success(`Webhook «${name}» eliminado`);
			await loadWebhooks();
		} catch (error) {
			toast.error(error instanceof Error ? error.message : String(error));
		}
	}

	async function handleCopy(text: string, label: string) {
		try {
			await navigator.clipboard.writeText(text);
			toast.success(`${label} copiado`);
		} catch {
			toast.error('No se pudo copiar al portapapeles');
		}
	}

	async function loadDispatchers() {
		try {
			const [cards, feed, engine] = await Promise.all([
				listDispatchers(),
				listDispatcherHistory({ kind: historyKind || undefined, limit: 100 }),
				getExecutorStatus()
			]);
			dispatchers = cards.dispatchers;
			history = feed.entries;
			executor = engine.executor;
		} catch (error) {
			toast.error(error instanceof Error ? error.message : String(error));
		}
	}

	async function handleToggleExecutor() {
		if (!executor) return;
		busyExecutor = true;
		try {
			const result = executor.paused ? await resumeExecutor() : await pauseExecutor();
			executor = result.executor;
			toast.success(
				result.executor.paused
					? 'Ejecutor en pausa: la cola se acumula'
					: 'Ejecutor reanudado: el backlog sale al instante'
			);
		} catch (error) {
			toast.error(error instanceof Error ? error.message : String(error));
		} finally {
			busyExecutor = false;
		}
	}

	async function handleToggleDispatcher(card: AutomationDispatcherCard) {
		busyDispatcher = card.kind;
		try {
			const result = card.paused
				? await resumeDispatcher(card.kind)
				: await pauseDispatcher(card.kind);
			toast.success(
				result.dispatcher.paused
					? `${result.dispatcher.label} en pausa`
					: `${result.dispatcher.label} reanudado`
			);
			await loadDispatchers();
		} catch (error) {
			toast.error(error instanceof Error ? error.message : String(error));
		} finally {
			busyDispatcher = '';
		}
	}

	async function loadFileWatches() {
		try {
			filewatches = (await listFileWatches()).items;
		} catch (error) {
			toast.error(error instanceof Error ? error.message : String(error));
		}
	}

	async function loadCrons() {
		try {
			crons = (await listCrons()).items;
		} catch (error) {
			toast.error(error instanceof Error ? error.message : String(error));
		}
	}

	function resetFileWatchForm() {
		filewatchForm = {
			name: '',
			path: '',
			pattern: '*',
			ignore: '',
			recursive: false,
			event: 'created',
			stable_seconds: 2,
			max_per_poll: 20,
			wake_prompt: '',
			agent_template: '',
			requires_approval: false,
			dedupe: true,
			enabled: true
		};
	}

	function resetCronForm() {
		cronForm = {
			name: '',
			schedule: '',
			every_seconds: 0,
			wake_prompt: '',
			agent_template: '',
			requires_approval: false,
			dedupe: true,
			enabled: true
		};
	}

	async function handleSaveFileWatch() {
		const name = filewatchForm.name.trim().toLowerCase();
		if (!name) return;
		try {
			await saveFileWatch({ ...filewatchForm, name });
			toast.success(`Carpeta «${name}» guardada`);
			resetFileWatchForm();
			await loadFileWatches();
		} catch (error) {
			toast.error(error instanceof Error ? error.message : String(error));
		}
	}

	async function handleSaveCron() {
		const name = cronForm.name.trim().toLowerCase();
		if (!name) return;
		try {
			await saveCron({ ...cronForm, name });
			toast.success(`Horario «${name}» guardado`);
			resetCronForm();
			await loadCrons();
		} catch (error) {
			toast.error(error instanceof Error ? error.message : String(error));
		}
	}

	function handleEditFileWatch(item: AutomationFileWatch) {
		filewatchForm = {
			name: item.name,
			path: item.path,
			pattern: item.pattern ?? '*',
			ignore: item.ignore ?? '',
			recursive: item.recursive,
			event: item.event,
			stable_seconds: item.stable_seconds ?? 2,
			max_per_poll: item.max_per_poll ?? 20,
			wake_prompt: item.wake_prompt ?? '',
			agent_template: item.agent_template ?? '',
			requires_approval: item.requires_approval === true,
			dedupe: item.dedupe ?? true,
			enabled: item.enabled
		};
	}

	function handleEditCron(item: AutomationCron) {
		cronForm = {
			name: item.name,
			schedule: item.schedule ?? '',
			every_seconds: item.every_seconds ?? 0,
			wake_prompt: item.wake_prompt ?? '',
			agent_template: item.agent_template ?? '',
			requires_approval: item.requires_approval === true,
			dedupe: item.dedupe ?? true,
			enabled: item.enabled
		};
	}

	async function handleScanFileWatch(name: string) {
		try {
			const result = await scanFileWatch(name);
			toast.success(
				result.enqueued
					? `${result.enqueued} archivo(s) encolado(s)`
					: 'Sin archivos nuevos'
			);
			await loadFileWatches();
		} catch (error) {
			toast.error(error instanceof Error ? error.message : String(error));
		}
	}

	async function handleDeleteFileWatch(name: string) {
		try {
			await deleteFileWatch(name);
			toast.success(`Carpeta «${name}» eliminada`);
			await loadFileWatches();
		} catch (error) {
			toast.error(error instanceof Error ? error.message : String(error));
		}
	}

	async function handleRunCron(name: string) {
		try {
			const result = await runCron(name);
			toast.success(`Rutina «${name}» lanzada (tarea #${result.task_id})`);
			await loadCrons();
		} catch (error) {
			toast.error(error instanceof Error ? error.message : String(error));
		}
	}

	async function handleDeleteCron(name: string) {
		try {
			await deleteCron(name);
			toast.success(`Horario «${name}» eliminado`);
			await loadCrons();
		} catch (error) {
			toast.error(error instanceof Error ? error.message : String(error));
		}
	}

	async function loadAudit() {
		try {
			audit = (await listAutomationAudit({ limit: 100 })).entries;
		} catch (error) {
			toast.error(error instanceof Error ? error.message : String(error));
		}
	}

	async function run(action: () => Promise<unknown>, successMessage: string) {
		try {
			await action();
			toast.success(successMessage);
			await refresh();
		} catch (error) {
			toast.error(error instanceof Error ? error.message : String(error));
		}
	}

	async function handleRetry(task: AutomationTask) {
		busyId = task.id;
		await run(() => retryAutomationTask(task.id), `Tarea #${task.id} reencolada`);
		busyId = null;
	}

	async function handleApprove(task: AutomationTask) {
		if (!task.approval_token) return;
		busyId = task.id;
		await run(() => approveAutomationTask(task.approval_token!), `Tarea #${task.id} aprobada`);
		busyId = null;
	}

	async function handleReject(task: AutomationTask) {
		if (!task.approval_token) return;
		busyId = task.id;
		await run(() => rejectAutomationTask(task.approval_token!), `Tarea #${task.id} rechazada`);
		busyId = null;
	}

	async function handleDispatchOnce() {
		await run(() => dispatchAutomationOnce(), 'Ciclo de reparto ejecutado');
	}

	async function handleEnqueue() {
		const wake_prompt = newPrompt.trim();
		if (!wake_prompt) return;
		await run(
			() =>
				enqueueAutomationTask({
					source: newSource.trim() || 'manual',
					wake_prompt,
					requires_approval: newRequiresApproval
				}),
			'Tarea encolada'
		);
		newPrompt = '';
	}

	async function handlePollImap() {
		await run(() => pollAutomationImapNow(), 'Sondeo IMAP solicitado');
	}

	function formatTime(epoch?: number | null): string {
		if (!epoch) return '—';
		return new Date(epoch * 1000).toLocaleString();
	}

	function statusClasses(status: AutomationTaskStatus): string {
		switch (status) {
			case 'done':
				return 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400';
			case 'failed':
				return 'bg-destructive/15 text-destructive';
			case 'awaiting_approval':
				return 'bg-amber-500/15 text-amber-600 dark:text-amber-400';
			case 'running':
				return 'bg-blue-500/15 text-blue-600 dark:text-blue-400';
			case 'cancelled':
				return 'bg-muted text-muted-foreground';
			default:
				return 'bg-secondary text-secondary-foreground';
		}
	}

	$effect(() => {
		void refresh();
		const timer = setInterval(() => void refresh(), REFRESH_MS);
		return () => clearInterval(timer);
	});

	$effect(() => {
		if (tab === 'dispatchers') void loadDispatchers();
		if (tab === 'audit') void loadAudit();
		if (tab === 'webhooks') void loadWebhooks();
		if (tab === 'filewatch') void loadFileWatches();
		if (tab === 'cron') void loadCrons();
	});
</script>

<div class={['mx-auto w-full p-4 md:p-8', className]}>
	<header class="mb-6 flex flex-wrap items-center justify-between gap-3">
		<div class="flex items-center gap-2">
			<Workflow class="h-5 w-5 md:h-6 md:w-6" />
			<h1 class="text-lg font-semibold md:text-2xl">Automatización 24/7</h1>
			<span
				class={[
					'rounded-full px-2 py-0.5 text-xs font-medium',
					connected
						? 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400'
						: 'bg-destructive/15 text-destructive'
				]}
			>
				{connected ? 'activo' : 'sin conexión'}
			</span>
		</div>

		<div class="flex items-center gap-2">
			<Button variant="outline" size="sm" disabled={loading} onclick={() => void refresh()}>
				{loading ? 'Actualizando…' : 'Actualizar'}
			</Button>
			<Button size="sm" onclick={handleDispatchOnce}>Ejecutar una ahora</Button>
		</div>
	</header>

	{#if dispatcher}
		<section class="mb-6 grid grid-cols-2 gap-3 md:grid-cols-6">
			{#each [
				{ label: 'Pendientes', value: dispatcher.queue.pending },
				{ label: 'En curso', value: dispatcher.queue.running },
				{ label: 'Aprobación', value: dispatcher.queue.awaiting_approval },
				{ label: 'Hechas', value: dispatcher.queue.done },
				{ label: 'Fallidas', value: dispatcher.queue.failed },
				{ label: 'Reintentos', value: dispatcher.retried }
			] as card (card.label)}
				<div class="rounded-lg border border-border p-3">
					<p class="text-xs text-muted-foreground">{card.label}</p>
					<p class="text-xl font-semibold">{card.value}</p>
				</div>
			{/each}
		</section>

		<p class="mb-6 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
			{#if dispatcher.paused}
				<button
					type="button"
					class="rounded-full bg-amber-500/15 px-2 py-0.5 font-medium text-amber-600 dark:text-amber-400"
					onclick={() => (tab = 'dispatchers')}
				>
					⏸ ejecutor en pausa · {dispatcher.backlog?.total ?? 0} represadas
				</button>
			{/if}
			<span>
				{dispatcher.num_workers} worker(s) · reintentos hasta {dispatcher.max_retries} · base
				{dispatcher.retry_base_seconds}s · exige aprobación:
				{dispatcher.require_approval ? 'sí' : 'no'}
			</span>
		</p>
	{/if}

	{#if pendingApprovals.length > 0}
		<section class="mb-6 rounded-lg border border-amber-500/40 p-4">
			<h2 class="mb-3 text-sm font-semibold">
				🔐 {pendingApprovals.length} tarea(s) esperando aprobación
			</h2>
			<ul class="space-y-2">
				{#each pendingApprovals as task (task.id)}
					<li class="flex flex-wrap items-center justify-between gap-2 text-sm">
						<span class="truncate">#{task.id} · {task.wake_prompt || task.source}</span>
						<span class="flex gap-2">
							<Button size="sm" disabled={busyId === task.id} onclick={() => handleApprove(task)}>
								Aprobar
							</Button>
							<Button
								size="sm"
								variant="outline"
								disabled={busyId === task.id}
								onclick={() => handleReject(task)}
							>
								Rechazar
							</Button>
						</span>
					</li>
				{/each}
			</ul>
		</section>
	{/if}

	<section class="mb-6 rounded-lg border border-border p-4">
		<h2 class="mb-3 text-sm font-semibold">Encolar una tarea</h2>
		<div class="flex flex-wrap items-end gap-2">
			<label class="flex flex-col gap-1 text-xs">
				<span class="text-muted-foreground">Origen</span>
				<input
					class="rounded-md border border-border bg-background px-2 py-1.5 text-sm"
					bind:value={newSource}
					placeholder="manual"
				/>
			</label>
			<label class="flex min-w-64 flex-1 flex-col gap-1 text-xs">
				<span class="text-muted-foreground">Prompt</span>
				<input
					class="rounded-md border border-border bg-background px-2 py-1.5 text-sm"
					bind:value={newPrompt}
					placeholder="Describe la tarea a ejecutar"
				/>
			</label>
			<label class="flex items-center gap-2 text-xs">
				<input type="checkbox" bind:checked={newRequiresApproval} />
				<span class="text-muted-foreground">Requiere aprobación</span>
			</label>
			<Button size="sm" disabled={!newPrompt.trim()} onclick={handleEnqueue}>Encolar</Button>
		</div>
	</section>

	<div class="mb-3 flex flex-wrap items-center gap-2">
		<Button
			variant={tab === 'dispatchers' ? 'default' : 'outline'}
			size="sm"
			onclick={() => (tab = 'dispatchers')}
		>
			Disparadores
		</Button>
		<Button variant={tab === 'tasks' ? 'default' : 'outline'} size="sm" onclick={() => (tab = 'tasks')}>
			Tareas
		</Button>
		<Button variant={tab === 'audit' ? 'default' : 'outline'} size="sm" onclick={() => (tab = 'audit')}>
			Auditoría
		</Button>
		<Button
			variant={tab === 'webhooks' ? 'default' : 'outline'}
			size="sm"
			onclick={() => (tab = 'webhooks')}
		>
			Webhooks
		</Button>
		<Button
			variant={tab === 'filewatch' ? 'default' : 'outline'}
			size="sm"
			onclick={() => (tab = 'filewatch')}
		>
			FileWatch
		</Button>
		<Button
			variant={tab === 'cron' ? 'default' : 'outline'}
			size="sm"
			onclick={() => (tab = 'cron')}
		>
			Cron
		</Button>

		{#if tab === 'tasks'}
			<select
				class="ml-auto rounded-md border border-border bg-background px-2 py-1.5 text-sm"
				bind:value={statusFilter}
			>
				{#each STATUS_FILTERS as filter (filter.value)}
					<option value={filter.value}>{filter.label}</option>
				{/each}
			</select>
		{/if}
	</div>

	{#if tab === 'tasks'}
		{#if tasks.length === 0}
			<p class="rounded-lg border border-dashed border-border p-8 text-center text-sm text-muted-foreground">
				No hay tareas para este filtro.
			</p>
		{:else}
			<div class="overflow-x-auto rounded-lg border border-border">
				<table class="w-full text-left text-sm">
					<thead class="bg-muted/50 text-xs uppercase text-muted-foreground">
						<tr>
							<th class="px-3 py-2">#</th>
							<th class="px-3 py-2">Estado</th>
							<th class="px-3 py-2">Origen</th>
							<th class="px-3 py-2">Tarea</th>
							<th class="px-3 py-2">Intentos</th>
							<th class="px-3 py-2">Creada</th>
							<th class="px-3 py-2"></th>
						</tr>
					</thead>
					<tbody>
						{#each tasks as task (task.id)}
							<tr class="border-t border-border align-top">
								<td class="px-3 py-2 font-mono text-xs">{task.id}</td>
								<td class="px-3 py-2">
									<span class="rounded-full px-2 py-0.5 text-xs {statusClasses(task.status)}">
										{task.status}
									</span>
								</td>
								<td class="px-3 py-2">{task.source}</td>
								<td class="max-w-md px-3 py-2">
									<p class="truncate">{task.wake_prompt || '—'}</p>
									{#if task.error}
										<p class="mt-1 text-xs text-destructive">{task.error}</p>
									{/if}
								</td>
								<td class="px-3 py-2 text-xs">
									{task.attempts}
									{#if task.next_attempt_at && task.next_attempt_at * 1000 > Date.now()}
										<span class="text-muted-foreground">
											· {formatTime(task.next_attempt_at)}
										</span>
									{/if}
								</td>
								<td class="px-3 py-2 text-xs text-muted-foreground">{formatTime(task.created_at)}</td>
								<td class="px-3 py-2 text-right">
									{#if task.status === 'awaiting_approval'}
										<span class="flex justify-end gap-2">
											<Button size="sm" disabled={busyId === task.id} onclick={() => handleApprove(task)}>
												Aprobar
											</Button>
											<Button
												size="sm"
												variant="outline"
												disabled={busyId === task.id}
												onclick={() => handleReject(task)}
											>
												Rechazar
											</Button>
										</span>
									{:else if task.status === 'failed' || task.status === 'cancelled'}
										<Button
											size="sm"
											variant="outline"
											disabled={busyId === task.id}
											onclick={() => handleRetry(task)}
										>
											Reintentar
										</Button>
									{/if}
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>

			{#if retryable.length > 0}
				<p class="mt-3 text-xs text-muted-foreground">
					{retryable.length} tarea(s) fallida(s)/cancelada(s) se pueden reencolar manualmente.
				</p>
			{/if}
		{/if}
	{:else if tab === 'webhooks'}
		<div class="grid gap-6 lg:grid-cols-2">
			<section class="rounded-lg border border-border p-4">
				<h2 class="mb-3 text-sm font-semibold">Webhooks registrados ({webhooks.length})</h2>
				{#if webhooks.length === 0}
					<p class="text-sm text-muted-foreground">Aún no hay webhooks.</p>
				{:else}
					<ul class="divide-y divide-border">
						{#each webhooks as webhook (webhook.name)}
							<li class="py-3">
								<div class="flex flex-wrap items-center gap-2">
									<span class="font-mono text-sm">{webhook.name}</span>
									<span
										class={[
											'rounded-full px-2 py-0.5 text-xs',
											webhook.enabled
												? 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400'
												: 'bg-muted text-muted-foreground'
										]}
									>
										{webhook.enabled ? 'activo' : 'deshabilitado'}
									</span>
									{#if webhook.allow_anonymous}
										<span class="rounded-full bg-amber-500/15 px-2 py-0.5 text-xs text-amber-600 dark:text-amber-400">
											anónimo (solo loopback)
										</span>
									{:else if webhook.has_token}
										<span class="rounded-full bg-secondary px-2 py-0.5 text-xs">con token</span>
									{/if}
									{#if webhook.requires_approval}
										<span class="rounded-full bg-secondary px-2 py-0.5 text-xs">requiere aprobación</span>
									{/if}
								</div>
								<p class="mt-1 truncate font-mono text-xs text-muted-foreground">
									POST {webhookIngestUrl(webhook.name)}
								</p>
								<div class="mt-2 flex flex-wrap gap-2">
									<Button
										size="sm"
										variant="outline"
										onclick={() => handleCopy(webhookIngestUrl(webhook.name), 'URL del webhook')}
									>
										Copiar URL
									</Button>
									<Button size="sm" variant="outline" onclick={() => handleEditWebhook(webhook)}>
										Editar
									</Button>
									<Button size="sm" variant="outline" onclick={() => handleRotateWebhook(webhook.name)}>
										Rotar token
									</Button>
									<Button size="sm" variant="outline" onclick={() => handleDeleteWebhook(webhook.name)}>
										Eliminar
									</Button>
								</div>
							</li>
						{/each}
					</ul>
				{/if}

				{#if webhookSecret}
					<div class="mt-4 rounded-md border border-amber-500/40 p-3 text-xs">
						<p class="font-semibold">Token de «{webhookSecret.name}»</p>
						<p class="mt-1 text-muted-foreground">
							Se muestra una sola vez. Cópialo ahora en el sistema que llamará al webhook.
						</p>
						<p class="mt-2 break-all font-mono">{webhookSecret.token}</p>
						<Button
							class="mt-2"
							size="sm"
							onclick={() => handleCopy(webhookSecret!.token, 'Token')}
						>
							Copiar token
						</Button>
					</div>
				{/if}
			</section>

			<section class="rounded-lg border border-border p-4">
				<h2 class="mb-3 text-sm font-semibold">
					{webhookForm.name.trim() ? `Editar «${webhookForm.name.trim().toLowerCase()}»` : 'Nuevo webhook'}
				</h2>
				<div class="grid gap-3">
					<label class="flex flex-col gap-1 text-xs">
						<span class="text-muted-foreground">Nombre (slug en la URL)</span>
						<input
							class="rounded-md border border-border bg-background px-2 py-1.5 text-sm"
							bind:value={webhookForm.name}
							placeholder="github"
						/>
					</label>
					<label class="flex flex-col gap-1 text-xs">
						<span class="text-muted-foreground">
							Prompt (admite campos del cuerpo, p. ej. {"{action}"})
						</span>
						<textarea
							class="rounded-md border border-border bg-background px-2 py-1.5 text-sm"
							rows="3"
							bind:value={webhookForm.wake_prompt}
							placeholder={'Procesa el evento {action} del repositorio {repository.name}'}
						></textarea>
					</label>
					<label class="flex flex-col gap-1 text-xs">
						<span class="text-muted-foreground">Instrucciones fijas del agente (opcional)</span>
						<textarea
							class="rounded-md border border-border bg-background px-2 py-1.5 text-sm"
							rows="2"
							bind:value={webhookForm.agent_template}
						></textarea>
					</label>
					<label class="flex items-center gap-2 text-xs">
						<input type="checkbox" bind:checked={webhookForm.enabled} />
						<span class="text-muted-foreground">Habilitado</span>
					</label>
					<label class="flex items-center gap-2 text-xs">
						<input type="checkbox" bind:checked={webhookForm.requires_approval} />
						<span class="text-muted-foreground">
							Exigir aprobación antes de ejecutar
						</span>
					</label>
					<label class="flex items-center gap-2 text-xs">
						<input type="checkbox" bind:checked={webhookForm.allow_anonymous} />
						<span class="text-muted-foreground">
							Permitir sin token (solo desde loopback)
						</span>
					</label>
					<label class="flex items-center gap-2 text-xs">
						<input type="checkbox" bind:checked={webhookForm.dedupe} />
						<span class="text-muted-foreground">
							Deduplicar por identificador de entrega
						</span>
					</label>
					<div class="flex gap-2">
						<Button size="sm" disabled={!webhookForm.name.trim()} onclick={handleSaveWebhook}>
							Guardar
						</Button>
						<Button size="sm" variant="outline" onclick={resetWebhookForm}>Limpiar</Button>
					</div>
				</div>
			</section>
		</div>
	{:else if tab === 'filewatch'}
		<div class="grid gap-6 lg:grid-cols-2">
			<section class="rounded-lg border border-border p-4">
				<h2 class="mb-3 text-sm font-semibold">Carpetas vigiladas ({filewatches.length})</h2>
				{#if filewatches.length === 0}
					<p class="text-sm text-muted-foreground">Aún no hay carpetas vigiladas.</p>
				{:else}
					<ul class="divide-y divide-border">
						{#each filewatches as item (item.name)}
							<li class="py-3">
								<div class="flex flex-wrap items-center gap-2">
									<span class="font-mono text-sm">{item.name}</span>
									<span
										class={[
											'rounded-full px-2 py-0.5 text-xs',
											item.enabled
												? 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400'
												: 'bg-muted text-muted-foreground'
										]}
									>
										{item.enabled ? 'activo' : 'pausado'}
									</span>
									<span class="rounded-full bg-secondary px-2 py-0.5 text-xs">{item.event}</span>
									{#if item.requires_approval}
										<span class="rounded-full bg-secondary px-2 py-0.5 text-xs">aprobación</span>
									{/if}
								</div>
								<p class="mt-1 truncate font-mono text-xs text-muted-foreground">
									{item.path} · {item.pattern}{item.recursive ? ' (recursivo)' : ''}
								</p>
								<p class="mt-1 text-xs text-muted-foreground">
									Último aviso: {item.last_run ? formatTime(item.last_run) : '—'}
								</p>
								<div class="mt-2 flex flex-wrap gap-2">
									<Button size="sm" variant="outline" onclick={() => handleScanFileWatch(item.name)}>
										Escanear ahora
									</Button>
									<Button size="sm" variant="outline" onclick={() => handleEditFileWatch(item)}>
										Editar
									</Button>
									<Button size="sm" variant="outline" onclick={() => handleDeleteFileWatch(item.name)}>
										Eliminar
									</Button>
								</div>
							</li>
						{/each}
					</ul>
				{/if}
			</section>

			<section class="rounded-lg border border-border p-4">
				<h2 class="mb-3 text-sm font-semibold">
					{filewatchForm.name.trim()
						? `Editar «${filewatchForm.name.trim().toLowerCase()}»`
						: 'Vigilar una carpeta'}
				</h2>
				<div class="grid gap-3">
					<label class="flex flex-col gap-1 text-xs">
						<span class="text-muted-foreground">Nombre</span>
						<input
							class="rounded-md border border-border bg-background px-2 py-1.5 text-sm"
							bind:value={filewatchForm.name}
							placeholder="facturas"
						/>
					</label>
					<label class="flex flex-col gap-1 text-xs">
						<span class="text-muted-foreground">
							Carpeta (absoluta o relativa al workspace)
						</span>
						<input
							class="rounded-md border border-border bg-background px-2 py-1.5 text-sm"
							bind:value={filewatchForm.path}
							placeholder="informes/facturas"
						/>
					</label>
					<div class="grid grid-cols-2 gap-3">
						<label class="flex flex-col gap-1 text-xs">
							<span class="text-muted-foreground">Patrones (glob, separados por coma)</span>
							<input
								class="rounded-md border border-border bg-background px-2 py-1.5 text-sm"
								bind:value={filewatchForm.pattern}
							/>
						</label>
						<label class="flex flex-col gap-1 text-xs">
							<span class="text-muted-foreground">Ignorar (glob, separados por coma)</span>
							<input
								class="rounded-md border border-border bg-background px-2 py-1.5 text-sm"
								bind:value={filewatchForm.ignore}
								placeholder="*.tmp, ~*"
							/>
						</label>
					</div>
					<div class="grid grid-cols-3 gap-3">
						<label class="flex flex-col gap-1 text-xs">
							<span class="text-muted-foreground">Evento</span>
							<select
								class="rounded-md border border-border bg-background px-2 py-1.5 text-sm"
								bind:value={filewatchForm.event}
							>
								<option value="created">creado</option>
								<option value="modified">modificado</option>
								<option value="any">cualquiera</option>
							</select>
						</label>
						<label class="flex flex-col gap-1 text-xs">
							<span class="text-muted-foreground">Estable (s)</span>
							<input
								type="number"
								min="0"
								class="rounded-md border border-border bg-background px-2 py-1.5 text-sm"
								bind:value={filewatchForm.stable_seconds}
							/>
						</label>
						<label class="flex flex-col gap-1 text-xs">
							<span class="text-muted-foreground">Máx. por vuelta</span>
							<input
								type="number"
								min="1"
								class="rounded-md border border-border bg-background px-2 py-1.5 text-sm"
								bind:value={filewatchForm.max_per_poll}
							/>
						</label>
					</div>
					<label class="flex flex-col gap-1 text-xs">
						<span class="text-muted-foreground">
							Prompt (admite {"{file}"}, {"{name}"}, {"{path}"}, {"{size}"})
						</span>
						<textarea
							class="rounded-md border border-border bg-background px-2 py-1.5 text-sm"
							rows="3"
							bind:value={filewatchForm.wake_prompt}
							placeholder={'Lee el archivo {file} y resume su contenido'}
						></textarea>
					</label>
					<label class="flex flex-col gap-1 text-xs">
						<span class="text-muted-foreground">Instrucciones fijas del agente (opcional)</span>
						<textarea
							class="rounded-md border border-border bg-background px-2 py-1.5 text-sm"
							rows="2"
							bind:value={filewatchForm.agent_template}
						></textarea>
					</label>
					<div class="flex flex-wrap gap-4">
						<label class="flex items-center gap-2 text-xs">
							<input type="checkbox" bind:checked={filewatchForm.recursive} />
							<span class="text-muted-foreground">Subcarpetas</span>
						</label>
						<label class="flex items-center gap-2 text-xs">
							<input type="checkbox" bind:checked={filewatchForm.requires_approval} />
							<span class="text-muted-foreground">Exigir aprobación</span>
						</label>
						<label class="flex items-center gap-2 text-xs">
							<input type="checkbox" bind:checked={filewatchForm.dedupe} />
							<span class="text-muted-foreground">No repetir el mismo archivo</span>
						</label>
						<label class="flex items-center gap-2 text-xs">
							<input type="checkbox" bind:checked={filewatchForm.enabled} />
							<span class="text-muted-foreground">Habilitada</span>
						</label>
					</div>
					<div class="flex gap-2">
						<Button
							size="sm"
							disabled={!filewatchForm.name.trim() || !filewatchForm.path.trim()}
							onclick={handleSaveFileWatch}
						>
							Guardar
						</Button>
						<Button size="sm" variant="outline" onclick={resetFileWatchForm}>Limpiar</Button>
					</div>
				</div>
			</section>
		</div>
	{:else if tab === 'cron'}
		<div class="grid gap-6 lg:grid-cols-2">
			<section class="rounded-lg border border-border p-4">
				<h2 class="mb-3 text-sm font-semibold">Horarios ({crons.length})</h2>
				{#if crons.length === 0}
					<p class="text-sm text-muted-foreground">Aún no hay tareas programadas.</p>
				{:else}
					<ul class="divide-y divide-border">
						{#each crons as item (item.name)}
							<li class="py-3">
								<div class="flex flex-wrap items-center gap-2">
									<span class="font-mono text-sm">{item.name}</span>
									<span
										class={[
											'rounded-full px-2 py-0.5 text-xs',
											item.enabled
												? 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400'
												: 'bg-muted text-muted-foreground'
										]}
									>
										{item.enabled ? 'activo' : 'pausado'}
									</span>
									<span class="rounded-full bg-secondary px-2 py-0.5 font-mono text-xs">
										{item.schedule || `cada ${item.every_seconds}s`}
									</span>
									{#if item.requires_approval}
										<span class="rounded-full bg-secondary px-2 py-0.5 text-xs">aprobación</span>
									{/if}
								</div>
								<p class="mt-1 text-xs text-muted-foreground">
									Próximo: {item.next_run ?? '—'} · Último: {item.last_run
										? formatTime(item.last_run)
										: '—'}
								</p>
								<div class="mt-2 flex flex-wrap gap-2">
									<Button size="sm" variant="outline" onclick={() => handleRunCron(item.name)}>
										Ejecutar ahora
									</Button>
									<Button size="sm" variant="outline" onclick={() => handleEditCron(item)}>
										Editar
									</Button>
									<Button size="sm" variant="outline" onclick={() => handleDeleteCron(item.name)}>
										Eliminar
									</Button>
								</div>
							</li>
						{/each}
					</ul>
				{/if}
			</section>

			<section class="rounded-lg border border-border p-4">
				<h2 class="mb-3 text-sm font-semibold">
					{cronForm.name.trim() ? `Editar «${cronForm.name.trim().toLowerCase()}»` : 'Nueva rutina'}
				</h2>
				<div class="grid gap-3">
					<label class="flex flex-col gap-1 text-xs">
						<span class="text-muted-foreground">Nombre</span>
						<input
							class="rounded-md border border-border bg-background px-2 py-1.5 text-sm"
							bind:value={cronForm.name}
							placeholder="resumen-matutino"
						/>
					</label>
					<div class="grid grid-cols-2 gap-3">
						<label class="flex flex-col gap-1 text-xs">
							<span class="text-muted-foreground">Horario cron (min hora dom mes dow)</span>
							<input
								class="rounded-md border border-border bg-background px-2 py-1.5 font-mono text-sm"
								bind:value={cronForm.schedule}
								placeholder="0 8 * * mon-fri"
							/>
						</label>
						<label class="flex flex-col gap-1 text-xs">
							<span class="text-muted-foreground">o cada N segundos</span>
							<input
								type="number"
								min="0"
								class="rounded-md border border-border bg-background px-2 py-1.5 text-sm"
								bind:value={cronForm.every_seconds}
							/>
						</label>
					</div>
					<p class="text-xs text-muted-foreground">
						Admite `@daily`, `@hourly`, `*/15 * * * *`, `mon-fri`… Usa uno de los dos campos.
					</p>
					<label class="flex flex-col gap-1 text-xs">
						<span class="text-muted-foreground">
							Prompt (admite {"{date}"}, {"{time}"}, {"{weekday}"})
						</span>
						<textarea
							class="rounded-md border border-border bg-background px-2 py-1.5 text-sm"
							rows="3"
							bind:value={cronForm.wake_prompt}
							placeholder={'Prepara el resumen del {weekday} {date}'}
						></textarea>
					</label>
					<label class="flex flex-col gap-1 text-xs">
						<span class="text-muted-foreground">Instrucciones fijas del agente (opcional)</span>
						<textarea
							class="rounded-md border border-border bg-background px-2 py-1.5 text-sm"
							rows="2"
							bind:value={cronForm.agent_template}
						></textarea>
					</label>
					<div class="flex flex-wrap gap-4">
						<label class="flex items-center gap-2 text-xs">
							<input type="checkbox" bind:checked={cronForm.requires_approval} />
							<span class="text-muted-foreground">Exigir aprobación</span>
						</label>
						<label class="flex items-center gap-2 text-xs">
							<input type="checkbox" bind:checked={cronForm.dedupe} />
							<span class="text-muted-foreground">No repetir el mismo minuto</span>
						</label>
						<label class="flex items-center gap-2 text-xs">
							<input type="checkbox" bind:checked={cronForm.enabled} />
							<span class="text-muted-foreground">Habilitado</span>
						</label>
					</div>
					<div class="flex gap-2">
						<Button
							size="sm"
							disabled={!cronForm.name.trim() ||
								(!cronForm.schedule.trim() && !cronForm.every_seconds)}
							onclick={handleSaveCron}
						>
							Guardar
						</Button>
						<Button size="sm" variant="outline" onclick={resetCronForm}>Limpiar</Button>
					</div>
				</div>
			</section>
		</div>
	{:else if tab === 'dispatchers'}
		{#if executor}
			<section
				class={[
					'mb-4 rounded-lg border p-4',
					executor.paused ? 'border-amber-500/40 bg-amber-500/5' : 'border-border'
				]}
			>
				<div class="flex flex-wrap items-center justify-between gap-3">
					<div class="flex items-center gap-2">
						<span class="text-sm font-semibold">Ejecutor de la cola</span>
						<span
							class={[
								'rounded-full px-2 py-0.5 text-xs font-medium',
								executor.paused
									? 'bg-amber-500/15 text-amber-600 dark:text-amber-400'
									: executor.running
										? 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400'
										: 'bg-muted text-muted-foreground'
							]}
						>
							{executor.paused ? 'en pausa' : executor.running ? 'activo' : 'detenido'}
						</span>
						<span class="text-xs text-muted-foreground">
							{executor.num_workers} worker(s)
						</span>
					</div>
					<Button
						size="sm"
						variant={executor.paused ? 'default' : 'outline'}
						disabled={busyExecutor}
						onclick={handleToggleExecutor}
					>
						{executor.paused ? 'Reanudar el ejecutor' : 'Pausar el ejecutor'}
					</Button>
				</div>

				<div class="mt-3 grid grid-cols-2 gap-3 md:grid-cols-5">
					<div>
						<p class="text-xs text-muted-foreground">Represadas</p>
						<p class="text-xl font-semibold">{executor.backlog.total}</p>
					</div>
					<div>
						<p class="text-xs text-muted-foreground">Listas</p>
						<p class="text-xl font-semibold">{executor.backlog.ready}</p>
					</div>
					<div>
						<p class="text-xs text-muted-foreground">En backoff</p>
						<p class="text-xl font-semibold">{executor.backlog.waiting_backoff}</p>
					</div>
					<div>
						<p class="text-xs text-muted-foreground">Aprobación</p>
						<p class="text-xl font-semibold">{executor.backlog.awaiting_approval}</p>
					</div>
					<div>
						<p class="text-xs text-muted-foreground">Procesadas / fallidas</p>
						<p class="text-xl font-semibold">
							{executor.processed} / {executor.failed}
						</p>
					</div>
				</div>

				<p class="mt-3 text-xs text-muted-foreground">
					{#if executor.paused}
						Los workers han dejado de reclamar tareas (la que estuviera en curso termina).
						Al reanudar salen todas al instante.
						{#if executor.paused_reason}· Motivo: {executor.paused_reason}{/if}
					{:else}
						Consume la cola: los disparadores solo la alimentan. Reintentos hasta
						{executor.max_retries}.
					{/if}
					{#if executor.current}
						· En curso: #{executor.current.id} ({executor.current.source})
					{/if}
				</p>
			</section>
		{/if}

		<section class="mb-6 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
			{#each dispatchers as card (card.kind)}
				<div class="flex flex-col gap-2 rounded-lg border border-border p-4">
					<div class="flex items-center justify-between gap-2">
						<span class="text-sm font-semibold">{card.label}</span>
						<span
							class={[
								'rounded-full px-2 py-0.5 text-xs font-medium',
								card.paused
									? 'bg-amber-500/15 text-amber-600 dark:text-amber-400'
									: card.running
										? 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400'
										: 'bg-muted text-muted-foreground'
							]}
						>
							{card.paused ? 'en pausa' : card.running ? 'activo' : 'detenido'}
						</span>
					</div>

					<dl class="grid grid-cols-3 gap-1 text-xs">
						<div>
							<dt class="text-muted-foreground">{card.kind === 'imap' ? 'Correos' : 'Entradas'}</dt>
							<dd class="font-semibold">{card.total}</dd>
						</div>
						<div>
							<dt class="text-muted-foreground">Encoladas</dt>
							<dd class="font-semibold">{card.enqueued_total}</dd>
						</div>
						<div>
							<dt class="text-muted-foreground">Rechazadas</dt>
							<dd class="font-semibold">{card.rejected_total}</dd>
						</div>
					</dl>

					<p class="text-xs text-muted-foreground">
						Último evento: {card.last_event?.at ? formatTime(card.last_event.at) : '—'}
						{#if card.last_event?.task_id}· tarea #{card.last_event.task_id}{/if}
					</p>

					{#if card.paused && card.reason}
						<p class="text-xs text-amber-600 dark:text-amber-400">Motivo: {card.reason}</p>
					{/if}

					{#if !card.configured}
						<p class="text-xs text-muted-foreground">
							Sin configurar: no generará tareas hasta que lo configures.
						</p>
					{/if}

					<Button
						class="mt-auto"
						size="sm"
						variant={card.paused ? 'default' : 'outline'}
						disabled={busyDispatcher === card.kind}
						onclick={() => handleToggleDispatcher(card)}
					>
						{card.paused ? 'Reanudar' : 'Pausar'}
					</Button>
				</div>
			{/each}
		</section>

		<p class="mb-3 text-xs text-muted-foreground">
			Pausar detiene la generación automática de tareas y sobrevive a un reinicio del servicio.
			Las acciones manuales (escanear, ejecutar ahora, sondear el buzón) siguen disponibles.
		</p>

		<div class="mb-3 flex flex-wrap items-center gap-2">
			<h2 class="text-sm font-semibold">Historial unificado</h2>
			<select
				class="ml-auto rounded-md border border-border bg-background px-2 py-1.5 text-sm"
				bind:value={historyKind}
			>
				<option value="">Todos los disparadores</option>
				{#each dispatchers as card (card.kind)}
					<option value={card.kind}>{card.label}</option>
				{/each}
			</select>
		</div>

		{#if history.length === 0}
			<p class="rounded-lg border border-dashed border-border p-8 text-center text-sm text-muted-foreground">
				Sin eventos registrados todavía.
			</p>
		{:else}
			<div class="overflow-x-auto rounded-lg border border-border">
				<table class="w-full text-left text-sm">
					<thead class="bg-muted/50 text-xs uppercase text-muted-foreground">
						<tr>
							<th class="px-3 py-2">Cuándo</th>
							<th class="px-3 py-2">Disparador</th>
							<th class="px-3 py-2">Acción</th>
							<th class="px-3 py-2">Tarea</th>
							<th class="px-3 py-2">Origen</th>
						</tr>
					</thead>
					<tbody>
						{#each history as entry (entry.id)}
							<tr class="border-t border-border">
								<td class="px-3 py-2 text-xs text-muted-foreground">{formatTime(entry.at)}</td>
								<td class="px-3 py-2">
									<span class="rounded-full bg-secondary px-2 py-0.5 text-xs">{entry.kind || '—'}</span>
								</td>
								<td class="px-3 py-2">{entry.action}</td>
								<td class="px-3 py-2 font-mono text-xs">
									{entry.task_id != null ? `#${entry.task_id}` : '—'}
								</td>
								<td class="px-3 py-2 text-xs text-muted-foreground">{entry.actor || '—'}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	{:else}
		{#if audit.length === 0}
			<p class="rounded-lg border border-dashed border-border p-8 text-center text-sm text-muted-foreground">
				Sin registros de auditoría.
			</p>
		{:else}
			<ul class="divide-y divide-border rounded-lg border border-border text-sm">
				{#each audit as entry (entry.id)}
					<li class="flex flex-wrap items-start justify-between gap-2 px-3 py-2">
						<span class="flex items-center gap-2">
							<span class="rounded-full bg-secondary px-2 py-0.5 text-xs">{entry.action}</span>
							{#if entry.task_id}<span class="font-mono text-xs">#{entry.task_id}</span>{/if}
							<span class="text-muted-foreground">{entry.actor || '—'}</span>
						</span>
						<span class="text-xs text-muted-foreground">{formatTime(entry.at)}</span>
					</li>
				{/each}
			</ul>
		{/if}
	{/if}

	<section class="mt-6 rounded-lg border border-border p-4 text-sm">
		<h2 class="mb-2 text-sm font-semibold">📥 Correo (IMAP)</h2>
		{#if imap && (imap.enabled === undefined || imap.enabled)}
			<dl class="grid grid-cols-2 gap-2 text-xs md:grid-cols-4">
				<div>
					<dt class="text-muted-foreground">Carpeta</dt>
					<dd>{imap.folder ?? 'INBOX'}</dd>
				</div>
				<div>
					<dt class="text-muted-foreground">Último sondeo</dt>
					<dd>{formatTime(imap.last_poll ?? null)}</dd>
				</div>
				<div class="col-span-2">
					<dt class="text-muted-foreground">Último error</dt>
					<dd>{imap.last_error ?? '—'}</dd>
				</div>
			</dl>
			<Button class="mt-3" variant="outline" size="sm" onclick={handlePollImap}>
				Probar sondeo
			</Button>
		{:else}
			<p class="text-xs text-muted-foreground">
				El listener IMAP no está activo en el servicio. Configúralo en el archivo de
				configuración de la automatización y reinicia el servicio.
			</p>
		{/if}
	</section>
</div>
