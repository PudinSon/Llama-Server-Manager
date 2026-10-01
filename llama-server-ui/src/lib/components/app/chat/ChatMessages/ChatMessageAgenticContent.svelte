<script lang="ts">
	import ChatMessageToolCallBlock from './ChatMessage/ChatMessageToolCall/ChatMessageToolCallBlock.svelte';
	import ChatMessageReasoningBlock from './ChatMessageReasoningBlock.svelte';
	import { Bot, Brain } from '@lucide/svelte';
	import {
		ChatMessageActionCardContinueRequest,
		ChatMessageActionCardPermissionRequest,
		ChatMessageStatistics,
		MarkdownContent
	} from '$lib/components/app';
	import { AgenticSectionType, ChatMessageStatsView, ToolPermissionDecision } from '$lib/enums';
	import { agenticStore, settingsStore } from '$lib/stores';
	import type {
		AgenticSection,
		ChatMessageAgenticTimings,
		ChatMessageAgenticTurnStats,
		DatabaseMessage
	} from '$lib/types';
	import { deriveAgenticSections } from '$lib/utils';

	interface Props {
		message: DatabaseMessage;
		toolMessages?: DatabaseMessage[];
		isStreaming?: boolean;
		isLastAssistantMessage?: boolean;
	}

	let {
		isLastAssistantMessage = false,
		isStreaming = false,
		message,
		toolMessages = []
	}: Props = $props();

	let expandedStates: Record<number, boolean> = $state({});

	const showThoughtInProgress = $derived(Boolean(settingsStore.config.showThoughtInProgress));
	const alwaysShowToolCallContent = $derived(
		Boolean(settingsStore.config.alwaysShowToolCallContent)
	);
	const showMessageStats = $derived(Boolean(settingsStore.config.showMessageStats));
	const showAgenticTurnStats = $derived(
		showMessageStats && Boolean(settingsStore.config.showAgenticTurnStats)
	);

	const hasReasoningError = $derived(
		isLastAssistantMessage ? !!agenticStore.getLastError(message.convId) : false
	);

	let permissionDismissed = $state(false);

	const pendingPermission = $derived(
		isStreaming && isLastAssistantMessage
			? agenticStore.getPendingPermissionRequest(message.convId)
			: null
	);

	let prevPendingRef: typeof pendingPermission = null;
	$effect(() => {
		if (pendingPermission !== prevPendingRef) {
			prevPendingRef = pendingPermission;

			if (pendingPermission) {
				permissionDismissed = false;
			}
		}
	});

	function handlePermission(decision: ToolPermissionDecision) {
		permissionDismissed = true;
		agenticStore.resolvePermission(message.convId, decision);
	}

	let continueDismissed = $state(false);

	const pendingContinue = $derived(
		isStreaming && isLastAssistantMessage
			? agenticStore.getPendingContinueRequest(message.convId)
			: false
	);

	let prevContinueRef = false;
	$effect(() => {
		if (pendingContinue !== prevContinueRef) {
			prevContinueRef = pendingContinue;

			if (pendingContinue) {
				continueDismissed = false;
			}
		}
	});

	function handleContinue(shouldContinue: boolean) {
		continueDismissed = true;
		agenticStore.resolveContinue(message.convId, shouldContinue);
	}

	const sections = $derived(deriveAgenticSections(message, toolMessages, [], isStreaming));

	const currentlyExecutingToolCallId = $derived(
		isStreaming ? agenticStore.getExecutingToolCallId(message.convId) : null
	);

	type TurnGroup = {
		sections: AgenticSection[];
		flatIndices: number[];
	};

	const turnGroups: TurnGroup[] = $derived.by(() => {
		const groups: TurnGroup[] = [];

		let currentTurn: AgenticSection[] = [];
		let currentIndices: number[] = [];
		let prevWasTool = false;

		for (let i = 0; i < sections.length; i++) {
			const section = sections[i];
			const isTool =
				section.type === AgenticSectionType.TOOL_CALL ||
				section.type === AgenticSectionType.TOOL_CALL_PENDING ||
				section.type === AgenticSectionType.TOOL_CALL_STREAMING;

			if (!isTool && prevWasTool && currentTurn.length > 0) {
				groups.push({ flatIndices: currentIndices, sections: currentTurn });
				currentTurn = [];
				currentIndices = [];
			}

			currentTurn.push(section);
			currentIndices.push(i);
			prevWasTool = isTool;
		}

		if (currentTurn.length > 0) {
			groups.push({ flatIndices: currentIndices, sections: currentTurn });
		}

		return groups;
	});

	function getDefaultExpanded(section: AgenticSection): boolean {
		if (
			section.type === AgenticSectionType.TOOL_CALL ||
			section.type === AgenticSectionType.TOOL_CALL_PENDING ||
			section.type === AgenticSectionType.TOOL_CALL_STREAMING
		) {
			return alwaysShowToolCallContent;
		}

		if (section.type === AgenticSectionType.REASONING_PENDING) {
			return showThoughtInProgress;
		}

		return false;
	}

	function isExpanded(index: number, section: AgenticSection): boolean {
		if (expandedStates[index] !== undefined) {
			return expandedStates[index];
		}

		return getDefaultExpanded(section);
	}

	function toggleExpanded(index: number, section: AgenticSection) {
		const currentState = isExpanded(index, section);

		expandedStates[index] = !currentState;
	}

	function buildTurnAgenticTimings(stats: ChatMessageAgenticTurnStats): ChatMessageAgenticTimings {
		return {
			llm: stats.llm,
			toolCalls: stats.toolCalls,
			toolCallsCount: stats.toolCalls.length,
			toolsMs: stats.toolsMs,
			turns: 1
		};
	}
