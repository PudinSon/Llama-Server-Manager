<script lang="ts">
	import { goto } from '$app/navigation';
	import { MessageSquare, Play, RefreshCw, Trash2, X } from '@lucide/svelte';
	import { Button } from '$lib/components/ui/button';
	import { RouterService } from '$lib/services/router.service';
	import { modalStore, closeTasksModal } from '$lib/stores/modal.svelte';
	import { taskStore } from '$lib/stores/task.svelte';

	let description = $state('');
	let when = $state('');
	let repeat = $state('');

	// El scheduler corre en el backend: no necesita la ventana abierta ni el
	// daemon arrancado, solo que haya un modelo cargado.
	const scheduler = $derived($taskStore.scheduler);
	const schedulerLabel = $derived.by(() => {
		if (!scheduler) return 'comprobando…';
		if (!scheduler.running) return 'detenido';
		if (scheduler.busy) return 'ejecutando una tarea…';
		if (scheduler.waiting_for_model) return 'esperando al modelo';
		return scheduler.model_ready ? 'activo' : 'activo · sin modelo cargado';
	});

	$effect(() => {
		if (!$modalStore.tasks) return;
		// Mientras el modal está abierto, el estado (y las ejecuciones que ya
		// ocurrieron en segundo plano) se refresca solo.
		taskStore.startPolling();
		return () => taskStore.stopPolling();
	});

	$effect(() => {
		if ($modalStore.tasks) {
			void taskStore.fetchTasks();
			if (!when) {
				// Por defecto: dentro de una hora, en formato local para datetime-local
				const soon = new Date(Date.now() + 60 * 60 * 1000);
				soon.setSeconds(0, 0);
				when = new Date(soon.getTime() - soon.getTimezoneOffset() * 60000)
					.toISOString()
					.slice(0, 16);
			}
		}
	});

	async function handleCreate(): Promise<void> {
		if (!description.trim() || !when) return;
		const iso = new Date(when).toISOString();
		const created = await taskStore.createTask(description.trim(), iso, repeat || undefined);
		if (created) {
			description = '';
			repeat = '';
		}
	}

	/** Abre en el chat la conversación que generó la última ejecución de la tarea. */
	async function openGeneratedChat(chatId: string): Promise<void> {
		closeTasksModal();
		await goto(RouterService.chat(chatId));
	}

	function statusLabel(status: string): string {
		switch (status) {
			case 'pending':
				return 'pendiente';
			case 'completed':
				return 'completada';
			case 'failed':
				return 'fallida';
			default:
				return status;
		}
	}
</script>

{#if $modalStore.tasks}
	<div
		class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
		role="presentation"
		onclick={event => event.target === event.currentTarget && closeTasksModal()}
	>
		<div
			class="bg-background border-border/50 flex h-[min(80vh,700px)] w-full max-w-3xl flex-col rounded-xl border p-5 shadow-2xl"
			role="dialog"
			aria-modal="true"
			aria-labelledby="tasks-modal-title"
		>
			<div class="mb-4 flex items-center justify-between">
				<h2 id="tasks-modal-title" class="text-base font-semibold">Tareas programadas</h2>
				<div class="flex items-center gap-1">
					<Button variant="ghost" size="sm" onclick={() => taskStore.fetchTasks()}>
						<RefreshCw class="h-4 w-4" />
					</Button>
					<button type="button" onclick={closeTasksModal} aria-label="Cerrar">
						<X class="h-4 w-4" />
					</button>
				</div>
			</div>

			<div class="bg-muted/30 border-border/50 mb-3 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border px-3 py-2 text-[11px]">
				<span class="font-medium">Scheduler: {schedulerLabel}</span>
				{#if scheduler?.due_task_ids?.length}
					<span class="text-amber-500">{scheduler.due_task_ids.length} vencida(s)</span>
				{/if}
				{#if scheduler?.upcoming?.next_execution}
					<span class="text-muted-foreground">
						próxima: {scheduler.upcoming.description ?? scheduler.upcoming.id} ·
						{new Date(scheduler.upcoming.next_execution).toLocaleString()}
					</span>
				{/if}
				{#if scheduler?.last_run}
					<span class="text-muted-foreground">
						última: {scheduler.last_run.description ?? scheduler.last_run.task_id} ·
						{scheduler.last_run.status === 'completed' ? 'ok' : 'falló'}
					</span>
				{/if}
				{#if scheduler && !scheduler.model_ready}
					<span class="text-amber-500">
						Las tareas vencidas se reintentan en cuanto haya modelo ({scheduler.llm_url}).
					</span>
				{/if}
			</div>

			<form
				class="mb-4 grid gap-2 sm:grid-cols-[2fr_1fr_1fr_auto]"
				onsubmit={event => {
					event.preventDefault();
					void handleCreate();
				}}
			>
				<input
					class="bg-muted/40 border-border rounded-lg border px-3 py-2 text-sm outline-none"
					placeholder="Qué debe hacer el agente"
					bind:value={description}
				/>
				<input
					type="datetime-local"
					class="bg-muted/40 border-border rounded-lg border px-3 py-2 text-sm outline-none"
					bind:value={when}
				/>
				<select
					class="bg-muted/40 border-border rounded-lg border px-3 py-2 text-sm"
					bind:value={repeat}
				>
					<option value="">Una vez</option>
					<option value="daily">Diario</option>
					<option value="weekly">Semanal</option>
					<option value="monthly">Mensual</option>
				</select>
				<Button type="submit" size="sm">Programar</Button>
			</form>

			<div class="border-border/50 min-h-0 flex-1 overflow-y-auto rounded-lg border">
				{#if $taskStore.loading}
					<p class="text-muted-foreground p-3 text-xs">Cargando…</p>
				{:else if $taskStore.tasks.length === 0}					<p class="text-muted-foreground p-3 text-xs">
						No hay tareas programadas. El scheduler del backend las ejecuta aunque esta
						ventana esté cerrada; el resultado aparece en el historial como un chat más.
					</p>
				{/if}
				{#each $taskStore.tasks as task (task.id)}
					<div class="border-border/40 flex items-start gap-3 border-b px-3 py-2 last:border-b-0">
						<div class="min-w-0 flex-1">
							<p class="truncate text-xs font-medium">{task.description}</p>
							<p class="text-muted-foreground text-[10px]">
								{statusLabel(task.status)} · próxima: {task.next_execution}
								{#if task.repeat}· {task.repeat}{/if}
							</p>
							{#if task.result}
								<p class="text-muted-foreground mt-1 line-clamp-2 font-mono text-[10px]">
									{String(task.result).slice(0, 200)}
								</p>
							{/if}
						</div>
						{#if task.last_chat_id}
							<button
								type="button"
								aria-label="Ver el chat generado"
								title="Ver el chat generado"
								class="text-muted-foreground hover:text-foreground"
								onclick={() => openGeneratedChat(task.last_chat_id as string)}
							>
								<MessageSquare class="h-3.5 w-3.5" />
							</button>
						{/if}
						<button
							type="button"
							aria-label="Ejecutar ahora"
							title="Ejecutar ahora"
							class="text-muted-foreground hover:text-foreground"
							onclick={() => taskStore.runTaskNow(task.id)}
						>
							<Play class="h-3.5 w-3.5" />
						</button>
						<button
							type="button"
							aria-label="Eliminar tarea"
							class="text-muted-foreground hover:text-red-500"
							onclick={() => taskStore.deleteTask(task.id)}
						>
							<Trash2 class="h-3.5 w-3.5" />
						</button>
					</div>
				{/each}
			</div>
		</div>
	</div>
{/if}
