<script lang="ts">
	import { X } from '@lucide/svelte';
	import TelegramSettings from '$lib/components/app/settings/TelegramSettings.svelte';
	import { modalStore, closeTelegramModal } from '$lib/stores/modal.svelte';
</script>

{#if $modalStore.telegram}
	<div class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" role="presentation" onclick={(event) => event.target === event.currentTarget && closeTelegramModal()}>
		<div class="bg-background w-full max-w-lg rounded-xl border border-border/50 p-5 shadow-2xl" role="dialog" aria-modal="true" aria-labelledby="telegram-modal-title">
			<div class="mb-4 flex items-center justify-between">
				<h2 id="telegram-modal-title" class="text-base font-semibold">Telegram</h2>
				<button type="button" onclick={closeTelegramModal} aria-label="Cerrar"><X class="h-4 w-4" /></button>
			</div>
			<TelegramSettings />
		</div>
	</div>
{/if}