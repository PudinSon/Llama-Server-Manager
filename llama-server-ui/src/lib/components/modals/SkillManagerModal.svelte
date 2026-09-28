<script lang="ts">
	// Gestor de skills (memoria procedimental).
	//
	// Interacción separada a propósito:
	//  - el checkbox de cada fila ACTIVA/DESACTIVA el skill (lo que se indexa y
	//    se inyecta en el contexto del agente);
	//  - el resto de la fila ABRE el skill para verlo y editarlo (SKILL.md
	//    completo) y guardarlo con el botón Guardar.
	import { Eye, Loader2, Pencil, RefreshCw, Save, Upload, X } from '@lucide/svelte';
	import { Button } from '$lib/components/ui/button';
	import { apiJson, serviceUrl } from '$lib/api';
	import { modalStore, closeSkillsModal } from '$lib/stores/modal.svelte';
	import { skillStore, type Skill } from '$lib/stores/skill.svelte';
	import { toast } from 'svelte-sonner';

	let newName = $state('');
	let newDescription = $state('');
	let importing = $state(false);
	let fileInput = $state<HTMLInputElement | null>(null);

	let selectedPath = $state<string | null>(null);
	let draft = $state('');
	let editing = $state(false);
	let saving = $state(false);

	let skills = $derived($skillStore.skills as Skill[]);
	let selected = $derived(skills.find(skill => skill.path === selectedPath) ?? null);
	let dirty = $derived(editing && draft !== (selected?.content ?? ''));

	$effect(() => {
		if ($modalStore.skills) {
			void skillStore.fetchSkills();
		}
	});

	// Si el skill seleccionado se recarga desde el backend, sincroniza el
	// borrador mientras no se esté editando (para no pisar cambios en curso).
	$effect(() => {
		const current = selected;
		if (!current || editing) return;
		const content = current.content ?? '';
		if (content !== draft) draft = content;
	});

	function isActive(name: string): boolean {
		return $skillStore.activeSkills.has(name);
	}

	async function toggleSkill(name: string): Promise<void> {
		if (isActive(name)) await skillStore.deactivateSkill(name);
		else await skillStore.activateSkill(name);
	}

	function openSkill(skill: Skill): void {
		selectedPath = skill.path;
		draft = skill.content ?? '';
		editing = false;
	}

	function startEditing(): void {
		draft = selected?.content ?? '';
		editing = true;
	}

	function cancelEditing(): void {
		draft = selected?.content ?? '';
		editing = false;
	}

	async function saveSkill(): Promise<void> {
		if (!selected) return;
		if (!draft.trim()) {
			toast.error('El contenido no puede estar vacío');
			return;
		}
		saving = true;
		try {
			const saved = await skillStore.updateSkill(selected.name, draft, selected.scope);
			if (saved) editing = false;
		} finally {
			saving = false;
		}
	}

	async function importFiles(files: FileList | null): Promise<void> {
		const list = Array.from(files || []);
		if (!list.length) return;
		importing = true;
		try {
			const form = new FormData();
			for (const file of list) form.append('file', file, file.name);
			const res = await fetch(serviceUrl('agent', '/api/skills/import'), {
				method: 'POST',
				body: form
			});
			const data = await res.json();
			if (!res.ok || !data.success) throw new Error(data.error || `HTTP ${res.status}`);
			toast.success(`${data.count} skill(s) importado(s)`);
			await skillStore.fetchSkills();
		} catch (err) {
			toast.error(`Error importando: ${err instanceof Error ? err.message : 'Error'}`);
		} finally {
			importing = false;
			if (fileInput) fileInput.value = '';
		}
	}

	async function createSkill(): Promise<void> {
		if (!newName.trim()) return;
		try {
			await apiJson('agent', '/api/skills', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					name: newName.trim(),
					description: newDescription.trim(),
					steps: [{ action: newDescription.trim() || `Aplicar ${newName.trim()}` }]
				})
			});
			toast.success('Skill creado');
			newName = '';
			newDescription = '';
			await skillStore.fetchSkills();
		} catch (err) {
			toast.error(`No se pudo crear: ${err instanceof Error ? err.message : 'Error'}`);
		}
	}
</script>

