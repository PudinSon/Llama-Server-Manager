<script lang="ts">
import AgentPanel from '$lib/components/agent/AgentPanel.svelte';
import { modalStore, closeAgentModal } from '$lib/stores/modal.svelte';
import { onMount } from 'svelte';

let open = $state(false);

const unsubscribe = modalStore.subscribe((value) => {
open = value.agent;
});

onMount(() => unsubscribe);
</script>

{#if open}
<div
class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
role="presentation"
onclick={(event) => event.target === event.currentTarget && closeAgentModal()}
>
<div
class="bg-background border-border/50 flex h-[min(80vh,720px)] w-full max-w-2xl flex-col overflow-hidden rounded-xl border p-0 shadow-2xl"
role="dialog"
aria-modal="true"
aria-label="Agentes"
>
<AgentPanel />
</div>
</div>
{/if}
