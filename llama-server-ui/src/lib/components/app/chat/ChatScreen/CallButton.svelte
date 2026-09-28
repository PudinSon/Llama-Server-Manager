<script lang="ts">
	import { Phone, PhoneOff, Loader2 } from '@lucide/svelte';
	import { Button } from '$lib/components/ui/button';
	import * as Tooltip from '$lib/components/ui/tooltip';
	import { callStore } from '$lib/stores/call.svelte';
	import { serviceUrl } from '$lib/api';
	import { onDestroy, onMount } from 'svelte';

	let animFrame = $state<number | null>(null);
	let serviceOk = $state<boolean | null>(null);
	let healthInterval: ReturnType<typeof setInterval> | undefined;

	// Animated equalizer bars
	const BAR_COUNT = 12;
	const bars = $state(new Array(BAR_COUNT).fill(0));

	onMount(() => {
		startEqualizerLoop();
		checkHealth();
		callStore.loadInputDevices();
		healthInterval = setInterval(checkHealth, 5000);
		return () => {
			if (healthInterval) clearInterval(healthInterval);
		};
	});

	onDestroy(() => {
		if (animFrame) cancelAnimationFrame(animFrame);
	});

	async function checkHealth(): Promise<boolean> {
		try {
			const resp = await fetch(serviceUrl('piper', '/health'), {
				signal: AbortSignal.timeout(3000)
			});
			const data = await resp.json();
			serviceOk = resp.ok && (data.status === 'healthy' || data.status === 'ok');
			return serviceOk;
		} catch (err) {
			console.warn('[Piper Call] Health check failed:', err);
			serviceOk = false;
			return false;
		}
	}

	function startEqualizerLoop() {
		const tick = () => {
			if (callStore.state === 'active') {
				const data = callStore.getEqualizerData();
				if (data && data.length > 0) {
					for (let i = 0; i < BAR_COUNT; i++) {
						const start = Math.floor(i * data.length / BAR_COUNT);
						const end = Math.max(start + 1, Math.floor((i + 1) * data.length / BAR_COUNT));
						const value = Math.max(...data.slice(start, end)) / 255;
						bars[i] = Math.max(value, bars[i] * 0.7);
					}
					const vadValue = Math.min(1, callStore.vadScore * 2);
					for (let i = 0; i < BAR_COUNT; i++) bars[i] = Math.max(bars[i], vadValue);
				} else {
					for (let i = 0; i < BAR_COUNT; i++) {
						bars[i] *= 0.85;
					}
				}
			} else {
				for (let i = 0; i < BAR_COUNT; i++) {
					bars[i] *= 0.85;
				}
			}
			animFrame = requestAnimationFrame(tick);
		};
		animFrame = requestAnimationFrame(tick);
	}

	async function handleToggle() {
		console.info('[Piper Call] handleToggle', { state: callStore.state, serviceOk });
		if (callStore.state === 'idle') {
			console.info('[Piper Call] Start button clicked');
			const healthy = await checkHealth();
			if (!healthy) {
				console.error('[Piper Call] Cannot start: Piper health check failed');
				callStore.error = 'El servicio Piper FastAPI no está disponible en puerto 8880';
				return;
			}
			try {
				callStore.error = 'Solicitando permiso de micrófono...';
				await callStore.startCall();
			} catch (err) {
				console.error('[Piper Call] Start failed:', err);
				callStore.error = err instanceof Error ? err.message : 'No se pudo iniciar la llamada';
			}
		} else {
			console.info('[Piper Call] Stop button clicked');
			callStore.stopCall();
		}
	}

	function dismissError() {
		callStore.error = null;
	}

	function handleDeviceChange(event: Event) {
		callStore.setInputDevice((event.currentTarget as HTMLSelectElement).value);
	}

	function handleSttLanguageChange(event: Event) {
		callStore.sttLanguage = (event.currentTarget as HTMLSelectElement).value;
	}

	const isActive = $derived(callStore.state === 'active' || callStore.state === 'connecting');
	const isConnecting = $derived(callStore.state === 'connecting');

	// Medidor de nivel de micrófono: verde OK, ámbar justo, rojo demasiado bajo.
	// Umbral alto (0.06) porque en reposo el ruido de fondo da picos ~0.005-0.01;
	// si ni hablando se supera 0.06, Whisper difícilmente transcribirá.
	const MIC_LOW = 0.06;
	const MIC_FAIR = 0.25;
	const micLevel = $derived(callStore.micInputLevel);
	const micQuality = $derived(micLevel >= MIC_FAIR ? 'ok' : micLevel >= MIC_LOW ? 'fair' : 'low');
	const tooltipText = $derived(
		callStore.state === 'idle'
			? serviceOk === false
				? 'Servicio no disponible'
				: 'Iniciar llamada'
			: callStore.state === 'connecting'
				? 'Conectando...'
				: 'Finalizar llamada'
	);
</script>

