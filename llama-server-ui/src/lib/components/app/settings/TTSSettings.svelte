<script lang="ts">
	import { Phone, Volume2, Play, Square, AlertTriangle } from '@lucide/svelte';
	import { Button } from '$lib/components/ui/button';
	import * as Tooltip from '$lib/components/ui/tooltip';
	import { callStore } from '$lib/stores/call.svelte';
	import { VOICE_METADATA, VOICE_GRADES, type VoiceMeta } from '$lib/data/piper-voices';
	import { serviceUrl } from '$lib/api';
	import { onMount } from 'svelte';

	let isTestingVoice = $state(false);
	let testText = $state('Hola, esto es una prueba del sistema de texto a voz Piper.');
	let serviceStatus = $state<'unknown' | 'ok' | 'error'>('unknown');

	let healthInterval: ReturnType<typeof setInterval> | undefined;

	onMount(() => {
		if (!callStore.voicesLoaded) {
			callStore.fetchVoices();
		}
		checkServiceHealth();
		// Re-check every 5 seconds in case Piper starts after page load
		healthInterval = setInterval(checkServiceHealth, 5000);
		return () => {
			if (healthInterval) clearInterval(healthInterval);
		};
	});

	async function checkServiceHealth() {
		try {
			const resp = await fetch(serviceUrl('piper', '/health'), {
				signal: AbortSignal.timeout(3000)
			});
			const data = await resp.json();
			serviceStatus = data.status === 'healthy' || data.status === 'ok' ? 'ok' : 'error';
		} catch {
			serviceStatus = 'error';
		}
	}

	function handleToggle() {
		callStore.toggleTTS();
	}

	function handleVoiceChange(e: Event) {
		const select = e.target as HTMLSelectElement;
		// Voice change auto-syncs language via updateTTS
		callStore.updateTTS({ voice: select.value });
	}

	function handleSpeedChange(e: Event) {
		const input = e.target as HTMLInputElement;
		callStore.updateTTS({ speed: parseFloat(input.value) });
	}

	function handleLangChange(e: Event) {
		const select = e.target as HTMLSelectElement;
		callStore.updateTTS({ lang: select.value });
	}

	async function handleTestVoice() {
		console.info('[Piper TTS] Test button clicked', { enabled: callStore.ttsEnabled, text: testText });
		if (isTestingVoice) {
			callStore.stopPlayback();
			isTestingVoice = false;
			return;
		}

		isTestingVoice = true;
		try {
			await callStore.synthesizeAndPlay(testText);
		} catch (err) {
			console.error('[Piper TTS] Test voice failed:', err);
			callStore.ttsError = err instanceof Error ? err.message : 'No se pudo reproducir la prueba de voz';
		} finally {
			isTestingVoice = false;
		}
	}

	function dismissTtsError() {
		callStore.ttsError = null;
	}

	function getVoiceMeta(name: string): VoiceMeta | undefined {
		return VOICE_METADATA[name];
	}

	function getGradeInfo(grade: string) {
		return VOICE_GRADES[grade];
	}

	const currentVoiceMeta = $derived(getVoiceMeta(callStore.voice));

	// Force dark-mode compatible styles for native <select>
	// The issue is that browser native dropdowns don't respect CSS variables
	// in all contexts, so we use inline computed styles
	const selectStyle = `color-scheme: dark; background-color: var(--popover); color: var(--popover-foreground); border-color: var(--input);`;
	const inputStyle = `color-scheme: dark; background-color: var(--popover); color: var(--popover-foreground); border-color: var(--input);`;
</script>