</script>

{#snippet renderSection(section: AgenticSection, index: number)}
	{#if section.type === AgenticSectionType.TEXT}
		<div class="agentic-text">
			<MarkdownContent content={section.content} attachments={message?.extra} />
		</div>
	{:else if section.type === AgenticSectionType.REASONING || section.type === AgenticSectionType.REASONING_PENDING}
		<ChatMessageReasoningBlock
			{section}
			open={isExpanded(index, section)}
			{isStreaming}
			{hasReasoningError}
			attachments={message?.extra}
			onToggle={() => toggleExpanded(index, section)}
		/>
	{:else if section.type === AgenticSectionType.TOOL_CALL || section.type === AgenticSectionType.TOOL_CALL_PENDING || section.type === AgenticSectionType.TOOL_CALL_STREAMING}
		<ChatMessageToolCallBlock
			{section}
			open={isExpanded(index, section)}
			{isStreaming}
			isExecuting={section.toolCallId !== undefined &&
				section.toolCallId === currentlyExecutingToolCallId}
			attachments={message?.extra}
			onToggle={() => toggleExpanded(index, section)}
		/>
	{/if}
{/snippet}

<div class="agentic-content gap-2">
	{#if message.agencyStep}
		<div
			class="agency-step flex min-w-0 items-start gap-2.5 border-l-2 border-primary/60 bg-muted/25 px-3 py-2.5"
		>
			{#if message.agencyStep.role === 'supervisor'}
				<Brain class="mt-0.5 h-4 w-4 shrink-0 text-primary" />
			{:else}
				<Bot class="mt-0.5 h-4 w-4 shrink-0 text-primary" />
			{/if}
			<div class="min-w-0 flex-1">
				<div class="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
					<span class="text-[9px] font-semibold uppercase tracking-wider text-muted-foreground">
						{message.agencyStep.role === 'supervisor' ? 'Supervisor' : 'Agente'}
					</span>
					<span class="break-words text-xs font-semibold leading-snug text-foreground">
						{message.agencyStep.label}
					</span>
					{#if isStreaming && isLastAssistantMessage}
						<span
							class="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-primary"
							aria-label="En curso"
						></span>
					{/if}
				</div>
				{#if message.agencyStep.phases?.length}
					<div class="mt-1.5 flex flex-wrap gap-1">
						{#each message.agencyStep.phases as phase (phase)}
							<span
								class="rounded-sm bg-background/70 px-1.5 py-0.5 text-[9px] font-medium text-muted-foreground"
								>{phase}</span
							>
						{/each}
					</div>
				{/if}
				{#if message.agencyStep.subtask}
					<p class="mt-1.5 break-words text-xs leading-relaxed text-muted-foreground">
						{message.agencyStep.subtask}
					</p>
				{/if}
			</div>
		</div>
	{/if}

	{#if turnGroups.length > 1}
		{#each turnGroups as turn, turnIndex (turnIndex)}
			{@const turnStats = message?.timings?.agentic?.perTurn?.[turnIndex]}

			<div class="agentic-turn group/turn grid gap-2">
				{#each turn.sections as section, sIdx (turn.flatIndices[sIdx])}
					{@render renderSection(section, turn.flatIndices[sIdx])}
				{/each}

				{#if turnStats && showAgenticTurnStats}
					<div class="turn-stats transition-opacity duration-150 mt-1 mb-4">
						<ChatMessageStatistics
							promptTokens={turnStats.llm.prompt_n}
							promptMs={turnStats.llm.prompt_ms}
							predictedTokens={turnStats.llm.predicted_n}
							predictedMs={turnStats.llm.predicted_ms}
							agenticTimings={turnStats.toolCalls.length > 0
								? buildTurnAgenticTimings(turnStats)
								: undefined}
							initialView={ChatMessageStatsView.GENERATION}
							hideSummary
						/>
					</div>
				{/if}
			</div>
		{/each}
	{:else}
		{#each sections as section, index (index)}
			{@render renderSection(section, index)}
		{/each}
	{/if}

	{#if pendingPermission && !permissionDismissed}
		<ChatMessageActionCardPermissionRequest
			toolName={pendingPermission.toolName}
			serverLabel={pendingPermission.serverLabel}
			onDecision={handlePermission}
		/>
	{/if}

	{#if pendingContinue && !continueDismissed}
		<ChatMessageActionCardContinueRequest onDecision={handleContinue} />
	{/if}
</div>

<style>
	.agentic-content {
		display: flex;
		flex-direction: column;
		width: 100%;
		max-width: 48rem;
	}

	.agentic-content > :global(*),
	.agentic-turn > :global(*) {
		min-width: 0;
	}

	.agentic-text {
		width: 100%;
	}

	.turn-stats {
		border-top: 1px solid hsl(var(--muted) / 0.5);
	}
</style>
