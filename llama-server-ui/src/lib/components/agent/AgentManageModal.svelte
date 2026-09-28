<script lang="ts">
	import { agentStore } from '$lib/stores/agentStore.svelte';
	import type { AgencyAgent, AgencyAgentDetail } from '$lib/stores/agentStore.svelte';
	import { toast } from 'svelte-sonner';
	import { FilePlus2, Loader2, Pencil, Search, Trash2, X } from '@lucide/svelte';

	type FullAgent = AgencyAgentDetail;

	let { onClose }: { onClose: () => void } = $props();

	let search = $state('');
	let busy = $state(false);
	let editing = $state<FullAgent | null>(null);
	let editContent = $state('');
	let confirmDelete = $state<string | null>(null);
	let fileInput: HTMLInputElement | null = null;

	let filtered = $derived(
		agentStore.agencyAgents.filter((a) =>
			!search.trim()
				? true
				: `${a.name} ${a.description ?? ''} ${a.category ?? ''}`.toLowerCase().includes(search.trim().toLowerCase())
		)
	);

	// El CRUD va por `agentStore` (endpoint resuelto con api.ts + token), no por
	// fetch propios: así funciona igual en localhost y desde otra máquina.
	async function openEditor(agent: AgencyAgent) {
		busy = true;
		confirmDelete = null;
		try {
			const full = await agentStore.fetchAgentDetail(agent.name);
			editing = { ...agent, content: full?.content ?? '' };
			editContent = full?.content ?? '';
		} catch (error) {
			toast.error(error instanceof Error ? error.message : 'No se pudo cargar el agente');
		} finally {
			busy = false;
		}
	}

	async function saveEdit() {
		if (!editing) return;
		busy = true;
		try {
			await agentStore.saveAgent(editing.name, editContent);
			toast.success(`Agente "${editing.name}" guardado`);
			editing = null;
			editContent = '';
			await agentStore.fetchAgencyAgents();
		} catch (error) {
			toast.error(error instanceof Error ? error.message : 'No se pudo guardar');
		} finally {
			busy = false;
		}
	}

	async function removeAgent(name: string) {
		busy = true;
		try {
			await agentStore.deleteAgent(name);
			toast.success(`Agente "${name}" eliminado`);
			confirmDelete = null;
			if (editing?.name === name) editing = null;
			await agentStore.fetchAgencyAgents();
		} catch (error) {
			toast.error(error instanceof Error ? error.message : 'No se pudo eliminar');
		} finally {
			busy = false;
		}
	}

	function parseFrontmatter(raw: string): { name?: string } {
		const match = raw.match(/^---\s*\n([\s\S]*?)\n---/);
		if (!match) return {};
		const name = match[1].match(/^name:\s*(.+)$/m)?.[1]?.trim();
		return name ? { name } : {};
	}

	async function importFiles(files: FileList | null) {
		if (!files?.length) return;
		busy = true;
		let imported = 0;
		for (const file of Array.from(files)) {
			try {
				const raw = await file.text();
				const fm = parseFrontmatter(raw);
				const fallback = file.name.replace(/\.md$/i, '');
				const name = fm.name || fallback;
				if (!name) continue;
				await agentStore.createAgent(name, raw);
				imported += 1;
			} catch (error) {
				toast.error(`${file.name}: ${error instanceof Error ? error.message : 'error al importar'}`);
			}
		}
		busy = false;
		if (imported) {
			toast.success(`${imported} agente(s) importado(s)`);
			await agentStore.fetchAgencyAgents();
		}
		if (fileInput) fileInput.value = '';
	}
</script>
<div
	class="fixed inset-0 z-[60] flex items-center justify-center bg-black/50 p-4"
	role="presentation"
	onclick={(event) => event.target === event.currentTarget && onClose()}
