<script lang="ts">
	import { taskStore, allTasks, tasksLoading } from '$lib/stores/task.svelte';
	import { CalendarClock, Plus, Trash2 } from '@lucide/svelte';
	import { onMount } from 'svelte';

	let description = $state('');
	let when = $state('');

	function getNextWhenValue(): string {
		const now = new Date(Date.now() + 60 * 60 * 1000);
		const offset = now.getTimezoneOffset();
		const local = new Date(now.getTime() - offset * 60 * 1000);
		return local.toISOString().slice(0, 16);
	}

	when = getNextWhenValue();

	async function addTask() {
		const trimmed = description.trim();
		if (!trimmed || !when) return;

		await taskStore.createTask(trimmed, new Date(when).toISOString());
		description = '';
		when = getNextWhenValue();
	}

	async function removeTask(id: string) {
		await taskStore.deleteTask(id);
	}

	onMount(() => {
		void taskStore.fetchTasks();
	});
</script>

<div class="space-y-3">
	<div class="flex items-center justify-between">
		<span class="text-xs font-medium uppercase tracking-wider text-muted-foreground">
			Tareas programadas
		</span>
		<span class="text-[10px] tabular-nums text-muted-foreground">{$allTasks.length}</span>
	</div>

	<div class="space-y-2 rounded-lg border border-border/50 bg-muted/30 p-2.5">
		<label class="space-y-1 text-[11px] text-muted-foreground">
			<span>Descripción</span>
			<input
				bind:value={description}
				type="text"
				placeholder="Ej. Revisar logs del agente"
				class="w-full rounded-md border border-border/50 bg-background px-2 py-1.5 text-xs text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
			/>
		</label>

		<label class="space-y-1 text-[11px] text-muted-foreground">
			<span>Fecha y hora</span>
			<input
				bind:value={when}
				type="datetime-local"
				class="w-full rounded-md border border-border/50 bg-background px-2 py-1.5 text-xs text-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
			/>
		</label>

		<button
			type="button"
			onclick={addTask}
			class="inline-flex items-center gap-1.5 rounded-md bg-primary px-2.5 py-1.5 text-[11px] font-medium text-primary-foreground transition-opacity hover:opacity-90"
		>
			<Plus class="h-3.5 w-3.5" />
			Guardar tarea
		</button>
	</div>

	{#if $allTasks.length > 0}
		<div class="space-y-2">
			{#each $allTasks as task (task.id)}
				<div class="flex items-start gap-2 rounded-lg border border-border/50 bg-muted/20 p-2.5">
					<div class="mt-0.5 shrink-0 rounded-md bg-emerald-500/10 p-1 text-emerald-600 dark:text-emerald-400">
						<CalendarClock class="h-3.5 w-3.5" />
					</div>
					<div class="min-w-0 flex-1">
						<p class="text-xs font-medium text-foreground">{task.description}</p>
						<p class="mt-1 text-[10px] text-muted-foreground">
							{new Date(task.when).toLocaleString([], {
								year: 'numeric',
								month: '2-digit',
								day: '2-digit',
								hour: '2-digit',
								minute: '2-digit'
							})}
						</p>
					</div>
					<button
						type="button"
						onclick={() => removeTask(task.id)}
						class="inline-flex h-7 w-7 items-center justify-center rounded-md border border-border/50 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
						aria-label="Eliminar tarea"
					>
						<Trash2 class="h-3.5 w-3.5" />
					</button>
				</div>
			{/each}
		</div>
	{:else}
		<div class="rounded-lg border border-dashed border-border/50 px-3 py-4 text-center text-[11px] text-muted-foreground">
			Sin tareas programadas
		</div>
	{/if}
</div>