<div class="call-button-wrapper flex items-center gap-2">
	<Tooltip.Root>
		<Tooltip.Trigger>
			{#snippet child({ props })}
				<Button
					class={[
						'rounded-full h-10 w-10 flex items-center justify-center transition-all duration-200',
						isActive
							? 'bg-red-500 hover:bg-red-600 text-white shadow-lg shadow-red-500/30'
							: serviceOk === false
								? 'bg-destructive/10 text-destructive opacity-60'
								: 'bg-primary/10 hover:bg-primary/20 text-primary'
					]}
					variant="ghost"
					size="icon"
					{...props}
					onclick={handleToggle}
					type="button"
				>
					{#if isConnecting}
						<Loader2 class="h-4.5 w-4.5 animate-spin" />
					{:else if isActive}
						<PhoneOff class="h-4.5 w-4.5" />
					{:else}
						<Phone class="h-4.5 w-4.5" />
					{/if}
				</Button>
			{/snippet}
		</Tooltip.Trigger>
		<Tooltip.Content>
			<p>{tooltipText}</p>
		</Tooltip.Content>
	</Tooltip.Root>

	<!-- Equalizer bars -->
	{#if isActive}
		<div
			class="equalizer flex items-end gap-[2px] h-6 transition-opacity duration-300"
			class:opacity-100={callStore.state === 'active'}
			class:opacity-50={callStore.state === 'connecting'}
		>
			{#each bars as bar, i}
				<div
					class="equalizer-bar w-[3px] rounded-full transition-all duration-75"
					class:bg-primary={callStore.isSpeaking}
					class:bg-muted-foreground={!callStore.isSpeaking && bar > 0.01}
					class:bg-green-500={callStore.isTtsPlaying}
					style="height: {Math.max(2, bar * 24)}px;"
				></div>
			{/each}
		</div>
	{/if}

	{#if callStore.inputDevices.length > 1}
		<select class="h-8 max-w-56 rounded-md border border-input bg-background px-2 text-xs" value={callStore.selectedInputDeviceId} onchange={handleDeviceChange} aria-label="Micrófono de llamada" disabled={callStore.state !== 'idle'}>
			{#each callStore.inputDevices as device, index}
				<option value={device.deviceId}>{device.label || `Micrófono ${index + 1}`}</option>
			{/each}
		</select>
	{/if}

	{#if callStore.state === 'idle'}
		<select class="h-8 rounded-md border border-input bg-background px-2 text-xs" value={callStore.sttLanguage} onchange={handleSttLanguageChange} aria-label="Idioma de transcripción">
			<option value="es">Español</option>
			<option value="en">English</option>
			<option value="fr">Français</option>
			<option value="de">Deutsch</option>
			<option value="it">Italiano</option>
			<option value="pt">Português</option>
			<option value="auto">Auto</option>
		</select>
	{/if}

	<!-- Status indicator -->
	{#if callStore.state === 'active'}
		<div class="flex items-center gap-1 text-xs text-muted-foreground">
			{#if callStore.isSpeaking}
				<span class="text-primary">Hablando</span>
			{:else if callStore.isCallProcessing}
				<span class="text-amber-500">Procesando</span>
			{:else if callStore.isTtsPlaying}
				<span class="text-green-500">Reproduciendo</span>
			{:else}
				<span>Escuchando</span>
			{/if}
		</div>
	{/if}

	<!-- Medidor de nivel de entrada del micrófono (solo en llamada activa) -->
	{#if callStore.state === 'active'}
		<div
			class="flex items-center gap-1.5"
			title="Nivel del micrófono. Si no sube al hablar, sube el volumen de entrada del sistema o el nivel del mic."
		>
			<div class="flex h-2 w-20 items-center gap-[2px]" aria-hidden="true">
				{#each Array(10) as _, i}
					<div
						class="h-2 w-[6px] rounded-sm transition-colors duration-100 {
							micLevel * 10 > i
								? i < 6
									? 'bg-green-500'
									: i < 8
										? 'bg-amber-500'
										: 'bg-red-500'
								: 'bg-muted'
						} {micQuality === 'low' ? 'opacity-100' : ''}"
					></div>
				{/each}
			</div>
			{#if micQuality === 'low'}
				<span class="text-xs font-medium text-red-500">volumen bajo</span>
			{/if}
		</div>
	{/if}

	<!-- Error indicator - visible and dismissible -->
	{#if callStore.error}
		<div class="flex items-center gap-2 rounded-md bg-destructive/10 px-3 py-1.5 text-xs text-destructive max-w-80">
			<span class="shrink-0">⚠️</span>
			<span class="flex-1 leading-tight">{callStore.error}</span>
			<button
				class="shrink-0 ml-1 hover:opacity-70 font-bold"
				onclick={dismissError}
				aria-label="Cerrar error"
			>
				✕
			</button>
		</div>
	{/if}
</div>

<style>
	.equalizer-bar {
		min-height: 2px;
	}
</style>
