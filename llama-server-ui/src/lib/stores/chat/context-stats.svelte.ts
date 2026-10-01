/**
 * ContextStatsStore - Context window usage stats for the active conversation
 *
 * Combines token usage persisted in message timings metadata with
 * server-originating data: model context size from /props (modelsStore)
 * and live processing state while streaming (chatStore).
 *
 * El modo agente se contabiliza APARTE. Los turnos de una ejecución agencial
 * (`agencyStep`) llevan el total ACUMULADO del job, no el del turno, así que
 * sumarlos al chat nativo multiplicaba el consumo y el gauge marcaba un
 * contexto inexistente. Aquí quedan fuera de `read/cacheTotal/output` y de
 * `lastTimings`, y se agregan en `agentRead/agentCacheTotal/agentOutput` para
 * mostrarlos como consumo propio del modo agente.
 */

import { MessageRole } from '$lib/enums';
// direct imports between stores, not via the barrel, to avoid circular deps
import { agentStore } from '$lib/stores/agentStore.svelte';
import { agenticStore } from '$lib/stores/agentic/index.svelte';
import { chatStore } from '$lib/stores/chat/index.svelte';
import { conversationsStore } from '$lib/stores/conversations/index.svelte';
import { modelsStore } from '$lib/stores/models/index.svelte';
import { serverStore } from '$lib/stores/server.svelte';
import type {
	ApiProcessingState,
	ChatMessageAgenticTimings,
	ChatMessageTimings,
	DatabaseMessage
} from '$lib/types';

interface LiveStats {
	freshTokens: number;
	promptTokens: number;
	cacheTokens: number;
	outputTokens: number;
}

interface AssistantTimingsSummary {
	lastAgenticLlm: ChatMessageAgenticTimings['llm'] | undefined;
	lastTimings: ChatMessageTimings | undefined;
	cacheTotal: number;
	output: number;
	outputMs: number;
	read: number;
	/** Tokens de prompt del modo agente (suma de los deltas por job). */
	agentRead: number;
	/** Tokens reutilizados de la KV cache en el modo agente. */
	agentCacheTotal: number;
	/** Tokens generados por el modo agente (suma de los deltas por job). */
	agentOutput: number;
}

/**
 * One forward pass over the messages computing everything the deriveds
 * below need: the last assistant timings (per-turn gauges), the last
 * agentic llm totals (cumulative gauge) and the cumulative sums. During
 * streaming activeMessages churns every chunk, and each of these used to be
 * its own O(n) scan re-run per chunk.
 *
 * Los turnos del modo agente (`agencyStep`) se agregan por separado: dentro de
 * un job cada mensaje lleva el total acumulado del job, así que el consumo real
 * es la suma de los incrementos, no la de los totales.
 */