>
	<div
		class="bg-background border-border/50 flex h-[min(85vh,760px)] w-full max-w-3xl flex-col overflow-hidden rounded-xl border shadow-2xl"
		role="dialog"
		aria-modal="true"
		aria-label="Gestionar agentes"
	>
		<div class="flex items-center justify-between border-b border-border/50 px-4 py-3">
			<span class="text-sm font-semibold">Gestionar agentes</span>
			<div class="flex items-center gap-1">
				<input
					type="file"
					accept=".md,.markdown,text/markdown"
					multiple
					class="hidden"
					bind:this={fileInput}
					onchange={(e) => void importFiles(e.currentTarget.files)}
				/>
				<button
					type="button"
					class="inline-flex h-7 items-center gap-1 rounded-md border border-border px-2 text-xs font-medium hover:bg-accent disabled:opacity-50"
					onclick={() => fileInput?.click()}
					disabled={busy}
				>
					<FilePlus2 class="h-3.5 w-3.5" /> Importar .md
				</button>
				<button
					type="button"
					class="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
					onclick={onClose}
					aria-label="Cerrar gestor"
				>
					<X class="h-3.5 w-3.5" />
				</button>
			</div>
		</div>

		{#if !editing}
			<div class="border-b border-border/50 px-4 py-2">
				<div class="relative">
					<Search class="text-muted-foreground absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2" />
					<input
						class="w-full rounded-md border border-border bg-background py-1.5 pl-8 pr-2 text-xs"
						placeholder="Buscar agente…"
						bind:value={search}
					/>
				</div>
			</div>
			<div class="min-h-0 flex-1 overflow-y-auto px-4 py-3">
				{#if busy && !filtered.length}
					<Loader2 class="text-muted-foreground mx-auto mt-8 h-5 w-5 animate-spin" />
				{:else if !filtered.length}
					<p class="text-muted-foreground py-8 text-center text-xs">Sin agentes.</p>
				{:else}
					<ul class="divide-y divide-border/50">
						{#each filtered as agent (agent.name)}
							<li class="flex items-center gap-3 py-2.5">
								<span class="text-lg leading-none">{agent.emoji || '🤖'}</span>
								<div class="min-w-0 flex-1">
									<p class="truncate text-xs font-medium">{agent.name}</p>
									<p class="text-muted-foreground truncate text-xs">{agent.description}</p>
								</div>
								<button
									type="button"
									class="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
									onclick={() => void openEditor(agent)}
									aria-label="Ver y editar {agent.name}"
									title="Ver texto completo y editar"
								>
									<Pencil class="h-3.5 w-3.5" />
								</button>
								{#if confirmDelete === agent.name}
									<button
										type="button"
										class="h-7 rounded-md bg-red-600 px-2 text-xs font-medium text-white hover:bg-red-700"
										onclick={() => void removeAgent(agent.name)}
									>
										¿Eliminar?
									</button>
								{:else}
									<button
										type="button"
										class="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground hover:bg-red-500/10 hover:text-red-500"
										onclick={() => (confirmDelete = agent.name)}
										aria-label="Eliminar {agent.name}"
										title="Eliminar"
									>
										<Trash2 class="h-3.5 w-3.5" />
									</button>
								{/if}
							</li>
						{/each}
					</ul>
				{/if}
			</div>
		{:else}
			<div class="flex flex-col min-h-0 flex-1">
				<div class="flex items-center justify-between border-b border-border/50 px-4 py-2">
					<span class="truncate text-xs font-semibold">{editing.emoji || '🤖'} {editing.name}</span>
					<div class="flex items-center gap-1">
						<button
							type="button"
							class="h-7 rounded-md bg-primary px-3 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
							onclick={() => void saveEdit()}
							disabled={busy}
						>
							Guardar
						</button>
						<button
							type="button"
							class="h-7 rounded-md border border-border px-2 text-xs hover:bg-accent disabled:opacity-50"
							onclick={() => { editing = null; editContent = ''; }}
							disabled={busy}
						>
							Cancelar
						</button>
					</div>
				</div>
				<textarea
					class="w-full flex-1 resize-none bg-background p-4 font-mono text-xs outline-none"
					bind:value={editContent}
					spellcheck="false"
					placeholder="Markdown del agente (frontmatter + instrucciones)…"
				></textarea>
			</div>
		{/if}
	</div>
</div>

