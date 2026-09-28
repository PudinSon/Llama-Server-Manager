<script lang="ts">
	import { BookOpen, FileText, Library, Plus, Search, Trash2, Upload, X } from '@lucide/svelte';
	import { onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import { modalStore, closeRAGModal } from '$lib/stores/modal.svelte';
	import { documentList, ragActiveLibrary, ragDocCount, ragLibraries, ragLoading, ragStore } from '$lib/stores/rag.svelte';

	type PendingFile = { file: File; words: number | null };
	let query = $state('');
	let searchResults = $state<any[]>([]);
	let fileInput = $state<HTMLInputElement | undefined>();
	let showNewLibInput = $state(false);
	let newLibName = $state('');
	let pendingFiles = $state<PendingFile[]>([]);
	let indexing = $state(false);
	let indexedCount = $state(0);
	let indexingFile = $state('');
	let indexingProgress = $state(0);
	let selectedDocument = $state<(typeof $documentList)[number] | null>(null);
	let showSearchModal = $state(false);

	onMount(() => {
		void ragStore.fetchLibraries();
		void ragStore.fetchDocuments();
		void ragStore.fetchActive();
	});

	function isTextFile(file: File): boolean {
		return /\.(txt|md|csv|tsv|json|yaml|yml|html|xml|rtf)$/i.test(file.name);
	}

	async function countWords(file: File): Promise<number | null> {
		if (!isTextFile(file)) return null;
		try {
			const text = await file.text();
			return text.trim() ? text.trim().split(/\s+/).length : 0;
		} catch {
			return null;
		}
	}

	async function handleFileSelect(event: Event) {
		const target = event.currentTarget as HTMLInputElement;
		const files = target.files ? Array.from(target.files) : [];
		for (const file of files) {
			const exists = pendingFiles.some((item) => item.file.name === file.name && item.file.size === file.size && item.file.lastModified === file.lastModified);
			if (!exists) pendingFiles = [...pendingFiles, { file, words: await countWords(file) }];
		}
		target.value = '';
	}

	function removePendingFile(index: number) {
		pendingFiles = pendingFiles.filter((_, fileIndex) => fileIndex !== index);
	}

	async function indexPendingFiles() {
		if (indexing || pendingFiles.length === 0 || !$ragActiveLibrary) return;
		indexing = true;
		indexedCount = 0;
		indexingProgress = 0;
		const filesToIndex = [...pendingFiles];
		for (const [index, item] of filesToIndex.entries()) {
			indexingFile = item.file.name;
			indexingProgress = Math.round((index / filesToIndex.length) * 100);
			const result = await ragStore.uploadDocument(item.file, $ragActiveLibrary);
			if (result?.success) {
				indexedCount += 1;
				pendingFiles = pendingFiles.filter((pending) => pending.file !== item.file);
			}
		}
		indexingProgress = 100;
		indexingFile = '';
		indexing = false;
		if (indexedCount > 0) toast.success(`${indexedCount} archivo(s) indexado(s)`);
	}

	async function handleSearch() {
		if (!query.trim()) return;
		searchResults = await ragStore.searchDocuments(query.trim(), 10);
		showSearchModal = true;
	}

	async function handleDeleteDocument(docId: string) {
		await ragStore.deleteDocument(docId);
		if (selectedDocument?.id === docId) selectedDocument = null;
	}

	async function handleCreateLibrary() {
		if (!newLibName.trim()) return;
		if (await ragStore.createLibrary(newLibName.trim())) {
			newLibName = '';
			showNewLibInput = false;
		}
	}

	async function handleDeleteLibrary() {
		if (!$ragActiveLibrary || !confirm(`Eliminar biblioteca "${$ragActiveLibrary}" y sus documentos?`)) return;
		await ragStore.deleteLibrary($ragActiveLibrary);
	}

	async function handleSetActive(event: Event) {
		const name = (event.currentTarget as HTMLSelectElement).value;
		if (name) await ragStore.setActive(name);
	}

	function formatSize(bytes: number): string {
		if (bytes < 1024) return `${bytes} B`;
		if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
		return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
	}
</script>

{#if $modalStore.rag}
	<div class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4 backdrop-blur-sm" onclick={(event) => event.target === event.currentTarget && closeRAGModal()} role="presentation">
		<div class="bg-background border-border/50 flex max-h-[88vh] w-full max-w-3xl flex-col overflow-hidden rounded-2xl border shadow-2xl" role="dialog" aria-modal="true" aria-labelledby="rag-modal-title">
			<div class="bg-muted/30 border-border/50 flex items-center justify-between border-b px-6 py-4">
				<div class="flex items-center gap-3"><BookOpen class="text-primary h-5 w-5" /><h2 id="rag-modal-title" class="text-lg font-semibold">Base de Conocimientos</h2></div>
				<button type="button" onclick={closeRAGModal} class="text-muted-foreground hover:bg-accent rounded-full p-2" aria-label="Cerrar"><X class="h-4 w-4" /></button>
			</div>
			<div class="bg-muted/10 border-border/30 flex items-center gap-4 border-b px-6 py-3 text-xs text-muted-foreground"><span class="flex items-center gap-1"><Library class="h-3.5 w-3.5" />{$ragLibraries.length} bibliotecas</span><span class="flex items-center gap-1"><FileText class="h-3.5 w-3.5" />{$ragDocCount} documentos</span>{#if $ragActiveLibrary}<span class="text-primary ml-auto">Activa: {$ragActiveLibrary}</span>{/if}</div>

			<div class="space-y-5 overflow-y-auto p-6">
				<section class="space-y-2">
					<label for="rag-active-lib" class="text-muted-foreground text-xs font-medium uppercase tracking-wider">Biblioteca</label>
					<div class="flex gap-2"><select id="rag-active-lib" value={$ragActiveLibrary ?? ''} onchange={handleSetActive} class="bg-background border-border/50 flex-1 rounded-lg border px-3 py-2 text-sm"><option value="">Seleccionar biblioteca...</option>{#each $ragLibraries as library}<option value={library}>{library}</option>{/each}</select><button type="button" onclick={() => (showNewLibInput = !showNewLibInput)} class="bg-primary text-primary-foreground inline-flex items-center gap-1 rounded-lg px-3 py-2 text-xs"><Plus class="h-3.5 w-3.5" />Nueva</button><button type="button" onclick={handleDeleteLibrary} disabled={!$ragActiveLibrary} class="border-destructive/30 text-destructive rounded-lg border px-3 py-2 text-xs disabled:opacity-50"><Trash2 class="inline h-3.5 w-3.5" /> Eliminar</button></div>
					{#if showNewLibInput}<div class="flex gap-2"><input bind:value={newLibName} placeholder="Nombre de biblioteca" class="bg-background border-border/50 flex-1 rounded-lg border px-3 py-2 text-sm" /><button type="button" onclick={handleCreateLibrary} class="rounded-lg bg-green-600 px-3 py-2 text-xs text-white">Crear</button></div>{/if}
				</section>

				<section class="border-border/50 space-y-3 rounded-xl border p-4"><div class="flex items-center justify-between"><div><h3 class="text-sm font-medium">Archivos para indexar</h3><p class="text-muted-foreground text-xs">Agrega o retira archivos antes de iniciar.</p></div><input id="rag-file-input" type="file" multiple bind:this={fileInput} onchange={handleFileSelect} accept=".pdf,.txt,.md,.docx,.pptx,.xlsx,.rtf,.odt,.odp,.ods,.html,.xml,.csv,.tsv,.json,.yaml,.yml" class="hidden" /><button type="button" onclick={() => fileInput?.click()} disabled={!$ragActiveLibrary || indexing} class="border-border/50 inline-flex items-center gap-2 rounded-lg border px-3 py-2 text-xs disabled:opacity-50"><Upload class="h-4 w-4" />Agregar archivos</button></div>{#if pendingFiles.length > 0}<div class="divide-border/50 divide-y rounded-lg border">{#each pendingFiles as item, index}<div class="flex items-center gap-3 px-3 py-2 text-xs"><FileText class="text-muted-foreground h-4 w-4" /><span class="min-w-0 flex-1 truncate">{item.file.name}</span><span class="text-muted-foreground">{formatSize(item.file.size)}{#if item.words !== null} · {item.words} palabras{/if}</span><button type="button" onclick={() => removePendingFile(index)} disabled={indexing} class="text-destructive p-1" aria-label="Retirar archivo"><X class="h-4 w-4" /></button></div>{/each}</div>{:else}<p class="text-muted-foreground border-border/50 rounded-lg border border-dashed p-4 text-center text-xs">No hay archivos pendientes.</p>{/if}<button type="button" onclick={indexPendingFiles} disabled={pendingFiles.length === 0 || !$ragActiveLibrary || indexing} class="bg-primary text-primary-foreground w-full rounded-lg px-4 py-2 text-sm disabled:opacity-50">{indexing ? 'Indexando...' : 'Iniciar indexado'}</button>{#if indexing}<div class="space-y-1"><div class="flex justify-between text-xs"><span class="truncate">{indexingFile}</span><span>{indexingProgress}%</span></div><div class="bg-muted h-2 overflow-hidden rounded-full"><div class="bg-primary h-full transition-all" style={`width: ${indexingProgress}%`}></div></div><p class="text-muted-foreground text-xs">Archivo {indexedCount + 1} de {pendingFiles.length + indexedCount}</p></div>{/if}</section>

				<section class="space-y-2"><div class="flex items-center justify-between"><h3 class="text-muted-foreground text-xs font-medium uppercase tracking-wider">Documentos indexados</h3><span class="text-muted-foreground text-xs">{$documentList.length}</span></div>{#if $documentList.length}<div class="max-h-52 space-y-1 overflow-y-auto rounded-lg border p-2">{#each $documentList as doc (doc.id)}<div class="flex items-center gap-2 rounded-lg p-2 hover:bg-accent/50"><FileText class="text-muted-foreground h-4 w-4" /><button type="button" onclick={() => (selectedDocument = doc)} class="min-w-0 flex-1 text-left"><p class="truncate text-xs font-medium">{doc.filename}</p><p class="text-muted-foreground text-[10px]">{doc.chunks} chunks</p></button><button type="button" onclick={() => handleDeleteDocument(doc.id)} class="text-destructive p-1" aria-label="Retirar documento"><Trash2 class="h-3.5 w-3.5" /></button></div>{/each}</div>{:else}<p class="text-muted-foreground border-border/50 rounded-lg border border-dashed p-4 text-center text-xs">Sin documentos en esta biblioteca.</p>{/if}</section>

				<section class="space-y-2"><label for="rag-search" class="text-muted-foreground text-xs font-medium uppercase tracking-wider">Buscar en biblioteca</label><div class="flex gap-2"><input id="rag-search" bind:value={query} type="text" placeholder="Buscar en documentos..." class="bg-background border-border/50 flex-1 rounded-lg border px-3 py-2 text-sm" onkeydown={(event) => event.key === 'Enter' && handleSearch()} /><button type="button" onclick={handleSearch} disabled={$ragLoading || !query.trim()} class="inline-flex items-center gap-1 rounded-lg bg-amber-600 px-4 py-2 text-xs text-white disabled:opacity-50"><Search class="h-3.5 w-3.5" />Buscar</button></div></section>
			</div>
		</div>
	</div>
{/if}

{#if selectedDocument}
	<div class="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 p-4" onclick={(event) => event.target === event.currentTarget && (selectedDocument = null)} role="presentation"><div class="bg-background w-full max-w-3xl rounded-xl border p-5 shadow-2xl" role="dialog" aria-modal="true"><div class="mb-4 flex items-center justify-between"><h2 class="truncate font-semibold">{selectedDocument.filename}</h2><button type="button" onclick={() => (selectedDocument = null)} aria-label="Cerrar"><X class="h-4 w-4" /></button></div><div class="text-muted-foreground max-h-[70vh] overflow-y-auto whitespace-pre-wrap text-sm">{selectedDocument.chunkTexts.join('\n\n--- chunk ---\n\n')}</div></div></div>
{/if}

{#if showSearchModal}
	<div class="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 p-4" onclick={(event) => event.target === event.currentTarget && (showSearchModal = false)} role="presentation"><div class="bg-background w-full max-w-3xl rounded-xl border p-5 shadow-2xl" role="dialog" aria-modal="true"><div class="mb-4 flex items-center justify-between"><h2 class="font-semibold">Resultados para “{query}”</h2><button type="button" onclick={() => (showSearchModal = false)} aria-label="Cerrar"><X class="h-4 w-4" /></button></div>{#if searchResults.length}<div class="max-h-[70vh] space-y-3 overflow-y-auto">{#each searchResults as result, index}<article class="border-border/50 rounded-lg border p-3 text-sm"><div class="mb-1 flex justify-between text-xs font-medium"><span>Resultado {index + 1}</span>{#if result.score !== undefined}<span>{Number(result.score).toFixed(3)}</span>{/if}</div><p class="text-muted-foreground whitespace-pre-wrap">{result.content || result.text || JSON.stringify(result)}</p></article>{/each}</div>{:else}<p class="text-muted-foreground text-sm">No se encontraron resultados.</p>{/if}</div></div>
{/if}
