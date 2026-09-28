<script lang="ts">
	import { telegramStore, telegramEnabled, telegramStatus } from '$lib/stores/telegram.svelte';
	import { MessageCircle, Copy, RefreshCcw } from '@lucide/svelte';
	import { onMount } from 'svelte';
	import { toast } from 'svelte-sonner';

	let token = $state('');
	let pairingCode = $state('');
	let loading = $state(false);

	onMount(() => {
		void telegramStore.fetchConfig();
	});

	async function handleEnable() {
		if (!token.trim()) {
			toast.error('Token requerido');
			return;
		}
		loading = true;
		await telegramStore.enableTelegram(token.trim(), Boolean(pairingCode.trim()));
		loading = false;
		token = '';
		pairingCode = '';
	}

	async function handleDisable() {
		loading = true;
		await telegramStore.disableTelegram();
		loading = false;
	}

	async function handleApproveCode() {
		if (!pairingCode.trim()) {
			toast.error('Código requerido');
			return;
		}
		loading = true;
		await telegramStore.requestPairingCode(pairingCode);
		loading = false;
		pairingCode = '';
	}

	function copyToClipboard(text: string) {
		navigator.clipboard.writeText(text);
		toast.success('Copiado');
	}
</script>

<div class="space-y-3">
	<div class="flex items-center justify-between">
		<span class="text-xs font-medium uppercase tracking-wider text-muted-foreground">
			Telegram
		</span>
		<span
			class="inline-flex shrink-0 items-center rounded-full px-2 py-1 text-[10px] font-semibold {$telegramEnabled
				? 'bg-emerald-500/20 text-emerald-600 dark:text-emerald-400'
				: 'bg-muted text-muted-foreground'}"
		>
			{$telegramEnabled ? 'Conectado' : 'Desconectado'}
		</span>
	</div>

	{#if $telegramEnabled}
		<div class="rounded-lg border border-emerald-500/20 bg-emerald-500/10 p-3">
			<div class="space-y-2">
				<div class="flex items-start gap-2">
					<MessageCircle class="h-4 w-4 mt-0.5 text-emerald-600 dark:text-emerald-400" />
					<div class="text-xs text-emerald-600 dark:text-emerald-400">
						<p class="font-medium">Bot conectado</p>
						<p class="mt-1 text-[11px] leading-relaxed">{$telegramStatus}</p>
					</div>
				</div>
			</div>

			<button
				type="button"
				onclick={handleDisable}
				disabled={loading}
				class="mt-3 w-full rounded-md bg-destructive px-2.5 py-1.5 text-xs font-medium text-destructive-foreground transition-opacity hover:opacity-90 disabled:opacity-50"
			>
				{loading ? 'Desconectando...' : 'Desconectar'}
			</button>
		</div>
	{:else}
		<div class="space-y-2 rounded-lg border border-border/50 bg-muted/30 p-3">
			<label class="space-y-1 text-[11px] text-muted-foreground">
				<span>Bot Token</span>
				<input
					bind:value={token}
					type="password"
					placeholder="Ej. 123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
					class="w-full rounded-md border border-border/50 bg-background px-2 py-1.5 text-xs text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
				/>
			</label>

			<label class="space-y-1 text-[11px] text-muted-foreground">
				<span>Código de /pairing (lo genera el bot)</span>
				<input
					bind:value={pairingCode}
					type="text"
					maxlength="6"
					placeholder="ABC123"
					style="text-transform: uppercase"
					class="w-full rounded-md border border-border/50 bg-background px-2 py-1.5 text-xs text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
				/>
			</label>

			<div class="flex gap-2">
				<button
					type="button"
					onclick={handleApproveCode}
					disabled={loading || !pairingCode.trim()}
					class="flex-1 inline-flex items-center justify-center gap-1 rounded-md border border-border/50 px-2 py-1.5 text-[11px] font-medium text-foreground transition-colors hover:bg-accent disabled:opacity-50"
				>
					<RefreshCcw class="h-3.5 w-3.5" />
					Autorizar código
				</button>

				<button
					type="button"
					onclick={handleEnable}
					disabled={loading || !token.trim()}
					class="flex-1 rounded-md bg-primary px-2.5 py-1.5 text-xs font-medium text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-50"
				>
					{loading ? 'Conectando...' : 'Conectar'}
				</button>
			</div>
		</div>
	{/if}
</div>
