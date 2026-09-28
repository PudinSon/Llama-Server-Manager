<script lang="ts">
	import { configStore, currentWorkspace, isGlobalMode, configLoading } from '$lib/stores/config.svelte';
	import { modalStore } from '$lib/stores/modal.svelte';
	import { Folder, Globe, History, X } from '@lucide/svelte';
	import { onMount } from 'svelte';

	let localMode = $state(false);
	let pathInput = $state('');
	let showHistory = $state(false);
	let showManualInput = $state(false);
	let pickerSupported = $state(false);
	let pickerInProgress = $state(false);
	let fileInputEl: HTMLInputElement | null = $state(null);

	onMount(() => {
		pickerSupported =
			typeof window !== 'undefined' &&
			'showDirectoryPicker' in window &&
			typeof (window as any).showDirectoryPicker === 'function';
	});

	$effect(() => {
		if ($modalStore.workspace) void configStore.fetchConfig();
	});

	$effect(() => {
		if ($currentWorkspace) pathInput = $currentWorkspace;
	});

	/**
	 * Selecciona carpeta usando la File System Access API (Chromium-based).
	 * Devuelve un objeto { name, kind, fullPath? } o null si cancela.
	 */
	async function pickFolderWithFSA() {
		try {
			// @ts-ignore - showDirectoryPicker no está en lib.dom estándar aún
			const handle: any = await (window as any).showDirectoryPicker({ mode: 'read' });
			if (!handle) return null;
			return {
				name: handle.name as string,
				kind: 'directory' as const,
				// fullPath sólo existe en navegadores no-estándar (Chromium con flag); en producción es undefined
				fullPath: (handle as any).fullPath || null
			};
		} catch (err: any) {
			if (err?.name === 'AbortError') return null;
			console.warn('showDirectoryPicker falló:', err);
			return null;
		}
	}

	/**
	 * Fallback: usa <input webkitdirectory> para que el usuario elija una carpeta.
	 * En navegadores modernos expone `webkitRelativePath` con la ruta relativa.
	 * Devuelve { name, kind, fullPath } o null.
	 */
	function pickFolderWithInput(): Promise<{ name: string; kind: 'directory'; fullPath: string } | null> {
		return new Promise(resolve => {
			if (!fileInputEl) {
				resolve(null);
				return;
			}
			const handler = () => {
				fileInputEl?.removeEventListener('change', handler);
				const files = fileInputEl?.files;
				if (!files || files.length === 0) {
					resolve(null);
					return;
				}
				const first = files[0] as File & { webkitRelativePath?: string };
				const relPath = first.webkitRelativePath || first.name;
				const parts = relPath.split('/').filter(Boolean);
				const folderName = parts[0] || first.name;
				resolve({
					name: folderName,
					kind: 'directory',
					fullPath: parts.length > 1 ? parts.slice(0, -1).join('/') : ''
				});
			};
			fileInputEl.addEventListener('change', handler);
			fileInputEl.value = '';
			fileInputEl.click();
		});
	}

	async function handleSelectWorkspace() {
		if (pickerInProgress) return;
		pickerInProgress = true;
		try {
			let selection = pickerSupported ? await pickFolderWithFSA() : null;
			if (!selection) {
				// Fallback al input webkitdirectory
				selection = await pickFolderWithInput();
			}
			if (!selection) return; // usuario canceló

			// Si el navegador nos dio la ruta absoluta (raro, sólo Chromium con flag), úsala directo.
			if (selection.fullPath && /[\\/]/.test(selection.fullPath)) {
				await configStore.updateWorkspace(selection.fullPath);
				return;
			}

			// Si sólo tenemos el nombre, el backend debe resolver la ruta absoluta.
			const resolved = await configStore.resolveWorkspaceFolder(
				selection.name,
				$currentWorkspace || ''
			);
			if (resolved?.path) {
				await configStore.updateWorkspace(resolved.path);
			}
		} finally {
			pickerInProgress = false;
		}
	}

	async function handleApplyManualPath() {
		const trimmed = pathInput.trim();
		if (!trimmed) return;
		await configStore.updateWorkspace(trimmed);
		showManualInput = false;
	}

	async function handleHistorySelect(path: string) {
		await configStore.updateWorkspace(path);
		showHistory = false;
	}

	async function handleGlobalToggle() {
		await configStore.toggleGlobalMode(!$isGlobalMode);
		localMode = !$isGlobalMode;
	}

	function handleClearWorkspace() {
		void configStore.updateWorkspace('');
	}

	function shortenPath(path: string, max = 50): string {
		if (!path || path.length <= max) return path;
		const start = path.slice(0, 18);
		const end = path.slice(-(max - 21));
		return `${start}…${end}`;
	}
</script>

