<script lang="ts">
	import { Volume2, Loader2 } from '@lucide/svelte';
	import { callStore } from '$lib/stores/call.svelte';
	import { ActionIcon } from '$lib/components/app';

	interface Props {
		text: string;
	}

	let { text }: Props = $props();

	let isPlaying = $state(false);

	async function handleSpeak() {
		if (isPlaying) {
			callStore.stopPlayback();
			isPlaying = false;
			return;
		}

		isPlaying = true;
		try {
			await callStore.synthesizeAndPlay(text);
		} catch (err) {
			console.error('TTS playback failed:', err);
		} finally {
			isPlaying = false;
		}
	}
</script>

<ActionIcon
	icon={isPlaying ? Loader2 : Volume2}
	tooltip={isPlaying ? 'Stop' : 'Listen'}
	onclick={handleSpeak}
/>
