<script lang="ts">
	import {
		Download,
		FileText,
		Folder,
		FolderPlus,
		Loader2,
		RefreshCw,
		Settings2,
		Trash2,
		Upload,
		X
	} from '@lucide/svelte';
	import { Button } from '$lib/components/ui/button';
	import WorkspaceSelector from '$lib/components/settings/WorkspaceSelector.svelte';
	import { apiJson, apiUpload, serviceUrl } from '$lib/api';
	import { closeWorkspaceModal, modalStore } from '$lib/stores/modal.svelte';
	import DOMPurify from 'dompurify';
	import { toast } from 'svelte-sonner';

	interface Entry {
		ext: string;
		mtime: string;
		name: string;
		path: string;
		size: number | null;
		type: 'dir' | 'file';
	}

	interface PreviewPayload {
		content?: string;
		html?: string;
		kind: 'binary' | 'html' | 'image' | 'pdf' | 'text';
		mime?: string;
		path?: string;
		size?: number;
		truncated?: boolean;
	}

	let currentPath = $state('.');
	let entries = $state<Entry[]>([]);
	let loading = $state(false);
	let uploading = $state(false);
	let error = $state<string | null>(null);
	let selected = $state<Entry | null>(null);
	let preview = $state<PreviewPayload | null>(null);
	let showWorkspaceConfig = $state(false);
	let fileInput = $state<HTMLInputElement | null>(null);
	let dragActive = $state(false);

	const IMAGE_EXTS = new Set(['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg', '.ico']);

	$effect(() => {
		if ($modalStore.workspace) {
			void loadEntries(currentPath);
		}
	});

	async function loadEntries(path = '.'): Promise<void> {
		loading = true;
		error = null;
		try {
			const data = await apiJson<{ entries: Entry[]; path: string }>(
				'workspace',
				`/api/workspace/list?path=${encodeURIComponent(path)}`
			);
			entries = data.entries || [];
			currentPath = data.path || path;
			selected = null;
			preview = null;
		} catch (err) {
			error = err instanceof Error ? err.message : 'Error';
		} finally {
			loading = false;
		}
	}

	async function openEntry(entry: Entry): Promise<void> {
		if (entry.type === 'dir') {
			await loadEntries(entry.path);
			return;
		}
		selected = entry;
		preview = null;
		try {
			preview = await apiJson<PreviewPayload>(
				'workspace',
				`/api/workspace/preview?path=${encodeURIComponent(entry.path)}`
			);
		} catch (err) {
			error = err instanceof Error ? err.message : 'Error';
		}
	}

	async function downloadEntry(entry: Entry): Promise<void> {
		try {
			const url = serviceUrl(
				'workspace',
				`/api/workspace/download?path=${encodeURIComponent(entry.path)}`
			);
			const res = await fetch(url);
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			const blob = await res.blob();
			// Descarga robusta (navegador y WebView2): blob + <a download>.
			const objUrl = URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = objUrl;
			a.download = entry.name || 'archivo';
			document.body.appendChild(a);
			a.click();
			document.body.removeChild(a);
			URL.revokeObjectURL(objUrl);
		} catch (err) {
			toast.error(`No se pudo descargar: ${err instanceof Error ? err.message : 'Error'}`);
		}
	}

	async function removeEntry(entry: Entry): Promise<void> {
		if (!confirm(`¿Eliminar "${entry.name}"?`)) return;
		try {
			await apiJson('workspace', `/api/workspace/delete?path=${encodeURIComponent(entry.path)}`, {
				method: 'DELETE'
			});
			toast.success('Eliminado');
			await loadEntries(currentPath);
		} catch (err) {
			toast.error(`No se pudo eliminar: ${err instanceof Error ? err.message : 'Error'}`);
		}
	}

	async function createFolder(): Promise<void> {
		const name = prompt('Nombre de la nueva carpeta');
		if (!name?.trim()) return;
		try {
			await apiJson('workspace', '/api/workspace/mkdir', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ name: name.trim(), path: currentPath })
			});
			await loadEntries(currentPath);
		} catch (err) {
			toast.error(`No se pudo crear: ${err instanceof Error ? err.message : 'Error'}`);
		}
	}

	async function uploadFiles(files: FileList | File[] | null): Promise<void> {
		const list = Array.from(files || []);
		if (!list.length) return;
		uploading = true;
		try {
			const form = new FormData();
			form.append('path', currentPath);
			for (const file of list) form.append('file', file, file.name);
			const res = await apiUpload('workspace', '/api/workspace/upload', form);
			const data = await res.json();
			if (!res.ok || !data.success) throw new Error(data.error || `HTTP ${res.status}`);
			toast.success(`Subidos ${data.count} archivo(s)`);
			await loadEntries(currentPath);
		} catch (err) {
			toast.error(`Error subiendo: ${err instanceof Error ? err.message : 'Error'}`);
		} finally {
			uploading = false;
			if (fileInput) fileInput.value = '';
		}
	}

	function goUp(): void {
		if (currentPath === '.' || currentPath === '') return;
		const parts = currentPath.split('/').filter(Boolean);
		parts.pop();
		void loadEntries(parts.length ? parts.join('/') : '.');
	}

	function formatSize(size: number | null): string {
		if (size === null || size === undefined) return '';
		if (size < 1024) return `${size} B`;
		if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
		return `${(size / (1024 * 1024)).toFixed(1)} MB`;
	}