<div class="space-y-6">
	<!-- Header -->
	<div class="flex items-center justify-between">
		<div class="flex items-center gap-2">
			<Phone class="h-5 w-5 text-primary" />
			<h4 class="text-sm font-semibold">TTS / Piper</h4>
		</div>
		{#if serviceStatus === 'error'}
			<Tooltip.Provider>
				<Tooltip.Root>
					<Tooltip.Trigger>
						{#snippet child({ props })}
							<span class="flex items-center gap-1 text-xs text-destructive" {...props}>
								<AlertTriangle class="h-3.5 w-3.5" />
								Servicio no disponible
							</span>
						{/snippet}
					</Tooltip.Trigger>
					<Tooltip.Content>
						<p class="max-w-64 text-xs">
							El servicio Piper FastAPI no está corriendo en puerto 8880.
							Verifica que app_data/piper esté activo.
						</p>
					</Tooltip.Content>
				</Tooltip.Root>
			</Tooltip.Provider>
		{:else if serviceStatus === 'ok'}
			<span class="flex items-center gap-1 text-xs text-green-500">
				<span class="h-1.5 w-1.5 rounded-full bg-green-500"></span>
				Conectado
			</span>
		{/if}
	</div>

	<!-- Enable/Disable Toggle -->
	<div class="flex items-center justify-between rounded-lg border border-border/50 p-4">
		<div class="space-y-1">
			<p class="text-sm font-medium">Enable TTS</p>
			<p class="text-xs text-muted-foreground">
				Synthesize LLM responses with Piper voice
			</p>
		</div>
		<button
			class="relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
			class:bg-primary={callStore.ttsEnabled}
			class:bg-input={!callStore.ttsEnabled}
			role="switch"
			aria-checked={callStore.ttsEnabled}
			aria-label="Enable TTS"
			onclick={handleToggle}
		>
			<span
				class="pointer-events-none inline-block h-5 w-5 transform rounded-full bg-background shadow-lg ring-0 transition duration-200 ease-in-out"
				class:translate-x-5={callStore.ttsEnabled}
				class:translate-x-0={!callStore.ttsEnabled}
			></span>
		</button>
	</div>	{#if callStore.ttsEnabled}
		<!-- Auto-Speak Toggle -->
		<div class="flex items-center justify-between rounded-lg border border-border/50 p-4">
			<div class="space-y-1">
				<p class="text-sm font-medium">Auto-leer respuestas</p>
				<p class="text-xs text-muted-foreground">
					Reproduce automáticamente la voz de cada respuesta del modelo
				</p>
			</div>
			<button
				class="relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
				class:bg-primary={callStore.autoSpeak}
				class:bg-input={!callStore.autoSpeak}
				role="switch"
				aria-checked={callStore.autoSpeak}
				aria-label="Auto-leer respuestas"
				onclick={() => callStore.toggleAutoSpeak()}
			>
				<span
					class="pointer-events-none inline-block h-5 w-5 transform rounded-full bg-background shadow-lg ring-0 transition duration-200 ease-in-out"
					class:translate-x-5={callStore.autoSpeak}
					class:translate-x-0={!callStore.autoSpeak}
				></span>
			</button>
		</div>

		<!-- Voice Selector with language groups -->
		<div class="space-y-2">
			<label for="tts-voice" class="text-sm font-medium">Voice</label>
			{#if callStore.voicesLoaded}
				<div class="space-y-1.5">
					<select
						id="tts-voice"
						class="flex h-9 w-full rounded-md border border-input px-3 py-1 text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
						style={selectStyle}
						value={callStore.voice}
						onchange={handleVoiceChange}
					>
						{#each callStore.voiceGroups as group}
							<optgroup label="{group.flag} {group.label} ({group.voices.length})">
								{#each group.voices as voice}
									<option value={voice.id ?? voice.name}>
										{voice.gender === 'f' ? '♀' : '♂'} {voice.name}
										{voice.traits ?? ''}
										{voice.grade ? ` [${voice.grade}]` : ''}
									</option>
								{/each}
							</optgroup>
						{/each}
					</select>

					<!-- Current voice details card -->
					{#if currentVoiceMeta}
						<div class="flex items-center gap-3 rounded-md bg-muted/30 px-3 py-2 text-xs">
							<span class="font-medium text-foreground">
								{currentVoiceMeta.gender === 'f' ? '♀' : '♂'} {currentVoiceMeta.name}
							</span>
							<span class="text-muted-foreground">
								{currentVoiceMeta.flag} {currentVoiceMeta.langLabel}
							</span>
							{#if currentVoiceMeta.traits}
								<span>{currentVoiceMeta.traits}</span>
							{/if}
							{#if currentVoiceMeta.grade && getGradeInfo(currentVoiceMeta.grade)}
								{@const gradeInfo = getGradeInfo(currentVoiceMeta.grade)}
								<span
									class="rounded-full px-1.5 py-0.5 text-[10px] font-bold leading-none"
									style="background-color: {gradeInfo.color}22; color: {gradeInfo.color}"
								>
									{currentVoiceMeta.grade}
								</span>
								<span class="text-muted-foreground">{gradeInfo.label}</span>
							{:else if currentVoiceMeta.grade}
								<span class="text-muted-foreground">Grado: {currentVoiceMeta.grade}</span>
							{/if}
						</div>
					{/if}
				</div>
			{:else}
				<div class="flex h-9 items-center rounded-md border border-input px-3 text-sm text-muted-foreground">
					Loading voices...
				</div>
			{/if}
		</div>

		<!-- Language selector (auto-syncs with voice) -->
		<div class="space-y-2">
			<div class="flex items-center justify-between">
				<label for="tts-lang" class="text-sm font-medium">Language</label>
				<span class="text-[10px] text-muted-foreground">Auto-synced with voice</span>
			</div>
			<select
				id="tts-lang"
				class="flex h-9 w-full rounded-md border border-input px-3 py-1 text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
				style={selectStyle}
				value={callStore.ttsLang}
				onchange={handleLangChange}
			>
				<option value="en">🇺🇸 English (US)</option>
				<option value="es">🇲🇽 Español (MX)</option>
			</select>
		</div>

		<!-- Speed Slider -->
		<div class="space-y-2">
			<div class="flex items-center justify-between">
				<label for="tts-speed" class="text-sm font-medium">Speed</label>
				<span class="text-xs text-muted-foreground tabular-nums">{callStore.ttsSpeed.toFixed(1)}x</span>
			</div>
			<input
				id="tts-speed"
				type="range"
				min="0.5"
				max="2.0"
				step="0.1"
				value={callStore.ttsSpeed}
				oninput={handleSpeedChange}
				class="w-full accent-primary"
			/>
			<div class="flex justify-between text-[10px] text-muted-foreground">
				<span>0.5x</span>
				<span>1.0x</span>
				<span>2.0x</span>
			</div>
		</div>

		<!-- Test Voice -->
		<div class="space-y-2">
			<label for="tts-test" class="text-sm font-medium">Test</label>
			<div class="flex gap-2">
				<input
					id="tts-test"
					type="text"
					class="flex h-9 flex-1 rounded-md border border-input px-3 py-1 text-sm shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
					style={inputStyle}
					placeholder="Test text..."
					bind:value={testText}
				/>
				<Tooltip.Provider>
					<Tooltip.Root>
						<Tooltip.Trigger>
							{#snippet child({ props })}
							<Button
								variant="outline"
								size="icon"
								class="h-9 w-9 shrink-0"
								aria-label={isTestingVoice ? 'Stop voice test' : 'Play voice test'}
								{...props}
								onclick={handleTestVoice}
								type="button"
							>
									{#if isTestingVoice}
										<Square class="h-3.5 w-3.5" />
									{:else}
										<Play class="h-3.5 w-3.5" />
									{/if}
								</Button>
							{/snippet}
						</Tooltip.Trigger>
						<Tooltip.Content>
							<p>{isTestingVoice ? 'Stop' : 'Play test'}</p>
						</Tooltip.Content>
					</Tooltip.Root>
				</Tooltip.Provider>
			</div>
		</div>

		<!-- TTS Error -->
		{#if callStore.ttsError}
			<div class="flex items-center justify-between rounded-md bg-destructive/10 px-3 py-2 text-xs text-destructive">
				<div class="flex items-center gap-2">
					<AlertTriangle class="h-3.5 w-3.5 shrink-0" />
					<span>{callStore.ttsError}</span>
				</div>
				<button class="ml-2 shrink-0 hover:opacity-70" onclick={dismissTtsError}>✕</button>
			</div>
		{/if}

		<!-- Status indicator -->
		{#if callStore.isTtsPlaying}
			<div class="flex items-center gap-2 text-xs text-primary">
				<Volume2 class="h-3.5 w-3.5 animate-pulse" />
				<span>Playing audio...</span>
			</div>
		{/if}
	{/if}
</div>