export function summarizeAssistantTimings(messages: DatabaseMessage[]): AssistantTimingsSummary {
	let lastAgenticLlm: ChatMessageAgenticTimings['llm'] | undefined;
	let lastTimings: ChatMessageTimings | undefined;
	let read = 0;
	let cacheTotal = 0;
	let output = 0;
	let outputMs = 0;
	let agentRead = 0;
	let agentCacheTotal = 0;
	let agentOutput = 0;
	// Último turno agencial visto y si el turno anterior del hilo fue agencial:
	// juntos marcan dónde los totales acumulados de un job empiezan de cero.
	let previousAgentTurn: { cache: number; output: number; prompt: number } | null = null;
	let previousTurnWasAgent = false;

	for (const m of messages) {
		if (m.role !== MessageRole.ASSISTANT || !m.timings) continue;

		if (m.agencyStep) {
			const prompt = m.timings.prompt_n ?? 0;
			const cache = m.timings.cache_n ?? 0;
			const generated = m.timings.predicted_n ?? 0;
			// Un turno agencial después de uno nativo abre un job nuevo, y sus
			// totales vuelven a empezar: sin base previa se suma el total entero.
			const baseline = previousTurnWasAgent ? previousAgentTurn : null;

			// Dentro de un job los totales solo crecen, así que lo consumido es
			// la diferencia con el turno anterior. Una bajada delata otro job,
			// que arranca su propia cuenta desde cero.
			if (baseline === null || prompt < baseline.prompt || generated < baseline.output) {
				agentRead += prompt;
				agentCacheTotal += cache;
				agentOutput += generated;
			} else {
				agentRead += prompt - baseline.prompt;
				agentCacheTotal += cache - baseline.cache;
				agentOutput += generated - baseline.output;
			}

			previousAgentTurn = { cache, output: generated, prompt };
			previousTurnWasAgent = true;

			continue;
		}

		previousTurnWasAgent = false;
		lastTimings = m.timings;

		if (m.timings.agentic?.llm?.predicted_n != null) {
			lastAgenticLlm = m.timings.agentic.llm;
		}

		read += m.timings.prompt_n ?? 0;
		cacheTotal += m.timings.cache_n ?? 0;
		output += m.timings.predicted_n ?? 0;
		outputMs += m.timings.predicted_ms ?? 0;
	}

	return {
		agentCacheTotal,
		agentOutput,
		agentRead,
		cacheTotal,
		lastAgenticLlm,
		lastTimings,
		output,
		outputMs,
		read
	};
}

function deriveLiveStats(state: ApiProcessingState | null): LiveStats | null {
	if (!state || (state.status !== 'preparing' && state.status !== 'generating')) {
		return null;
	}

	const promptTokens = state.promptTokens ?? 0;
	const cacheTokens = state.cacheTokens ?? 0;

	return {
		cacheTokens,
		freshTokens: promptTokens,
		outputTokens: state.outputTokensUsed ?? 0,
		promptTokens: promptTokens + cacheTokens
	};
}

class ContextStatsStore {
	// The canonical resolution lives in modelsStore.activeModelId.
	activeModelId = $derived(modelsStore.activeModelId);

	// shared by currentRead/Fresh/Cache/Output and cumulative so a per-chunk
	// churn of activeMessages triggers exactly one scan instead of one per
	// derived
	private assistantTimings = $derived.by(() =>
		summarizeAssistantTimings(conversationsStore.activeMessages as DatabaseMessage[])
	);

	private cumulative = $derived.by(() => {
		const convId = conversationsStore.activeConversation?.id;
		// A running agentic flow stamps llm totals on messages only when it
		// exits, so read its live session totals instead.
		const liveLlm = convId ? agenticStore.getLiveLlmTotals(convId) : null;

		if (liveLlm) {
			const outputMs = liveLlm.predicted_ms;
			const averageTokensPerSecond =
				outputMs > 0 && liveLlm.predicted_n > 0 ? (liveLlm.predicted_n / outputMs) * 1000 : null;

			return {
				averageTokensPerSecond,
				cacheTotal: 0,
				output: liveLlm.predicted_n,
				read: liveLlm.prompt_n
			};
		}

		const { cacheTotal, lastAgenticLlm, output, outputMs, read } = this.assistantTimings;

		// Agentic sessions stamp the same agentic.llm totals onto every
		// assistant message; cache_n is never per-turn so cache_total stays 0.
		if (lastAgenticLlm) {
			const averageTokensPerSecond =
				lastAgenticLlm.predicted_ms > 0 && lastAgenticLlm.predicted_n > 0
					? (lastAgenticLlm.predicted_n / lastAgenticLlm.predicted_ms) * 1000
					: null;

			return {
				averageTokensPerSecond,
				cacheTotal: 0,
				output: lastAgenticLlm.predicted_n ?? 0,
				read: lastAgenticLlm.prompt_n ?? 0
			};
		}

		const averageTokensPerSecond = outputMs > 0 && output > 0 ? (output / outputMs) * 1000 : null;

		return { averageTokensPerSecond, cacheTotal, output, read };
	});