</script>

{#if $modalStore.workspace}
	<div
		class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
		role="presentation"
		onclick={event => event.target === event.currentTarget && closeWorkspaceModal()}
	>
		<div
			class="bg-background border-border/50 flex h-[min(82vh,760px)] w-full max-w-6xl flex-col rounded-xl border p-5 shadow-2xl"
			role="dialog"
			aria-modal="true"
			aria-labelledby="workspace-modal-title"
		>
			<div class="mb-4 flex items-center justify-between">
				<h2 id="workspace-modal-title" class="text-base font-semibold">Workspace</h2>
				<div class="flex items-center gap-1">
					<Button
						variant="ghost"
						size="sm"
						onclick={() => (showWorkspaceConfig = !showWorkspaceConfig)}
					>
						<Settings2 class="h-4 w-4" /> Cambiar carpeta
					</Button>
					<button type="button" onclick={closeWorkspaceModal} aria-label="Cerrar">
						<X class="h-4 w-4" />
					</button>
				</div>
			</div>

			{#if showWorkspaceConfig}
				<div class="border-border/50 mb-4 rounded-lg border p-3">
					<WorkspaceSelector />
				</div>
			{/if}

			<div class="mb-3 flex flex-wrap items-center gap-2">
				<Button variant="ghost" size="sm" onclick={goUp} disabled={currentPath === '.'}>..</Button>
				<span class="text-muted-foreground truncate font-mono text-xs">{currentPath}</span>
				<div class="ml-auto flex items-center gap-1">
					<Button variant="ghost" size="sm" onclick={() => loadEntries(currentPath)}>
						<RefreshCw class="h-4 w-4" />
					</Button>
					<Button variant="ghost" size="sm" onclick={createFolder}>
						<FolderPlus class="h-4 w-4" /> Carpeta
					</Button>
					<Button variant="ghost" size="sm" onclick={() => fileInput?.click()} disabled={uploading}>
						{#if uploading}
							<Loader2 class="h-4 w-4 animate-spin" />
						{:else}
							<Upload class="h-4 w-4" />
						{/if}
						Subir
					</Button>
					<input
						bind:this={fileInput}
						type="file"
						multiple
						class="hidden"
						onchange={event => uploadFiles(event.currentTarget.files)}
					/>
				</div>
			</div>

			<div
				class="border-border/40 mb-2 rounded-lg border border-dashed p-2 text-center text-xs {dragActive
					? 'bg-accent/40'
					: 'text-muted-foreground'}"
				role="presentation"
				ondragover={event => {
					event.preventDefault();
					dragActive = true;
				}}
				ondragleave={() => (dragActive = false)}
				ondrop={event => {
					event.preventDefault();
					dragActive = false;
					void uploadFiles(event.dataTransfer?.files ?? null);
				}}
			>
				Arrastra archivos aquí para subirlos a <span class="font-mono">{currentPath}</span>
			</div>

			{#if error}
				<p class="mb-2 text-xs text-red-500">{error}</p>
			{/if}

			<div class="grid min-h-0 flex-1 grid-cols-1 gap-4 md:grid-cols-[320px_1fr]">
				<div class="border-border/50 min-h-0 overflow-y-auto rounded-lg border">
					{#if loading}
						<p class="text-muted-foreground p-3 text-xs">Cargando…</p>
					{:else if entries.length === 0}
						<p class="text-muted-foreground p-3 text-xs">Carpeta vacía.</p>
					{/if}
					{#each entries as entry (entry.path)}
						<div
							class="hover:bg-muted/50 border-border/40 flex items-center gap-2 border-b px-2 py-1.5 last:border-b-0 {selected?.path ===
							entry.path
								? 'bg-muted/60'
								: ''}"
						>
							<button
								type="button"
								class="flex min-w-0 flex-1 items-center gap-2 text-left"
								onclick={() => openEntry(entry)}
							>
								{#if entry.type === 'dir'}
									<Folder class="text-muted-foreground h-4 w-4 shrink-0" />
								{:else}
									<FileText class="text-muted-foreground h-4 w-4 shrink-0" />
								{/if}
								<span class="truncate text-xs">{entry.name}</span>
								<span class="text-muted-foreground ml-auto shrink-0 text-[10px]">
									{formatSize(entry.size)}
								</span>
							</button>
							{#if entry.type === 'file'}
								<button
									type="button"
									aria-label="Descargar"
									onclick={() => downloadEntry(entry)}
									class="text-muted-foreground hover:text-foreground"
								>
									<Download class="h-3.5 w-3.5" />
								</button>
							{/if}
							<button
								type="button"
								aria-label="Eliminar"
								onclick={() => removeEntry(entry)}
								class="text-muted-foreground hover:text-red-500"
							>
								<Trash2 class="h-3.5 w-3.5" />
							</button>
						</div>
					{/each}
				</div>

				<div class="border-border/50 flex min-h-0 flex-col rounded-lg border">
					{#if !selected}
						<p class="text-muted-foreground p-3 text-xs">
							Selecciona un archivo para previsualizarlo (texto, imágenes, PDF y DOCX).
						</p>
					{:else if !preview}
						<p class="text-muted-foreground p-3 text-xs">Cargando previsualización…</p>
					{:else if preview.kind === 'html'}
						<div class="min-h-0 flex-1 overflow-auto p-4 text-sm leading-relaxed">
							{@html DOMPurify.sanitize(preview.html ?? '')}
						</div>
					{:else if preview.kind === 'image' || (selected && IMAGE_EXTS.has(selected.ext))}
						<div class="flex flex-1 items-center justify-center overflow-auto p-4">
							<img
								src={serviceUrl('workspace', `/api/workspace/raw?path=${selected.path}`)}
								alt={selected.name}
								class="max-h-full max-w-full object-contain"
							/>
						</div>
					{:else if preview.kind === 'pdf' || selected?.ext === '.pdf'}
						<div class="flex min-h-0 flex-1 flex-col gap-2 p-2">
							<iframe
								title={selected.name}
								src={serviceUrl('workspace', `/api/workspace/raw?path=${selected.path}`)}
								class="min-h-0 flex-1 rounded-lg border-0 bg-white"
							></iframe>
							<div class="flex justify-end">
								<Button size="sm" variant="ghost" onclick={() => downloadEntry(selected!)}>
									<Download class="h-4 w-4" /> Descargar PDF
								</Button>
							</div>
						</div>
					{:else if preview.kind === 'binary'}
						<div class="flex flex-1 flex-col items-center justify-center gap-2 p-4">
							<p class="text-muted-foreground text-xs">
								Vista previa no disponible para {preview.mime || 'este formato'}.
							</p>
							<Button size="sm" onclick={() => downloadEntry(selected!)}>
								<Download class="h-4 w-4" /> Descargar
							</Button>
						</div>
					{:else}
						<pre
							class="min-h-0 flex-1 overflow-auto p-3 font-mono text-[11px] leading-relaxed">{preview.content ??
								''}</pre>
						{#if preview.truncated}
							<p class="text-muted-foreground border-border/40 border-t px-3 py-1 text-[10px]">
								Vista parcial ({formatSize(preview.size ?? null)}). Descárgalo para verlo completo.
							</p>
						{/if}
					{/if}
				</div>
			</div>
		</div>
	</div>
{/if}
