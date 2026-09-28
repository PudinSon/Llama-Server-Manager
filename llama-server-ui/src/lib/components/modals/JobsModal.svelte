<script lang="ts">
	import { onDestroy } from 'svelte';
	import { Loader2, Play, RefreshCw, Skull, Terminal, X } from '@lucide/svelte';
	import { Button } from '$lib/components/ui/button';
	import { jobsStore } from '$lib/stores/jobs.svelte';
	import { closeJobsModal, modalStore } from '$lib/stores/modal.svelte';

	let command = $state('');
	let shell = $state('auto');
	let starting = $state(false);
	let autoScroll = $state(true);
	let outputEl = $state<HTMLPreElement | null>(null);

	const SHELLS = [
		{ label: 'Automático', value: 'auto' },
		{ label: 'PowerShell', value: 'powershell' },
		{ label: 'cmd', value: 'cmd' },
		{ label: 'bash', value: 'bash' },
		{ label: 'Sin shell', value: 'none' }
	];

	$effect(() => {
		if ($modalStore.jobs) {
			void jobsStore.loadJobs();
		}
	});

	$effect(() => {
		// Auto-scroll del visor mientras llega salida nueva.
		if (!autoScroll || !outputEl) return;
		const lineCount = $jobsStore.output.length;
		if (lineCount > 0) outputEl.scrollTop = outputEl.scrollHeight;
	});

	onDestroy(() => jobsStore.stopStream());

	async function handleStart(): Promise<void> {
		if (!command.trim()) return;
		starting = true;
		const jobId = await jobsStore.startJob(command, { shell });
		starting = false;
		if (jobId) {
			command = '';
			jobsStore.startStream(jobId);
		}
	}

	function handleSelect(jobId: string): void {
		jobsStore.stopStream();
		void jobsStore.selectJob(jobId);
	}

	async function handleRefresh(): Promise<void> {
		await jobsStore.refreshSelected();
	}

	function statusColor(status: string): string {
		switch (status) {
			case 'running':
				return 'bg-emerald-500';
			case 'starting':
				return 'bg-amber-400';
			case 'killed':
				return 'bg-orange-500';
			case 'error':
				return 'bg-red-500';
			default:
				return 'bg-muted-foreground/50';
		}
	}
</script>

{#if $modalStore.jobs}
	<div
		class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
		role="presentation"
		onclick={event => event.target === event.currentTarget && closeJobsModal()}
	>
		<div
			class="bg-background border-border/50 flex h-[min(80vh,720px)] w-full max-w-5xl flex-col rounded-xl border p-5 shadow-2xl"
			role="dialog"
			aria-modal="true"
			aria-labelledby="jobs-modal-title"
		>
			<div class="mb-4 flex items-center justify-between">
				<h2 id="jobs-modal-title" class="flex items-center gap-2 text-base font-semibold">
					<Terminal class="h-4 w-4" /> Consola y jobs
				</h2>
				<div class="flex items-center gap-2">
					<Button variant="ghost" size="sm" onclick={() => void jobsStore.clearFinished()}>
						Limpiar terminados
					</Button>
					<Button variant="ghost" size="sm" onclick={handleRefresh} aria-label="Refrescar">
						<RefreshCw class="h-4 w-4" />
					</Button>
					<button type="button" onclick={closeJobsModal} aria-label="Cerrar">
						<X class="h-4 w-4" />
					</button>
				</div>
			</div>

			<form
				class="mb-4 flex flex-col gap-2 sm:flex-row"
				onsubmit={event => {
					event.preventDefault();
					void handleStart();
				}}
			>
				<input
					class="bg-muted/40 border-border focus:border-foreground/40 min-w-0 flex-1 rounded-lg border px-3 py-2 font-mono text-sm outline-none"
					placeholder="pip install pandas  |  ping -t 8.8.8.8  |  python mi_script.py"
					bind:value={command}
				/>
				<select
					class="bg-muted/40 border-border rounded-lg border px-3 py-2 text-sm"
					bind:value={shell}
				>
					{#each SHELLS as option (option.value)}
						<option value={option.value}>{option.label}</option>
					{/each}
				</select>
				<Button type="submit" size="sm" disabled={starting}>
					{#if starting}
						<Loader2 class="h-4 w-4 animate-spin" />
					{:else}
						<Play class="h-4 w-4" />
					{/if}
					Ejecutar
				</Button>
			</form>

			<div class="grid min-h-0 flex-1 grid-cols-1 gap-4 md:grid-cols-[260px_1fr]">
				<div class="border-border/50 max-h-40 overflow-y-auto rounded-lg border md:max-h-none">
					{#if $jobsStore.jobs.length === 0}
						<p class="text-muted-foreground p-3 text-xs">
							Sin jobs todavía. Los comandos largos que lanza el agente aparecen aquí.
						</p>
					{/if}
					{#each $jobsStore.jobs as job (job.job_id)}
						<button
							type="button"
							class="hover:bg-muted/50 border-border/40 flex w-full flex-col gap-1 border-b px-3 py-2 text-left last:border-b-0 {$jobsStore.selectedId ===
							job.job_id
								? 'bg-muted/60'
								: ''}"
							onclick={() => handleSelect(job.job_id)}
						>
							<span class="flex items-center gap-2">
								<span class="h-2 w-2 shrink-0 rounded-full {statusColor(job.status)}"></span>
								<span class="truncate font-mono text-xs">{job.command || job.label}</span>
							</span>
							<span class="text-muted-foreground flex items-center gap-2 text-[10px]">
								<span>{job.status}</span>
								{#if job.exit_code !== null && job.exit_code !== undefined}
									<span>exit {job.exit_code}</span>
								{/if}
								<span>{job.elapsed}s</span>
							</span>
						</button>
					{/each}
				</div>

				<div class="flex min-h-0 flex-col gap-2">
					<div class="flex items-center justify-between text-xs">
						<span class="text-muted-foreground font-mono">
							{$jobsStore.selectedId ?? 'ningún job seleccionado'}
							{#if $jobsStore.streaming}· en vivo{/if}
						</span>
						<div class="flex items-center gap-2">
							<label class="flex items-center gap-1">
								<input type="checkbox" bind:checked={autoScroll} /> auto-scroll
							</label>
							{#if $jobsStore.selectedId}
								<Button
									variant="ghost"
									size="sm"
									onclick={() => jobsStore.startStream($jobsStore.selectedId!)}
								>
									Ver en vivo
								</Button>
								<Button
									variant="ghost"
									size="sm"
									onclick={() => jobsStore.killJob($jobsStore.selectedId!)}
								>
									<Skull class="h-4 w-4" /> Kill
								</Button>
							{/if}
						</div>
					</div>
					<pre
						bind:this={outputEl}
						class="bg-muted/40 border-border/50 min-h-0 flex-1 overflow-auto rounded-lg border p-3 font-mono text-[11px] leading-relaxed whitespace-pre-wrap">{$jobsStore.output.join(
							'\n'
						) || 'Sin salida.'}</pre>
				</div>
			</div>
		</div>
	</div>
{/if}