<div class="space-y-3">
	<!-- Input oculto usado como fallback cuando el navegador no soporta File System Access API -->
	<input
		bind:this={fileInputEl}
		type="file"
		webkitdirectory
		multiple
		class="hidden"
		aria-hidden="true"
		tabindex="-1"
	/>

	<div class="flex items-center justify-between">
		<span class="text-xs font-medium uppercase tracking-wider text-muted-foreground">
			Workspace
		</span>
	</div>

	<div class="space-y-2 rounded-lg border border-border/50 bg-muted/30 p-3">
		<div class="flex items-center justify-between gap-2">
			<div class="flex min-w-0 flex-1 items-center gap-2">
				<Folder class="h-5 w-5 shrink-0 text-muted-foreground" />
				<div class="min-w-0 flex-1">
					<p
						class="truncate font-mono text-sm font-semibold leading-snug text-foreground"
						title={$currentWorkspace || 'Sin seleccionar'}
					>
						{$currentWorkspace || 'Sin seleccionar'}
					</p>
					<p class="text-xs text-muted-foreground">
						{$isGlobalMode ? 'Modo global activo' : 'Modo local'}
					</p>
				</div>
			</div>
			<div class="flex shrink-0 items-center gap-1">
				{#if $currentWorkspace}
					<button
						type="button"
						onclick={handleClearWorkspace}
						disabled={$configLoading}
						title="Quitar workspace"
						class="rounded-md border border-border/50 bg-background p-2 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:opacity-50"
					>
						<X class="h-4 w-4" />
					</button>
				{/if}
				<button
					type="button"
					onclick={handleSelectWorkspace}
					disabled={$configLoading || pickerInProgress}
					title={pickerSupported
						? 'Abrir selector de carpetas del navegador'
						: 'Selecciona una carpeta (se abrirá el diálogo del navegador)'}
					class="rounded-md border border-border/50 bg-background px-3 py-2 text-xs font-medium text-foreground transition-colors hover:bg-accent hover:text-foreground disabled:opacity-50"
				>
					{$configLoading || pickerInProgress ? 'Abriendo...' : 'Seleccionar'}
				</button>
			</div>
		</div>

		<!-- Input manual: pegar una ruta -->
		<div class="space-y-2 border-t border-border/50 pt-2">
			<button
				type="button"
				onclick={() => (showManualInput = !showManualInput)}
				class="text-xs text-muted-foreground transition-colors hover:text-foreground"
			>
				{showManualInput ? '▼ Ocultar' : '▶ Pegar ruta manualmente'}
			</button>
			{#if showManualInput}
				<div class="flex gap-1">
					<input
						type="text"
						bind:value={pathInput}
						placeholder="C:\\Users\\...\\mi-proyecto"
						class="flex-1 rounded-md border border-border/50 bg-background px-2 py-1.5 font-mono text-xs text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary"
					/>
					<button
						type="button"
						onclick={handleApplyManualPath}
						disabled={$configLoading || !pathInput.trim()}
						class="rounded-md border border-border/50 bg-background px-3 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-accent disabled:opacity-50"
					>
						Aplicar
					</button>
				</div>
			{/if}
		</div>

		<!-- Historial -->
		{#if $configStore && $configStore.workspaceHistory && $configStore.workspaceHistory.length > 0}
			<div class="space-y-1 border-t border-border/50 pt-2">
				<button
					type="button"
					onclick={() => (showHistory = !showHistory)}
					class="flex items-center gap-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground"
				>
					<History class="h-3.5 w-3.5" />
					{showHistory ? '▼' : '▶'} Historial ({$configStore.workspaceHistory.length})
				</button>
				{#if showHistory}
					<div class="max-h-44 space-y-0.5 overflow-y-auto">
						{#each $configStore.workspaceHistory as histPath (histPath)}
							{#if histPath && histPath !== $currentWorkspace}
								<button
									type="button"
									onclick={() => handleHistorySelect(histPath)}
									disabled={$configLoading}
									title={histPath}
									class="flex w-full items-center gap-1.5 rounded px-2 py-1.5 text-left text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:opacity-50"
								>
									<Folder class="h-3.5 w-3.5 shrink-0" />
									<span class="truncate font-mono">{shortenPath(histPath)}</span>
								</button>
							{/if}
						{/each}
					</div>
				{/if}
			</div>
		{/if}

		<div class="border-t border-border/50 pt-2">
			<label class="flex cursor-pointer items-center gap-2">
				<input
					type="checkbox"
					checked={$isGlobalMode}
					onchange={handleGlobalToggle}
					disabled={$configLoading}
					class="h-4 w-4 rounded border-border/50 accent-primary"
				/>
				<span class="flex items-center gap-1.5 text-xs text-foreground">
					<Globe class="h-3.5 w-3.5" />
					Modo global (acceso a toda la PC y sus discos)
				</span>
			</label>
		</div>
	</div>
</div>