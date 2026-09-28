<script lang="ts">
	import { Search } from '@lucide/svelte';
	import { ragResearchMode, ragStore } from '$lib/stores/rag.svelte';

	/**
	 * Botón de acceso directo al modo investigación RAG real del chat
	 * (el mismo método del menú `+` → Investigación RAG).
	 */
	let enabled = $derived($ragResearchMode);

	function toggle() {
		void ragStore.toggleResearchMode();
	}
</script>

<button
	type="button"
	class="relative inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-border/50 transition-all hover:bg-accent"
	class:bg-primary={enabled}
	class:text-primary-foreground={enabled}
	title={enabled ? 'Desactivar investigación RAG' : 'Activar investigación RAG'}
	aria-label={enabled ? 'Desactivar investigación RAG' : 'Activar investigación RAG'}
	aria-pressed={enabled}
	onclick={toggle}
>
	<Search class="h-4 w-4" />
</button>