{#if $modalStore.skills}
	<div
		class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
		role="presentation"
		onclick={event => event.target === event.currentTarget && closeSkillsModal()}
	>
		<div
			class="bg-background border-border/50 flex h-[min(85vh,760px)] w-full max-w-5xl flex-col rounded-xl border p-5 shadow-2xl"
			role="dialog"
			aria-modal="true"
			aria-labelledby="skills-modal-title"
		>
			<div class="mb-4 flex items-center justify-between">
				<h2 id="skills-modal-title" class="text-base font-semibold">
					Skills (memoria procedimental)
				</h2>
				<div class="flex items-center gap-1">
					<Button variant="ghost" size="sm" onclick={() => skillStore.fetchSkills()}>
						<RefreshCw class="h-4 w-4" />
					</Button>
					<Button variant="ghost" size="sm" onclick={() => fileInput?.click()} disabled={importing}>
						{#if importing}
							<Loader2 class="h-4 w-4 animate-spin" />
						{:else}
							<Upload class="h-4 w-4" />
						{/if}
						Importar .md
					</Button>
					<input
						bind:this={fileInput}
						type="file"
						multiple
						accept=".md,.markdown,.txt"
						class="hidden"
						onchange={event => importFiles(event.currentTarget.files)}
					/>
					<button type="button" onclick={closeSkillsModal} aria-label="Cerrar">
						<X class="h-4 w-4" />
					</button>
				</div>
			</div>

			<form
				class="mb-4 grid gap-2 sm:grid-cols-[1fr_1.5fr_auto]"
				onsubmit={event => {
					event.preventDefault();
					void createSkill();
				}}
			>
				<input
					class="bg-muted/40 border-border rounded-lg border px-3 py-2 text-sm outline-none"
					placeholder="nombre-del-skill"
					bind:value={newName}
				/>
				<input
					class="bg-muted/40 border-border rounded-lg border px-3 py-2 text-sm outline-none"
					placeholder="Descripción / cuándo usarlo"
					bind:value={newDescription}
				/>
				<Button type="submit" size="sm">Crear</Button>
			</form>

			<div class="grid min-h-0 flex-1 gap-3 md:grid-cols-[minmax(0,1fr)_minmax(0,1.25fr)]">
				<div class="border-border/50 min-h-0 overflow-y-auto rounded-lg border">
					{#if $skillStore.loading}
						<p class="text-muted-foreground p-3 text-xs">Cargando…</p>
					{:else if skills.length === 0}
						<p class="text-muted-foreground p-3 text-xs">
							No hay skills todavía. Crea uno o importa un archivo .md.
						</p>
					{/if}
					{#each skills as skill (skill.path ?? skill.name)}
						<div
							class="border-border/40 flex items-center gap-2.5 border-b px-2 py-1.5 last:border-b-0 {selectedPath ===
							skill.path
								? 'bg-accent/60'
								: 'hover:bg-muted/40'}"
						>
							<input
								class="shrink-0"
								type="checkbox"
								checked={isActive(skill.name)}
								onchange={() => toggleSkill(skill.name)}
								title={isActive(skill.name) ? 'Desactivar skill' : 'Activar skill'}
								aria-label="Activar {skill.name}"
							/>
							<button
								type="button"
								class="flex min-w-0 flex-1 flex-col items-start gap-0.5 py-1 text-left"
								onclick={() => openSkill(skill)}
								title="Ver y editar este skill"
							>
								<span class="block w-full truncate text-xs font-medium">
									{skill.name}
									{#if skill.auto_generated}
										<span class="text-muted-foreground text-[10px]">· auto</span>
									{/if}
								</span>
								<span class="text-muted-foreground block w-full truncate text-[10px]">
									{skill.description || skill.scope}
									{#if skill.confidence !== undefined}· conf {skill.confidence}{/if}
								</span>
							</button>
							<Pencil class="text-muted-foreground h-3 w-3 shrink-0" />
						</div>
					{/each}
				</div>

				<div class="border-border/50 flex min-h-0 flex-col rounded-lg border">
					{#if !selected}
						<p class="text-muted-foreground p-3 text-xs">
							Selecciona un skill de la lista para verlo y editarlo. El checkbox de la izquierda
							solo activa o desactiva el skill.
						</p>
					{:else}
						<div class="border-border/40 flex items-center gap-2 border-b px-3 py-2">
							<div class="min-w-0 flex-1">
								<p class="truncate text-xs font-semibold">{selected.name}</p>
								<p class="text-muted-foreground truncate font-mono text-[10px]" title={selected.path}>
									{selected.scope} · {selected.path}
								</p>
							</div>
							{#if editing}
								<Button size="sm" variant="ghost" onclick={cancelEditing} disabled={saving}>
									<X class="h-3.5 w-3.5" /> Descartar
								</Button>
								<Button size="sm" onclick={saveSkill} disabled={saving || !dirty}>
									{#if saving}
										<Loader2 class="h-3.5 w-3.5 animate-spin" />
									{:else}
										<Save class="h-3.5 w-3.5" />
									{/if}
									Guardar
								</Button>
							{:else}
								<Button size="sm" variant="ghost" onclick={startEditing}>
									<Pencil class="h-3.5 w-3.5" /> Editar
								</Button>
							{/if}
						</div>

						{#if editing}
							<textarea
								class="bg-muted/20 min-h-0 flex-1 resize-none p-3 font-mono text-[11px] leading-relaxed outline-none"
								spellcheck="false"
								bind:value={draft}
							></textarea>
						{:else}
							<pre
								class="min-h-0 flex-1 overflow-auto p-3 font-mono text-[11px] leading-relaxed whitespace-pre-wrap">{selected.content ??
									'(sin contenido)'}</pre>
						{/if}
					{/if}
				</div>
			</div>

			<p class="text-muted-foreground mt-2 flex items-center gap-1 text-[10px]">
				<Eye class="h-3 w-3" />
				Los skills activos se indexan en ChromaDB y se inyectan en el contexto del agente cuando
				la tarea coincide con su descripción.
			</p>
		</div>
	</div>
{/if}
