<script lang="ts">
import { agentStore } from '$lib/stores/agentStore.svelte';
import { conversationsStore } from '$lib/stores/conversations/index.svelte';
import { Bot, Loader2, RefreshCw, X } from '@lucide/svelte';
import { closeAgentModal } from '$lib/stores/modal.svelte';
import AgentMemory from './AgentMemory.svelte';
import AgentCheckpoints from './AgentCheckpoints.svelte';
import AgentPlan from './AgentPlan.svelte';
import AgentStatus from './AgentStatus.svelte';
import AgencyAgentSelector from './AgencyAgentSelector.svelte';
import AgentManageModal from './AgentManageModal.svelte';
import { Settings2 } from '@lucide/svelte';

let refreshedOnOpen = false;
let showConfig = $state(false);
let showManage = $state(false);

$effect(() => {
if (refreshedOnOpen) return;
refreshedOnOpen = true;
const conversationId = conversationsStore.activeConversation?.id;
void agentStore.refreshAll(conversationId);
void agentStore.fetchAgencyAgents();
});

function closeModal() {
closeAgentModal();
}

function refresh() {
void agentStore.refreshAll(conversationsStore.activeConversation?.id);
void agentStore.fetchAgencyAgents();
}
</script>

<div class="flex h-full min-h-0 w-full flex-col overflow-hidden bg-background">
<div class="flex items-center justify-between border-b border-border/50 px-4 py-3">
<div class="flex items-center gap-2">
{#if agentStore.isExecuting}
<Loader2 class="h-4 w-4 animate-spin text-primary" />
{:else}
<Bot class="h-4 w-4 text-primary" />
{/if}
<span class="text-sm font-semibold">Agentes</span>
<span
class="h-2.5 w-2.5 rounded-full border border-background {agentStore.isConnected === true
? 'bg-emerald-500'
: agentStore.isConnected === false
? 'bg-red-500'
: 'bg-muted-foreground'}"
title={agentStore.isConnected === true
? 'Conectado'
: agentStore.isConnected === false
? 'Sin conexión'
: 'Conectando…'}
></span>
</div>
<div class="flex items-center gap-1">
<button
type="button"
class="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
onclick={refresh}
aria-label="Actualizar"
title="Actualizar estado"
>
<RefreshCw class="h-3.5 w-3.5" />
</button>
<button
  type="button"
  class="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
  onclick={() => (showManage = true)}
  aria-label="Gestionar agentes"
  title="Importar, ver, editar o eliminar agentes"
>
  <Settings2 class="h-3.5 w-3.5" />
</button>
<button
type="button"
class="inline-flex h-7 items-center justify-center rounded-md px-2 text-xs font-medium text-muted-foreground hover:bg-accent hover:text-foreground"
onclick={() => (showConfig = !showConfig)}
aria-label="Configuración de selección"
title="Modo de selección y ejecución"
>
{agentStore.config.selectionMode === 'manual'
? 'Manual'
: agentStore.config.selectionMode === 'hybrid'
? 'Híbrido'
: 'Automático'}
<span class="mx-1 text-border">·</span>
{agentStore.config.executionMode === 'sequential' ? 'Secuencial' : 'Paralelo'}
</button>
<button
type="button"
class="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
onclick={closeModal}
aria-label="Cerrar agentes"
>
<X class="h-3.5 w-3.5" />
</button>
</div>
</div>

{#if showConfig}
<div class="border-b border-border/50 bg-muted/30 space-y-2 px-4 py-3">
<label class="flex items-center justify-between text-xs">
<span class="text-muted-foreground">Selección de agentes</span>
<select
class="rounded-md border border-border bg-background px-2 py-1 text-xs"
value={agentStore.config.selectionMode}
onchange={(e) =>
agentStore.updateConfig({
selectionMode: e.currentTarget.value as 'manual' | 'automatic' | 'hybrid'
})}
>
<option value="manual">Manual</option>
<option value="hybrid">Híbrido</option>
<option value="automatic">Automático</option>
</select>
</label>
<label class="flex items-center justify-between text-xs">
<span class="text-muted-foreground">Ejecución</span>
<select
class="rounded-md border border-border bg-background px-2 py-1 text-xs"
value={agentStore.config.executionMode}
onchange={(e) =>
agentStore.updateConfig({
executionMode: e.currentTarget.value as 'parallel' | 'sequential'
})}
>
<option value="sequential">Secuencial</option>
<option value="parallel">Paralelo</option>
</select>
</label>
</div>
{/if}

<div class="min-h-0 flex-1 overflow-y-auto px-4 py-3">
<div class="space-y-5">
<AgentStatus />
<div class="h-px bg-border/50"></div>
<AgencyAgentSelector />
<div class="h-px bg-border/50"></div>
<AgentMemory />
<div class="h-px bg-border/50"></div>
<AgentPlan />
<div class="h-px bg-border/50"></div>
<AgentCheckpoints />
</div>
</div>
</div>

{#if showManage}
<AgentManageModal onClose={() => (showManage = false)} />
{/if}
