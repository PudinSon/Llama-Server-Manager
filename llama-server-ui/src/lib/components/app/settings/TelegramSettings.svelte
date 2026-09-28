<script lang="ts">
	import { MessageCircle, Power, Shield, Users, RefreshCw, AlertTriangle } from '@lucide/svelte';
	import { Button } from '$lib/components/ui/button';
	import * as Tooltip from '$lib/components/ui/tooltip';
	import { telegramStatus, telegramStore } from '$lib/stores/telegram.svelte';
	import { modalStore } from '$lib/stores/modal.svelte';

	let tokenInput = $state('');
	let pairingRequired = $state(true);
	let pairingCodeInput = $state('');
	let isLoading = $state(false);

	$effect(() => {
		if ($modalStore.telegram) void telegramStore.fetchConfig();
	});

	async function handleToggle() {
		if (!$telegramStore.config?.enabled) {
			if (!tokenInput.trim()) return;
			isLoading = true;
			await telegramStore.enableTelegram(tokenInput.trim(), pairingRequired);
			isLoading = false;
		} else {
			isLoading = true;
			await telegramStore.disableTelegram();
			isLoading = false;
		}
	}

	async function handleApprovePairing() {
		if (!pairingCodeInput.trim()) return;
		await telegramStore.requestPairingCode(pairingCodeInput);
		pairingCodeInput = '';
	}

	const statusColor = $derived(
		$telegramStatus === 'running'
			? 'text-emerald-500'
			: $telegramStatus === 'error'
				? 'text-destructive'
				: 'text-muted-foreground'
	);
</script>

<div class="space-y-4">
	<!-- Header -->
	<div class="flex items-center justify-between">
		<div class="flex items-center gap-2">
			<MessageCircle class="h-5 w-5 text-primary" />
			<h4 class="text-sm font-semibold">Telegram Bot</h4>
		</div>
		<div class="flex items-center gap-2">
			<span class="text-[10px] uppercase tracking-wider {statusColor}">
				{$telegramStore.config?.status === 'running' ? 'Activo' : $telegramStore.config?.status === 'error' ? 'Error' : 'Inactivo'}
			</span>
			<button
				type="button"
				onclick={() => void telegramStore.fetchConfig()}
				class="inline-flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
				aria-label="Actualizar"
			>
				<RefreshCw class="h-3 w-3" />
			</button>
		</div>
	</div>

	{#if $telegramStore.config?.enabled}
		<!-- Bot is enabled -->
		<div class="space-y-3">
			<div class="flex items-center justify-between rounded-lg border border-emerald-500/20 bg-emerald-500/5 px-3 py-2">
				<div class="flex items-center gap-2">
					<div class="h-2 w-2 rounded-full bg-emerald-500 animate-pulse"></div>
					<span class="text-xs font-medium text-emerald-600 dark:text-emerald-400">
						Bot activo
					</span>
				</div>
				<span class="text-[10px] text-muted-foreground">
					{$telegramStore.config?.active_users || 0} usuarios
				</span>
			</div>

			<!-- Pairing request -->
			{#if $telegramStore.config?.pairing_required}
				<div class="space-y-2 rounded-lg border border-border/50 bg-muted/30 p-3">
					<div class="flex items-center gap-2 text-xs text-muted-foreground">
						<Shield class="h-3.5 w-3.5" />
						<span>Solicitud de emparejamiento</span>
					</div>
					<div class="flex gap-2">
						<input
							type="text"
							inputmode="text"
							autocomplete="off"
							spellcheck="false"
							placeholder="Código (ej. OF56BO)"
							maxlength="8"
							pattern="[A-Za-z0-9]*"
							style="text-transform: uppercase; letter-spacing: 0.12em;"
							class="flex h-9 flex-1 rounded-md border border-input bg-background px-3 font-mono text-sm font-semibold tracking-wider placeholder:text-xs placeholder:font-normal placeholder:tracking-normal placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
							value={pairingCodeInput}
							oninput={(e) => {
								const v = (e.currentTarget as HTMLInputElement).value
									.toUpperCase()
									.replace(/[^A-Z0-9]/g, '')
									.slice(0, 8);
								pairingCodeInput = v;
								(e.currentTarget as HTMLInputElement).value = v;
							}}
						/>
						<Button
							variant="outline"
							size="sm"
							class="h-9 px-3 text-xs"
							onclick={handleApprovePairing}
							disabled={!pairingCodeInput.trim()}
						>
							Autorizar
						</Button>
					</div>
				</div>
			{/if}

			<!-- Disable button -->
			<Button
				variant="destructive"
				size="sm"
				class="w-full"
				onclick={handleToggle}
				disabled={isLoading}
			>
				<Power class="h-3.5 w-3.5" />
				{isLoading ? 'Desactivando...' : 'Desactivar bot'}
			</Button>
		</div>
	{:else}
		<!-- Bot is disabled - show setup form -->
		<div class="space-y-3">
			<div class="space-y-2">
				<label for="telegram-token" class="text-xs font-medium text-muted-foreground">
					Token del bot (de @BotFather)
				</label>
				<input
					id="telegram-token"
					type="password"
					placeholder="123456:ABC-DEF..."
					class="flex h-9 w-full rounded-md border border-input bg-background px-3 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
					bind:value={tokenInput}
				/>
			</div>

			<!-- Pairing toggle -->
			<div class="flex items-center gap-2">
				<input
					id="pairing-toggle"
					type="checkbox"
					bind:checked={pairingRequired}
					class="h-4 w-4 rounded border-input"
				/>
				<label for="pairing-toggle" class="text-xs text-muted-foreground">
					Requerir emparejamiento
				</label>
			</div>

			<Button
				variant="default"
				size="sm"
				class="w-full"
				onclick={handleToggle}
				disabled={!tokenInput.trim() || isLoading}
			>
				{#if isLoading}
					Activando...
				{:else}
					<Power class="h-3.5 w-3.5" />
					Activar bot
				{/if}
			</Button>
		</div>
	{/if}

	{#if $telegramStore.error}
		<div class="flex items-center justify-between rounded-md bg-destructive/10 px-3 py-2 text-xs text-destructive">
			<div class="flex items-center gap-2">
				<AlertTriangle class="h-3.5 w-3.5 shrink-0" />
				<span>{$telegramStore.error}</span>
			</div>
			<button class="ml-2 shrink-0 hover:opacity-70" onclick={() => {}}>✕</button>
		</div>
	{/if}
</div>