	averageTokensPerSecond = $derived(this.cumulative.averageTokensPerSecond);

	contextTotal = $derived.by(() => {
		void modelsStore.props.cacheVersion;

		return this.activeModelId ? modelsStore.props.getModelContextSize(this.activeModelId) : null;
	});

	/**
	 * ¿El servidor está generando para un job agencial de ESTA conversación?
	 * `chatStore.consumeAgencyJob` alimenta el mismo estado de procesamiento que
	 * el chat, así que sin esta marca el consumo en vivo del agente entraría en
	 * el contador del chat igual que el consumo ya persistido.
	 */
	private agencyRunActive = $derived.by(() => {
		const convId = conversationsStore.activeConversation?.id ?? null;

		return convId !== null && agentStore.isExecuting && agentStore.agencyConversationId === convId;
	});

	private liveStats = $derived(
		this.agencyRunActive ? null : deriveLiveStats(chatStore.processing.activeState)
	);

	currentOutput = $derived.by(() => {
		if (this.liveStats && this.liveStats.outputTokens > 0) return this.liveStats.outputTokens;

		return this.assistantTimings.lastTimings?.predicted_n ?? 0;
	});

	currentRead = $derived.by(() => {
		const timings = this.assistantTimings.lastTimings;

		let read = 0;

		if (timings) {
			read = (timings.prompt_n ?? 0) + (timings.cache_n ?? 0);
		}

		// live.promptTokens is already the combined reading (prompt + cache),
		// so do not also add live.cacheTokens.
		if (this.liveStats && this.liveStats.promptTokens > 0) {
			read = Math.max(read, this.liveStats.promptTokens);
		}

		return read;
	});

	contextUsed = $derived(this.currentRead + this.currentOutput);

	contextAvailable = $derived(
		this.contextTotal !== null ? this.contextTotal - this.contextUsed : null
	);

	contextPercent = $derived.by(() => {
		if (this.contextTotal === null || this.contextTotal <= 0) return null;

		return Math.round((this.contextUsed / this.contextTotal) * 100);
	});

	cumulativeCacheTotal = $derived(this.cumulative.cacheTotal);

	cumulativeOutput = $derived(this.cumulative.output);

	cumulativeRead = $derived(this.cumulative.read);

	currentCache = $derived.by(() => {
		const cached = this.assistantTimings.lastTimings?.cache_n ?? 0;

		if (this.liveStats && this.liveStats.promptTokens > 0) {
			return Math.max(cached, this.liveStats.cacheTokens);
		}

		return cached;
	});

	currentFresh = $derived.by(() => {
		const fresh = this.assistantTimings.lastTimings?.prompt_n ?? 0;

		return Math.max(fresh, this.liveStats?.freshTokens ?? 0);
	});

	/** Tokens de prompt del modo agente (fuera del contexto del chat). */
	agentPromptTokens = $derived(this.assistantTimings.agentRead);

	/** Tokens reutilizados de la KV cache durante el modo agente. */
	agentCacheTotal = $derived(this.assistantTimings.agentCacheTotal);

	/** Tokens generados por el modo agente (fuera del contexto del chat). */
	agentOutputTokens = $derived(this.assistantTimings.agentOutput);

	/** Consumo total del modo agente, el que el gauge NO cuenta como contexto. */
	agentTokens = $derived(this.agentPromptTokens + this.agentOutputTokens);

	isActiveModelLoaded = $derived(
		this.activeModelId !== null &&
			(!serverStore.isRouterMode || modelsStore.isModelLoaded(this.activeModelId))
	);

	isActiveModelLoading = $derived(
		this.activeModelId !== null && modelsStore.status.isOperationInProgress(this.activeModelId)
	);

	kvTotal = $derived(this.currentRead + this.currentOutput);
}

export const contextStatsStore = new ContextStatsStore();
