/**
 * chatStore - Chat lifecycle, streaming and message operations
 *
 * Owns the active conversation's chat state: sending messages, streaming
 * responses, editing/regeneration flows and per-conversation processing
 * activity. Composes the stream manager, message flows, activity ledger and
 * processing snapshot; persists through conversationsStore.
 *
 * Uses ChatService for the API layer and conversationsStore for persistence.
 */

import { apiToken } from '$lib/api';
import { CWD_CLEARED_TEXT, SYSTEM_MESSAGE_PLACEHOLDER, TITLE_GENERATION } from '$lib/constants';
import {
	ErrorDialogType,
	MessageRole,
	MessageType,
	ReasoningEffort,
	StreamConnectionState
} from '$lib/enums';
import { ChatService } from '$lib/services/chat.service';
import { DatabaseService } from '$lib/services/database.service';
// direct imports between stores, not via the barrel, to avoid circular deps
import { agenticStore } from '$lib/stores/agentic/index.svelte';
import { agentStore } from '$lib/stores/agentStore.svelte';
import { chatActivityStore } from '$lib/stores/chat/activity.svelte';
import { type ChatFlowsHost, ChatMessageFlows } from '$lib/stores/chat/flows.svelte';
import { chatProcessingStore } from '$lib/stores/chat/processing.svelte';
import { type ChatStreamHost, ChatStreamManager } from '$lib/stores/chat/streams.svelte';
import { conversationsStore } from '$lib/stores/conversations/index.svelte';
import { mcpStore } from '$lib/stores/mcp/index.svelte';
import { modelsStore } from '$lib/stores/models/index.svelte';
import { serverStore } from '$lib/stores/server.svelte';
import { settingsStore } from '$lib/stores/settings/index.svelte';
import { toolsStore } from '$lib/stores/tools.svelte';
import type {
	AgencyStepMeta,
	ApiChatCompletionToolCall,
	ApiChatMessageData,
	ChatMessagePromptProgress,
	ChatMessageTimings,
	ChatStreamCallbacks,
	DatabaseMessage,
	DatabaseMessageExtra,
	ErrorDialogState
} from '$lib/types';
import {
	findMessageById,
	formatCwdMessage,
	getConversationModel,
	isAbortError,
	normalizeModelName
} from '$lib/utils';
import {
	AGENCY_RECONNECT_MAX_ATTEMPTS,
	type AgencySseFrame,
	streamAgencyJob
} from '$lib/utils/agency-stream';
import { SvelteMap } from 'svelte/reactivity';

/**
 * Cabeceras de autenticación opcionales del API de agentes.
 *
 * El token sale de `api.ts` (`llama-api-token`): el mismo que usan
 * `apiFetch`/`apiJson`, para que el modo agencia funcione igual en local y en
 * una instalación remota protegida por `@requires_auth`.
 */
function authHeaders(): Record<string, string> {
	const token = apiToken();

	return token ? { Authorization: `Bearer ${token}` } : {};
}

/**
 * Intervalo del volcado incremental del stream agencial al mensaje.
 *
 * El turno en curso se persiste cada pocos cientos de milisegundos en lugar de
 * solo al cerrarlo: si la pestaña se recarga (F5) o el stream se corta a mitad,
 * lo ya generado sigue en la conversación.
 */
const AGENCY_FLUSH_INTERVAL_MS = 700;

/** Estado vivo de una ejecución agencial que el chat está siguiendo. */
interface AgencyChatRun {
	/** Índice del último evento aplicado: base del resume tras reconectar. */
	appliedIndex: number;
	conversationId: string | null;
	endpoint: string;
	jobId: string;
	/** Mensaje asistente que abrió la ejecución (bloque del supervisor). */
	rootMessageId: string;
	/** Texto acumulado del job, usado para generar el título de la conversación. */
	text: string;
}

/** Turno en curso del flujo agencial: mensaje destino y buffers del stream. */
interface AgencyTurnState {
	content: string;
	messageId: string;
	reasoning: string;
	/** Texto acumulado de todo el job (título de la conversación). */
	text: string;
	timings?: ChatMessageTimings;
}

class ChatStore implements ChatStreamHost, ChatFlowsHost {
	chatReasoningStates = new SvelteMap<string, boolean>();
	chatStreamingStates = new SvelteMap<
		string,
		{ response: string; messageId: string; model?: string | null }
	>();
	currentResponse = $state('');
	errorDialogState = $state<ErrorDialogState | null>(null);
	// true while the active conversation has a local pipe (send, attach or resume-wait)
	isLoading = $derived(this.activity.isLocal(conversationsStore.activeConversation?.id ?? ''));
	// true while the active conversation streams reasoning content but no visible content yet
	isReasoning = $derived(
		this.chatReasoningStates.get(conversationsStore.activeConversation?.id ?? '') ?? false
	);
	pendingEditMessageId = $state<string | null>(null);
	// resumable stream connection state for the active conversation
	// streaming -> bytes flowing normally, resuming -> waiting on /v1/stream reconnect, lost -> unrecoverable
	streamConnectionState = $state<StreamConnectionState>(StreamConnectionState.STREAMING);
	private abortControllers = new SvelteMap<string, AbortController>();
	private addFilesHandler: ((files: File[]) => void) | null = $state(null);
	// message flows: edit, regenerate, continue, delete
	private flows = new ChatMessageFlows(this);
	private isEditModeActive = $state(false);
	private pendingDraftFiles = $state<ChatUploadedFile[]>([]);
	private pendingDraftMessage = $state<string>('');
	/** Reactive: queued pending messages for non-agentic streaming */
	private pendingMessages = new SvelteMap<
		string,
		{ content: string; extras?: DatabaseMessageExtra[] }
	>();
	private preEncodeAbortController: AbortController | null = null;

	// server-side stream sessions: discovery, attach/replay, resume retry, remote sync
	private streams = new ChatStreamManager(this);

	/**
	 * Ejecuciones agenciales (modo AGENCY) que el chat está siguiendo, por
	 * conversación. Es la fuente que permite reanudar un job tras un F5 y la que
	 * evita que el chat abra un segundo stream para la misma ejecución.
	 */
	private agencyRuns = new SvelteMap<string, AgencyChatRun>();

	/** Conv activity (local pipe / remote session), composed here. */
	get activity() {
		return chatActivityStore;
	}

	/** Processing state, composed here so consumers have a single chat scope. */
	get processing() {
		return chatProcessingStore;
	}

	/**
	 * Abort the current agentic flow signal without clearing loading state.
	 * Used by "Send immediately" to force the agentic loop to exit so that
	 * the pending steering message can be re-sent.
	 *
	 * Any tool calls captured mid-stream are dropped before the abort so the
	 * pending message (or a manual follow-up) does not re-send a half-received
	 * tool call with invalid JSON arguments to the server. Mirrors what the
	 * Stop button already does through stopGenerationForChat.
	 */
	async abortCurrentFlow(convId: string): Promise<void> {
		await this.savePartialResponseIfNeeded(convId);
		const c = this.abortControllers.get(convId);

		if (c) {
			c.abort();
			this.abortControllers.delete(convId);
		}
	}

	async addMessage(
		role: MessageRole,
		content: string,
		type: MessageType = MessageType.TEXT,
		parent: string = '-1',
		extras?: DatabaseMessageExtra[],
		isSynthetic?: boolean
	): Promise<DatabaseMessage> {
		const activeConv = conversationsStore.activeConversation;

		if (!activeConv) throw new Error('No active conversation');

		let parentId: string | null = null;

		if (parent === '-1') {
			const am = conversationsStore.activeMessages;

			if (am.length > 0) parentId = am[am.length - 1].id;
			else {
				const all = await conversationsStore.getConversationMessages(activeConv.id);
				const r = all.find((m) => m.parent === null && m.type === 'root');

				parentId = r ? r.id : await DatabaseService.createRootMessage(activeConv.id);
			}
		} else parentId = parent;

		const message = await DatabaseService.createMessageBranch(
			{
				children: [],
				content,
				convId: activeConv.id,
				extra: extras,
				isSynthetic,
				role,
				timestamp: Date.now(),
				toolCalls: '',
				type
			},
			parentId
		);

		conversationsStore.addMessageToActive(message);
		await conversationsStore.updateCurrentNode(message.id);
		conversationsStore.updateConversationTimestamp();

		return message;
	}
	async addSystemPrompt(): Promise<void> {
		let activeConv = conversationsStore.activeConversation;

		if (!activeConv) {
			await conversationsStore.createConversation();
			activeConv = conversationsStore.activeConversation;
		}

		if (!activeConv) return;

		try {
			const allMessages = await conversationsStore.getConversationMessages(activeConv.id);
			const rootMessage = allMessages.find((m) => m.type === 'root' && m.parent === null);
			const rootId = rootMessage
				? rootMessage.id
				: await DatabaseService.createRootMessage(activeConv.id);
			const existingSystemMessage = allMessages.find(
				(m) => m.role === MessageRole.SYSTEM && m.parent === rootId
			);

			if (existingSystemMessage) {
				this.pendingEditMessageId = existingSystemMessage.id;

				if (!conversationsStore.activeMessages.some((m) => m.id === existingSystemMessage.id))
					conversationsStore.activeMessages.unshift(existingSystemMessage);

				return;
			}

			const am = conversationsStore.activeMessages;
			const firstActiveMessage = am.find((m) => m.parent === rootId);
			const systemMessage = await DatabaseService.createSystemMessage(
				activeConv.id,
				SYSTEM_MESSAGE_PLACEHOLDER,
				rootId
			);

			if (firstActiveMessage) {
				await DatabaseService.updateMessage(firstActiveMessage.id, {
					parent: systemMessage.id
				});
				await DatabaseService.updateMessage(systemMessage.id, {
					children: [firstActiveMessage.id]
				});
				const updatedRootChildren = rootMessage
					? rootMessage.children.filter((id: string) => id !== firstActiveMessage.id)
					: [];

				await DatabaseService.updateMessage(rootId, {
					children: [
						...updatedRootChildren.filter((id: string) => id !== systemMessage.id),
						systemMessage.id
					]
				});
				const firstMsgIndex = conversationsStore.findMessageIndex(firstActiveMessage.id);

				if (firstMsgIndex !== -1)
					conversationsStore.updateMessageAtIndex(firstMsgIndex, {
						parent: systemMessage.id
					});
			}

			conversationsStore.activeMessages.unshift(systemMessage);
			this.pendingEditMessageId = systemMessage.id;
			conversationsStore.updateConversationTimestamp();
		} catch (error) {
			console.error('Failed to add system prompt:', error);
		}
	}
	/** Persist completed turns produced by the isolated voice-call session. */
	async appendCallSession(
		turns: Array<{ user: string; assistant: string; reasoning: string }>
	): Promise<void> {
		if (!turns.length) return;

		if (!conversationsStore.activeConversation) await conversationsStore.createConversation();

		if (!conversationsStore.activeConversation) return;

		for (const turn of turns) {
			if (!turn.user.trim()) continue;

			const userMessage = await this.addMessage(
				MessageRole.USER,
				turn.user.trim(),
				MessageType.TEXT
			);
			const assistantMessage = await this.createAssistantMessage(userMessage.id);
			const assistantContent = turn.assistant.trim();
			const reasoningContent = turn.reasoning.trim() || undefined;
			const updates = { content: assistantContent, reasoningContent };

			await DatabaseService.updateMessage(assistantMessage.id, updates);
			conversationsStore.addMessageToActive({ ...assistantMessage, ...updates });
			await conversationsStore.updateCurrentNode(assistantMessage.id);
		}
	}

	cancelPreEncode(): void {
		if (this.preEncodeAbortController) {
			this.preEncodeAbortController.abort();
			this.preEncodeAbortController = null;
		}
	}
	/**
	 * Resets the loading, streaming and processing state for a conversation
	 * after a generation ends or errors. Shared by the flows' exit paths.
	 */
	cleanupStreaming(convId: string): void {
		this.setChatLoading(convId, false);
		this.clearChatStreaming(convId);
		this.processing.setState(convId, null);
	}
	clearChatStreaming(convId: string, messageId?: string): void {
		// session aware: a stale generation must not wipe a newer one's streaming state on the
		// same conversation, that would drop the frozen stop identity and stop the wrong session
		if (messageId !== undefined) {
			const cur = this.chatStreamingStates.get(convId);

			if (cur && cur.messageId !== messageId) return;
		}

		this.chatStreamingStates.delete(convId);

		if (convId === conversationsStore.activeConversation?.id) this.currentResponse = '';
	}

	clearEditMode(): void {
		this.isEditModeActive = false;
		this.addFilesHandler = null;
	}

	clearPendingEditMessageId(): void {
		this.pendingEditMessageId = null;
	}

	clearPendingMessage(convId: string): void {
		this.pendingMessages.delete(convId);
	}

	/** Reset per-view state when (re)mounting the empty chat screen. */
	clearUIState(): void {
		this.currentResponse = '';
	}

	consumePendingDraft(): { message: string; files: ChatUploadedFile[] } | null {
		if (!this.pendingDraftMessage && this.pendingDraftFiles.length === 0) return null;

		const d = { files: [...this.pendingDraftFiles], message: this.pendingDraftMessage };

		this.pendingDraftMessage = '';
		this.pendingDraftFiles = [];

		return d;
	}

	consumePendingMessage(
		convId: string
	): { content: string; extras?: DatabaseMessageExtra[] } | null {
		const msg = this.pendingMessages.get(convId);

		if (!msg) return null;

		this.pendingMessages.delete(convId);

		return msg;
	}

	async continueAssistantMessage(messageId: string): Promise<void> {
		return this.flows.continueAssistantMessage(messageId);
	}

	async createAssistantMessage(parentId?: string): Promise<DatabaseMessage> {
		const activeConv = conversationsStore.activeConversation;

		if (!activeConv) throw new Error('No active conversation');

		return await DatabaseService.createMessageBranch(
			{
				children: [],
				content: '',
				convId: activeConv.id,
				model: null,
				role: MessageRole.ASSISTANT,
				timestamp: Date.now(),
				toolCalls: '',
				type: MessageType.TEXT
			},
			parentId || null
		);
	}

	async deleteMessage(messageId: string): Promise<void> {
		return this.flows.deleteMessage(messageId);
	}

	/**
	 * Server-side stream sessions (discovery, attach/replay, resume retry,
	 * remote-running snapshot) live in ChatStreamManager.
	 */
	async discoverActiveStream(convId: string): Promise<void> {
		return this.streams.discoverActiveStream(convId);
	}

	dismissErrorDialog(): void {
		this.errorDialogState = null;
	}

	async editAssistantMessage(
		messageId: string,
		newContent: string,
		shouldBranch: boolean
	): Promise<void> {
		return this.flows.editAssistantMessage(messageId, newContent, shouldBranch);
	}

	async editMessageWithBranching(
		messageId: string,
		newContent: string,
		newExtras?: DatabaseMessageExtra[]
	): Promise<void> {
		return this.flows.editMessageWithBranching(messageId, newContent, newExtras);
	}

	async editUserMessagePreserveResponses(
		messageId: string,
		newContent: string,
		newExtras?: DatabaseMessageExtra[]
	): Promise<void> {
		return this.flows.editUserMessagePreserveResponses(messageId, newContent, newExtras);
	}

	async ensureResearchSystemPrompt(prompt: string): Promise<void> {
		const conversation = conversationsStore.activeConversation;
		const trimmedPrompt = prompt.trim();

		if (!conversation || !trimmedPrompt) return;

		const allMessages = await conversationsStore.getConversationMessages(conversation.id);
		const existing = allMessages.find(
			(message) =>
				message.role === MessageRole.SYSTEM && message.content.startsWith('[RAG_RESEARCH_MODE]')
		);

		if (existing) {
			if (existing.content !== trimmedPrompt) {
				await DatabaseService.updateMessage(existing.id, { content: trimmedPrompt });
				const index = conversationsStore.findMessageIndex(existing.id);

				if (index !== -1)
					conversationsStore.updateMessageAtIndex(index, { content: trimmedPrompt });
			}

			return;
		}

		const root = allMessages.find((message) => message.type === 'root' && message.parent === null);
		const rootId = root?.id ?? (await DatabaseService.createRootMessage(conversation.id));
		const firstRootChild = allMessages.find(
			(message) => message.parent === rootId && message.role !== MessageRole.SYSTEM
		);
		const systemMessage = await DatabaseService.createSystemMessage(
			conversation.id,
			trimmedPrompt,
			rootId
		);

		if (firstRootChild) {
			await DatabaseService.updateMessage(firstRootChild.id, { parent: systemMessage.id });
			await DatabaseService.updateMessage(systemMessage.id, { children: [firstRootChild.id] });

			if (root) {
				await DatabaseService.updateMessage(root.id, {
					children: root.children.map((id: string) =>
						id === firstRootChild.id ? systemMessage.id : id
					)
				});
			}
		}

		conversationsStore.activeMessages.unshift(systemMessage);
	}

	getAddFilesHandler(): ((files: File[]) => void) | null {
		return this.addFilesHandler;
	}

	/** Convs with any activity (local pipe or remote session), sidebar spinners. */
	getAllLoadingChats(): string[] {
		return this.activity.loadingConvs;
	}

	getApiOptions(): Record<string, unknown> {
		const currentConfig = settingsStore.config;
		const hasValue = (value: unknown): boolean =>
			value !== undefined && value !== null && value !== '';
		const apiOptions: Record<string, unknown> = { stream: true, timings_per_token: true };

		if (serverStore.isRouterMode) {
			const modelName = modelsStore.selectedModelName;

			if (modelName) apiOptions.model = modelName;
		}

		if (currentConfig.systemMessage) apiOptions.systemMessage = currentConfig.systemMessage;

		// Modo agente (AGENCY): inyecta agentes seleccionados y skills activos en el
		// contexto del chat nativo, tanto en conversaciones nuevas como existentes.
		if (agentStore.config.agentMode) {
			const block = agentStore.buildContextBlock();

			if (block) {
				const base = currentConfig.systemMessage?.toString() ?? '';

				apiOptions.systemMessage = base.trim() + (base.trim() ? '\n\n' : '') + block;
			}
		}

		if (currentConfig.disableReasoningParsing) apiOptions.disableReasoningParsing = true;

		if (currentConfig.excludeReasoningFromContext) apiOptions.excludeReasoningFromContext = true;

		// an explicit reasoning choice overrides the server default, DEFAULT sends nothing
		const effort = conversationsStore.preferences.getReasoningEffort();

		if (effort !== ReasoningEffort.DEFAULT) {
			apiOptions.enableThinking = effort !== ReasoningEffort.OFF;

			if (effort !== ReasoningEffort.OFF) apiOptions.reasoningEffort = effort;
		}

		if (hasValue(currentConfig.temperature))
			apiOptions.temperature = Number(currentConfig.temperature);

		if (hasValue(currentConfig.max_tokens))
			apiOptions.max_tokens = Number(currentConfig.max_tokens);

		if (hasValue(currentConfig.dynatemp_range))
			apiOptions.dynatemp_range = Number(currentConfig.dynatemp_range);

		if (hasValue(currentConfig.dynatemp_exponent))
			apiOptions.dynatemp_exponent = Number(currentConfig.dynatemp_exponent);

		if (hasValue(currentConfig.top_k)) apiOptions.top_k = Number(currentConfig.top_k);

		if (hasValue(currentConfig.top_p)) apiOptions.top_p = Number(currentConfig.top_p);

		if (hasValue(currentConfig.min_p)) apiOptions.min_p = Number(currentConfig.min_p);

		if (hasValue(currentConfig.xtc_probability))
			apiOptions.xtc_probability = Number(currentConfig.xtc_probability);

		if (hasValue(currentConfig.xtc_threshold))
			apiOptions.xtc_threshold = Number(currentConfig.xtc_threshold);

		if (hasValue(currentConfig.typ_p)) apiOptions.typ_p = Number(currentConfig.typ_p);

		if (hasValue(currentConfig.repeat_last_n))
			apiOptions.repeat_last_n = Number(currentConfig.repeat_last_n);

		if (hasValue(currentConfig.repeat_penalty))
			apiOptions.repeat_penalty = Number(currentConfig.repeat_penalty);

		if (hasValue(currentConfig.presence_penalty))
			apiOptions.presence_penalty = Number(currentConfig.presence_penalty);

		if (hasValue(currentConfig.frequency_penalty))
			apiOptions.frequency_penalty = Number(currentConfig.frequency_penalty);

		if (hasValue(currentConfig.dry_multiplier))
			apiOptions.dry_multiplier = Number(currentConfig.dry_multiplier);

		if (hasValue(currentConfig.dry_base)) apiOptions.dry_base = Number(currentConfig.dry_base);

		if (hasValue(currentConfig.dry_allowed_length))
			apiOptions.dry_allowed_length = Number(currentConfig.dry_allowed_length);

		if (hasValue(currentConfig.dry_penalty_last_n))
			apiOptions.dry_penalty_last_n = Number(currentConfig.dry_penalty_last_n);

		if (currentConfig.samplers) apiOptions.samplers = currentConfig.samplers;

		if (hasValue(currentConfig.backend_sampling))
			apiOptions.backend_sampling = currentConfig.backend_sampling;

		if (currentConfig.customJson) apiOptions.custom = currentConfig.customJson;

		return apiOptions;
	}

	getChatStreaming(convId: string): { response: string; messageId: string } | undefined {
		return this.getChatStreamingState(convId);
	}

	async getDeletionInfo(messageId: string): Promise<{
		totalCount: number;
		userMessages: number;
		assistantMessages: number;
		messageTypes: string[];
	}> {
		return this.flows.getDeletionInfo(messageId);
	}

	getOrCreateAbortController(convId: string): AbortController {
		let c = this.abortControllers.get(convId);

		if (!c || c.signal.aborted) {
			c = new AbortController();
			this.abortControllers.set(convId, c);
		}

		return c;
	}

	getPendingMessageContent(convId: string): string | null {
		return this.pendingMessages.get(convId)?.content ?? null;
	}

	getPendingMessageExtras(convId: string): DatabaseMessageExtra[] | undefined {
		return this.pendingMessages.get(convId)?.extras;
	}

	getResumeModel(convId: string): string | null {
		return this.streams.getResumeModel(convId);
	}

	hasPendingDraft(): boolean {
		return Boolean(this.pendingDraftMessage) || this.pendingDraftFiles.length > 0;
	}

	hasPendingMessage(convId: string): boolean {
		return this.pendingMessages.has(convId);
	}

	injectPendingMessage(convId: string, content: string, extras?: DatabaseMessageExtra[]): void {
		this.pendingMessages.set(convId, { content, extras });
	}

	isChatLoading(convId: string): boolean {
		return this.activity.isLocal(convId);
	}

	isChatLoadingInternal(convId: string): boolean {
		return this.activity.isLocal(convId) || this.chatStreamingStates.has(convId);
	}

	isEditing(): boolean {
		return this.isEditModeActive;
	}

	/** True while the active conversation has a live streaming pipe. */
	isStreaming(): boolean {
		return this.chatStreamingStates.has(conversationsStore.activeConversation?.id ?? '');
	}

	/**
	 * Record a working-directory change into chat history as a synthetic
	 * user message, so the model sees it on its next turn (the client
	 * sends the cwd itself via the x-tool-cwd header on tool calls).
	 * A plain user message is used because some chat templates reject
	 * tool messages without a preceding tool call.
	 */
	async recordCwdChange(cwd: string | null): Promise<void> {
		const content = cwd
			? formatCwdMessage(cwd, await toolsStore.resolveServerHome())
			: CWD_CLEARED_TEXT;
		// Reuse the trailing cwd row when it is already the last message, so
		// repeated picks update it in place instead of stacking another row.
		const last = conversationsStore.activeMessages[conversationsStore.activeMessages.length - 1];

		if (last && last.role === MessageRole.USER && last.isSynthetic === true) {
			await DatabaseService.updateMessage(last.id, { content, isSynthetic: true });
			conversationsStore.updateMessageAtIndex(conversationsStore.activeMessages.length - 1, {
				content,
				isSynthetic: true
			});

			return;
		}

		await this.addMessage(MessageRole.USER, content, MessageType.TEXT, '-1', undefined, true);
	}

	async regenerateMessage(messageId: string): Promise<void> {
		return this.flows.regenerateMessage(messageId);
	}

	async regenerateMessageWithBranching(messageId: string, modelOverride?: string): Promise<void> {
		return this.flows.regenerateMessageWithBranching(messageId, modelOverride);
	}

	async removeSystemPromptPlaceholder(messageId: string): Promise<boolean> {
		const activeConv = conversationsStore.activeConversation;

		if (!activeConv) return false;

		try {
			const allMessages = await conversationsStore.getConversationMessages(activeConv.id);
			const systemMessage = findMessageById(allMessages, messageId);

			if (!systemMessage || systemMessage.role !== MessageRole.SYSTEM) return false;

			const rootMessage = allMessages.find((m) => m.type === 'root' && m.parent === null);

			if (!rootMessage) return false;

			if (allMessages.length === 2 && systemMessage.children.length === 0) {
				await conversationsStore.deleteConversation(activeConv.id);

				return true;
			}

			for (const childId of systemMessage.children) {
				await DatabaseService.updateMessage(childId, { parent: rootMessage.id });
				const childIndex = conversationsStore.findMessageIndex(childId);

				if (childIndex !== -1)
					conversationsStore.updateMessageAtIndex(childIndex, { parent: rootMessage.id });
			}
			await DatabaseService.updateMessage(rootMessage.id, {
				children: [
					...rootMessage.children.filter((id: string) => id !== messageId),
					...systemMessage.children
				]
			});
			await DatabaseService.deleteMessage(messageId);
			const systemIndex = conversationsStore.findMessageIndex(messageId);

			if (systemIndex !== -1) conversationsStore.activeMessages.splice(systemIndex, 1);

			conversationsStore.updateConversationTimestamp();

			return false;
		} catch (error) {
			console.error('Failed to remove system prompt placeholder:', error);

			return false;
		}
	}

	savePendingDraft(message: string, files: ChatUploadedFile[]): void {
		this.pendingDraftMessage = message;
		this.pendingDraftFiles = [...files];
	}

	async sendMessage(
		content: string,
		extras?: DatabaseMessageExtra[],
		systemPromptOverride?: string
	): Promise<void> {
		if (!content.trim() && (!extras || extras.length === 0)) return;

		const activeConv = conversationsStore.activeConversation;

		// If agentic loop is running, inject as a steering message instead of starting a new flow
		if (activeConv && agenticStore.isRunning(activeConv.id)) {
			agenticStore.injectSteeringMessage(activeConv.id, content, extras);

			return;
		}

		// If non-agentic streaming is active, queue as a pending message to send after completion
		if (activeConv && this.isChatLoadingInternal(activeConv.id)) {
			this.injectPendingMessage(activeConv.id, content, extras);

			return;
		}

		// Cancel any in-flight pre-encode request
		this.cancelPreEncode();

		// Consume MCP resource attachments - converts them to extras and clears the live store
		const resourceExtras = mcpStore.consumeResourceAttachmentsAsExtras();
		const allExtras = resourceExtras.length > 0 ? [...(extras || []), ...resourceExtras] : extras;

		let isNewConversation = false;

		if (!activeConv) {
			await conversationsStore.createConversation();
			isNewConversation = true;
		}

		const currentConv = conversationsStore.activeConversation;

		if (!currentConv) return;

		this.showErrorDialog(null);
		this.setChatLoading(currentConv.id, true);
		this.clearChatStreaming(currentConv.id);
		try {
			let parentIdForUserMessage: string | undefined;

			if (isNewConversation) {
				const rootId = await DatabaseService.createRootMessage(currentConv.id);
				const currentConfig = settingsStore.config;
				const systemPrompt =
					systemPromptOverride?.trim() || currentConfig.systemMessage?.toString().trim();

				let sysOrRootId = rootId;

				if (systemPrompt) {
					const systemMessage = await DatabaseService.createSystemMessage(
						currentConv.id,
						systemPrompt,
						rootId
					);

					conversationsStore.addMessageToActive(systemMessage);
					sysOrRootId = systemMessage.id;
				}

				// Reflect a working directory picked on the new-chat screen into
				// chat history before the first user message, so the model sees
				// it on its first turn. createConversation() has already threaded
				// the pending pick onto the conversation.
				if (currentConv.cwd) {
					const cwdMessage = await this.addMessage(
						MessageRole.USER,
						formatCwdMessage(currentConv.cwd, await toolsStore.resolveServerHome()),
						MessageType.TEXT,
						sysOrRootId,
						undefined,
						true
					);

					parentIdForUserMessage = cwdMessage.id;
				} else {
					parentIdForUserMessage = sysOrRootId;
				}
			}

			const userMessage = await this.addMessage(
				MessageRole.USER,
				content,
				MessageType.TEXT,
				parentIdForUserMessage ?? '-1',
				allExtras
			);

			if (isNewConversation && content)
				await conversationsStore.applyTitleFromContent(currentConv.id, content);

			const assistantMessage = await this.createAssistantMessage(userMessage.id);

			conversationsStore.addMessageToActive(assistantMessage);
			await this.streamChatCompletion(
				conversationsStore.activeMessages.slice(0, -1),
				assistantMessage,
				undefined,
				undefined,
				undefined,
				settingsStore.config.titleGenerationUseLLM && isNewConversation ? content : undefined
			);
		} catch (error) {
			if (isAbortError(error)) {
				this.setChatLoading(currentConv.id, false);

				return;
			}

			console.error('Failed to send message:', error);
			this.setChatLoading(currentConv.id, false);
			const dialogType =
				error instanceof Error && error.name === 'TimeoutError'
					? ErrorDialogType.TIMEOUT
					: ErrorDialogType.SERVER;
			const contextInfo = (
				error as Error & { contextInfo?: { n_prompt_tokens: number; n_ctx: number } }
			).contextInfo;

			this.showErrorDialog({
				contextInfo,
				message: error instanceof Error ? error.message : 'Unknown error',
				type: dialogType
			});
		}
	}

	setChatLoading(convId: string, loading: boolean): void {
		if (loading) {
			this.activity.markLocal(convId);
		} else {
			this.activity.localEnded(convId);
			this.setChatReasoning(convId, false);
		}
	}

	setChatReasoning(convId: string, reasoning: boolean): void {
		if (reasoning) this.chatReasoningStates.set(convId, true);
		else this.chatReasoningStates.delete(convId);
	}

	setChatStreaming(
		convId: string,
		response: string,
		messageId: string,
		model?: string | null
	): void {
		this.chatStreamingStates.set(convId, {
			messageId,
			model: model ?? this.chatStreamingStates.get(convId)?.model,
			response
		});

		if (convId === conversationsStore.activeConversation?.id) this.currentResponse = response;
	}

	setEditModeActive(handler: (files: File[]) => void): void {
		this.isEditModeActive = true;
		this.addFilesHandler = handler;
	}

	showErrorDialog(state: ErrorDialogState | null): void {
		this.errorDialogState = state;
	}

	async stopGeneration(): Promise<void> {
		const activeConv = conversationsStore.activeConversation;

		if (!activeConv) return;

		await this.stopGenerationForChat(activeConv.id);
	}

	async stopGenerationForChat(convId: string): Promise<void> {
		await this.savePartialResponseIfNeeded(convId);
		// tell the server to stop the generation, not just drop the HTTP socket. without this the
		// detached drain keeps producing tokens until eos or max_tokens. use the frozen identity
		// captured when the session started, not the live dropdown
		const streamStateForStop = this.chatStreamingStates.get(convId);
		const modelForStop = streamStateForStop?.model ?? ChatService.getStreamState(convId)?.model;

		void ChatService.cancelServerStream(convId, modelForStop);
		// an explicit stop leaves nothing to resume and kills a pending resume retry
		ChatService.clearStreamState(convId);
		this.streams.cancelResumeRetry(convId);
		this.abortRequest(convId);
		this.setChatLoading(convId, false);
		this.clearChatStreaming(convId);
		this.processing.setState(convId, null);
		this.clearPendingMessage(convId);
	}

	async streamChatCompletion(
		allMessages: DatabaseMessage[],
		assistantMessage: DatabaseMessage,
		onComplete?: (content: string) => Promise<void>,
		onError?: (error: Error) => void,
		modelOverride?: string | null,
		firstUserMessageContent?: string
	): Promise<void> {
		// the ::model suffix in the stream identity is only for router mode, where it routes to the
		// owning child. in single-model mode the identity stays the bare conv id so that attach, stop
		// and reattach all agree, regardless of fresh send vs regenerate passing a resolved model
		let effectiveModel: string | null | undefined = undefined;

		if (serverStore.isRouterMode) {
			const conversationModel = getConversationModel(allMessages);

			effectiveModel = modelOverride || modelsStore.selectedModelName || conversationModel;
		}

		if (serverStore.isRouterMode && effectiveModel) {
			if (!modelsStore.props.getModelProps(effectiveModel))
				await modelsStore.props.fetchModelProps(effectiveModel);
		}

		// Mutable state for the current message being streamed
		let currentMessageId = assistantMessage.id;
		let streamedContent = '';
		let streamedReasoningContent = '';
		let resolvedModel: string | null = null;
		let modelPersisted = false;

		const convId = assistantMessage.convId;

		// Tracks the last message created in this flow. Used as the parent for the next
		// turn's assistant message so createAssistantMessage does not have to read
		// conversationsStore.activeMessages, which may belong to a different conversation
		// after the user navigates while the loop is still running.
		let lastCreatedInFlow = currentMessageId;

		// freeze the POST identity from t0 so a stop cancels with the exact session key,
		// never a stale or empty model resolved later
		this.setChatStreaming(convId, streamedContent, currentMessageId, effectiveModel);

		const recordModel = (modelName: string | null | undefined, persistImmediately = true): void => {
			if (!modelName) return;

			const n = normalizeModelName(modelName);

			if (!n || n === resolvedModel) return;

			resolvedModel = n;
			const idx = conversationsStore.findMessageIndex(currentMessageId);

			conversationsStore.updateMessageAtIndex(idx, { model: n });

			if (persistImmediately && !modelPersisted) {
				modelPersisted = true;
				DatabaseService.updateMessage(currentMessageId, { model: n }).catch(() => {
					modelPersisted = false;
					resolvedModel = null;
				});
			}
		};

		let completionIdRecorded = false;

		const recordCompletionId = (id: string): void => {
			if (!id || completionIdRecorded) return;

			completionIdRecorded = true;
			const idx = conversationsStore.findMessageIndex(currentMessageId);

			conversationsStore.updateMessageAtIndex(idx, { completionId: id });
			DatabaseService.updateMessage(currentMessageId, { completionId: id }).catch(() => {
				completionIdRecorded = false;
			});
		};
		const updateStreamingUI = () => {
			this.setChatStreaming(convId, streamedContent, currentMessageId, effectiveModel);
			const idx = conversationsStore.findMessageIndex(currentMessageId);

			conversationsStore.updateMessageAtIndex(idx, { content: streamedContent });
		};
		const cleanupStreamingState = () => {
			this.setChatLoading(convId, false);
			this.clearChatStreaming(convId, currentMessageId);
			this.processing.setState(convId, null);
		};

		this.processing.setActiveConversation(convId);
		const abortController = this.getOrCreateAbortController(convId);
		const streamCallbacks: ChatStreamCallbacks = {
			createAssistantMessage: async (agencyStep?: AgencyStepMeta) => {
				// Reset streaming state for new message
				streamedContent = '';
				streamedReasoningContent = '';

				const msg = await DatabaseService.createMessageBranch(
					{
						children: [],
						content: '',
						convId,
						// `agencyStep` marca el bloque agencial (supervisor o
						// especialista) al que pertenece el turno: el renderer
						// lo usa para rotular y separar cada paso en el chat.
						...(agencyStep ? { agencyStep } : {}),
						model: resolvedModel,
						role: MessageRole.ASSISTANT,
						timestamp: Date.now(),
						toolCalls: '',
						type: MessageType.TEXT
					},
					lastCreatedInFlow
				);

				if (conversationsStore.activeConversation?.id === convId) {
					conversationsStore.addMessageToActive(msg);
				}

				currentMessageId = msg.id;
				lastCreatedInFlow = msg.id;

				return msg;
			},
			createToolResultMessage: async (
				toolCallId: string,
				content: string,
				extras?: DatabaseMessageExtra[],
				toolCwd?: string
			) => {
				const msg = await DatabaseService.createMessageBranch(
					{
						children: [],
						content,
						convId,
						extra: extras,
						role: MessageRole.TOOL,
						timestamp: Date.now(),
						toolCallId,
						toolCalls: '',
						toolCwd,
						type: MessageType.TEXT
					},
					currentMessageId
				);

				// mirror into the active store and move the node pointer only when this
				// conversation is displayed; otherwise persist the node move straight to
				// the db for the owning conv so a foreign conv's currNode stays untouched
				if (conversationsStore.activeConversation?.id === convId) {
					conversationsStore.addMessageToActive(msg);
					await conversationsStore.updateCurrentNode(msg.id);
				} else {
					await DatabaseService.updateCurrentNode(convId, msg.id);
				}

				lastCreatedInFlow = msg.id;

				return msg;
			},
			onAssistantTurnComplete: async (
				content: string,
				reasoningContent: string | undefined,
				timings: ChatMessageTimings | undefined,
				toolCalls: import('$lib/types/api').ApiChatCompletionToolCall[] | undefined,
				agencyStep?: AgencyStepMeta
			) => {
				const updateData: Record<string, unknown> = {
					content,
					reasoningContent: reasoningContent || undefined,
					timings,
					toolCalls: toolCalls ? JSON.stringify(toolCalls) : ''
				};

				if (resolvedModel && !modelPersisted) updateData.model = resolvedModel;

				// Sella el bloque agencial del turno (aplica al mensaje inicial
				// creado por sendMessage y a los turnos siguientes del mismo paso).
				if (agencyStep) updateData.agencyStep = agencyStep;

				await DatabaseService.updateMessage(currentMessageId, updateData);
				const idx = conversationsStore.findMessageIndex(currentMessageId);
				const uiUpdate: Partial<DatabaseMessage> = {
					content,
					reasoningContent: reasoningContent || undefined,
					toolCalls: toolCalls ? JSON.stringify(toolCalls) : ''
				};

				if (timings) uiUpdate.timings = timings;

				if (resolvedModel) uiUpdate.model = resolvedModel;

				if (agencyStep) uiUpdate.agencyStep = agencyStep;

				// touch the active ui array and node pointer only when this conversation
				// is displayed; otherwise persist the node move straight to the db so a
				// foreign conv's currNode stays untouched
				if (conversationsStore.activeConversation?.id === convId) {
					conversationsStore.updateMessageAtIndex(idx, uiUpdate);
					await conversationsStore.updateCurrentNode(currentMessageId);
				} else {
					await DatabaseService.updateCurrentNode(convId, currentMessageId);
				}
			},
			onAttachments: (messageId: string, extras: DatabaseMessageExtra[]) => {
				if (!extras.length) return;

				const idx = conversationsStore.findMessageIndex(messageId);

				if (idx === -1) return;

				const msg = conversationsStore.activeMessages[idx];
				const updatedExtras = [...(msg.extra || []), ...extras];

				conversationsStore.updateMessageAtIndex(idx, { extra: updatedExtras });
				DatabaseService.updateMessage(messageId, { extra: updatedExtras }).catch(console.error);
			},
			onChunk: (chunk: string) => {
				streamedContent += chunk;
				updateStreamingUI();
				this.setChatReasoning(convId, false);
			},
			onCompletionId: (id: string) => recordCompletionId(id),
			onError: async (error: Error) => {
				if (isAbortError(error)) {
					cleanupStreamingState();
					// If aborted with a pending message (e.g. "Send immediately"), re-send it
					const pending = this.consumePendingMessage(convId);

					if (pending) {
						this.sendMessage(pending.content, pending.extras);
					}

					return;
				}

				console.error('Streaming error:', error);
				// keep whatever was streamed so far, the message stays in memory and in DB
				await this.savePartialResponseIfNeeded(convId);
				cleanupStreamingState();
				this.clearPendingMessage(convId);

				const contextInfo = (
					error as Error & { contextInfo?: { n_prompt_tokens: number; n_ctx: number } }
				).contextInfo;

				this.showErrorDialog({
					contextInfo,
					message: error.message,
					type: error.name === 'TimeoutError' ? ErrorDialogType.TIMEOUT : ErrorDialogType.SERVER
				});

				if (onError) onError(error);
			},
			onFlowComplete: (finalTimings?: ChatMessageTimings) => {
				if (finalTimings) {
					const idx = conversationsStore.findMessageIndex(assistantMessage.id);

					conversationsStore.updateMessageAtIndex(idx, { timings: finalTimings });
					DatabaseService.updateMessage(assistantMessage.id, {
						timings: finalTimings
					}).catch(console.error);
				}

				cleanupStreamingState();

				if (onComplete) onComplete(streamedContent);

				if (serverStore.isRouterMode) modelsStore.fetchRouterModels().catch(console.error);

				// Pre-encode conversation in KV cache for faster next turn
				if (settingsStore.config.preEncodeConversation) {
					this.triggerPreEncode(
						allMessages,
						assistantMessage,
						streamedContent,
						effectiveModel,
						!!settingsStore.config.excludeReasoningFromContext
					);
				}
			},
			onModel: (modelName: string) => recordModel(modelName),
			onReasoningChunk: (chunk: string) => {
				streamedReasoningContent += chunk;
				// mark streaming state so a stop mid-thinking can persist the partial reasoning
				this.setChatStreaming(convId, streamedContent, currentMessageId, effectiveModel);
				const idx = conversationsStore.findMessageIndex(currentMessageId);

				conversationsStore.updateMessageAtIndex(idx, {
					reasoningContent: streamedReasoningContent
				});
				this.setChatReasoning(convId, true);
			},
			onTimings: (timings?: ChatMessageTimings, promptProgress?: ChatMessagePromptProgress) => {
				this.processing.applyStreamTimings(timings, promptProgress, convId);
			},
			onToolCallsStreaming: (toolCalls) => {
				const idx = conversationsStore.findMessageIndex(currentMessageId);

				conversationsStore.updateMessageAtIndex(idx, {
					toolCalls: JSON.stringify(toolCalls)
				});
			},
			onTurnComplete: (intermediateTimings: ChatMessageTimings) => {
				// Update the first assistant message with cumulative agentic timings
				const idx = conversationsStore.findMessageIndex(assistantMessage.id);

				conversationsStore.updateMessageAtIndex(idx, { timings: intermediateTimings });
			},
			updateToolResultMessage: async (
				messageId: string,
				content: string,
				extras?: DatabaseMessageExtra[]
			) => {
				// Persist latest content + merged extras; mirror into the active
				// store so the chat view sees live updates for streaming tools
				// (e.g. exec_shell_command). The existing tool message node
				// pointer stays put - the renderer is already scoped to it.
				const updates: Partial<DatabaseMessage> = { content };

				if (extras) {
					const idx = conversationsStore.findMessageIndex(messageId);
					const existing = idx >= 0 ? (conversationsStore.activeMessages[idx]?.extra ?? []) : [];
					const merged = [...existing, ...extras];

					updates.extra = merged;
				}

				if (conversationsStore.activeConversation?.id === convId) {
					const idx = conversationsStore.findMessageIndex(messageId);

					if (idx >= 0) conversationsStore.updateMessageAtIndex(idx, updates);
				}

				await DatabaseService.updateMessage(messageId, updates);
			}
		};
		const perChatOverrides = conversationsStore.preferences.getAllMcpServerOverrides();

		// Modo agente (AGENCY): si hay agentes seleccionados (o selección automática),
		// la tarea se delega al backend agéntico (job real + eventos SSE) en lugar de
		// responder directo con el modelo.
		if (
			agentStore.config.agentMode &&
			(agentStore.config.selectionMode !== 'manual' || agentStore.selectedAgencyAgents.length > 0)
		) {
			const task =
				firstUserMessageContent ||
				allMessages.findLast((message) => message.role === MessageRole.USER)?.content?.toString() ||
				'';
			const conversationHistory = allMessages
				.filter(
					(message) =>
						(message.role === MessageRole.USER || message.role === MessageRole.ASSISTANT) &&
						typeof message.content === 'string' &&
						message.content.trim()
				)
				.map((message) => ({ content: message.content as string, role: message.role }));

			if (
				conversationHistory.at(-1)?.role === MessageRole.USER &&
				conversationHistory.at(-1)?.content === task
			) {
				conversationHistory.pop();
			}

			if (task.trim()) {
				// El flujo de agencia escribe sus propios turnos (un bloque por
				// paso del plan) con su propio escritor de mensajes, así que no
				// usa los callbacks genéricos del chat: los mensajes del stream
				// agencial no deben mezclarse con el stream del modelo directo.
				const writer = this.createAgencyTurnWriter(convId, assistantMessage.id);

				try {
					await this.streamSelectedAgencyTask(
						task,
						{ conversation_history: conversationHistory.slice(-8) },
						agentStore.selectedAgencyAgents,
						writer.callbacks,
						abortController.signal,
						agentStore.config.executionMode === 'parallel',
						agentStore.config.selectionMode,
						convId,
						writer.state
					);

					if (firstUserMessageContent) {
						await this.generateTitleWithLLM(firstUserMessageContent, writer.state.text, convId);
					}

					return;
				} catch (error) {
					// El job puede haber fallado antes de abrir el SSE (400/500 del
					// backend): la timeline no debe quedarse "en curso" para siempre.
					agentStore.finishAgencyTimeline();

					if (isAbortError(error)) {
						this.cleanupStreaming(convId);

						return;
					}

					await writer.callbacks.onError?.(
						error instanceof Error ? error : new Error(String(error))
					);

					return;
				} finally {
					writer.dispose();
				}
			}
		}

		{
			const agenticResult = await agenticStore.runAgenticFlow({
				callbacks: streamCallbacks,
				conversationId: convId,
				flowRootMessageId: assistantMessage.id,
				messages: allMessages,
				options: {
					...this.getApiOptions(),
					...(effectiveModel ? { model: effectiveModel } : {})
				},
				perChatOverrides,
				signal: abortController.signal
			});

			if (agenticResult.handled) {
				// Generate LLM based title for new conversations after agentic flow completes
				if (firstUserMessageContent) {
					await this.generateTitleWithLLM(firstUserMessageContent, streamedContent, convId);
				}

				// Check if there's a pending steering message to re-send
				const pending = agenticStore.consumePendingSteeringMessage(convId);

				if (pending) {
					await this.sendMessage(pending.content, pending.extras);
				}

				return;
			}
		}

		await ChatService.sendMessage(
			allMessages,
			{
				...this.getApiOptions(),
				...(effectiveModel ? { model: effectiveModel } : {}),
				onChunk: streamCallbacks.onChunk,
				onComplete: async (
					finalContent?: string,
					reasoningContent?: string,
					timings?: ChatMessageTimings,
					toolCalls?: string
				) => {
					const content = streamedContent || finalContent || '';
					const reasoning = streamedReasoningContent || reasoningContent;
					const updateData: Record<string, unknown> = {
						content,
						reasoningContent: reasoning || undefined,
						timings,
						toolCalls: toolCalls || ''
					};

					if (resolvedModel && !modelPersisted) updateData.model = resolvedModel;

					await DatabaseService.updateMessage(currentMessageId, updateData);
					const idx = conversationsStore.findMessageIndex(currentMessageId);
					const uiUpdate: Partial<DatabaseMessage> = {
						content,
						reasoningContent: reasoning || undefined,
						toolCalls: toolCalls || ''
					};

					if (timings) uiUpdate.timings = timings;

					if (resolvedModel) uiUpdate.model = resolvedModel;

					conversationsStore.updateMessageAtIndex(idx, uiUpdate);
					await conversationsStore.updateCurrentNode(currentMessageId);
					cleanupStreamingState();

					if (onComplete) await onComplete(content);

					if (serverStore.isRouterMode) modelsStore.fetchRouterModels().catch(console.error);

					// Generate LLM based title for new conversations (avoids stale reference
					// issue when user switches conversations while streaming)
					if (firstUserMessageContent) {
						await this.generateTitleWithLLM(firstUserMessageContent, streamedContent, convId);
					}

					// Check if there's a pending message queued during streaming
					const pending = this.consumePendingMessage(convId);

					if (pending) {
						await this.sendMessage(pending.content, pending.extras);
					}
				},
				onCompletionId: streamCallbacks.onCompletionId,
				onConnectionState: (state: StreamConnectionState) => {
					if (convId === conversationsStore.activeConversation?.id) {
						this.streamConnectionState = state;
					}
				},
				onError: streamCallbacks.onError,
				onModel: streamCallbacks.onModel,
				onReasoningChunk: streamCallbacks.onReasoningChunk,
				onTimings: streamCallbacks.onTimings,
				stream: true
			},
			convId,
			abortController.signal
		);
	}

	syncLoadingStateForChat(convId: string): void {
		const s = this.chatStreamingStates.get(convId);

		this.currentResponse = s?.response || '';
		this.processing.setActiveConversation(convId);

		// Sync streaming content to activeMessages so UI displays current content
		if (s?.response && s?.messageId) {
			const idx = conversationsStore.findMessageIndex(s.messageId);

			if (idx !== -1) {
				conversationsStore.updateMessageAtIndex(idx, { content: s.response });
			}
		}
	}

	async syncRemoteRunningStreams(): Promise<void> {
		return this.streams.syncRemoteRunningStreams();
	}

	/**
	 * Message flows (edit / regenerate / continue / delete) live in
	 * ChatMessageFlows; these delegate so consumers keep a single entry point.
	 */
	async updateMessage(messageId: string, newContent: string): Promise<void> {
		return this.flows.updateMessage(messageId, newContent);
	}

	private abortRequest(convId?: string): void {
		if (convId) {
			const c = this.abortControllers.get(convId);

			if (c) {
				c.abort();
				this.abortControllers.delete(convId);
			}
		} else {
			for (const c of this.abortControllers.values()) c.abort();
			this.abortControllers.clear();
		}
	}
	private async generateTitleWithLLM(
		userContent: string,
		assistantContent: string,
		convId: string
	): Promise<void> {
		const effectiveModel =
			serverStore.isRouterMode && modelsStore.selectedModelName
				? modelsStore.selectedModelName
				: undefined;
		const configValue = settingsStore.config;
		const titlePromptTemplate =
			typeof configValue.titleGenerationPrompt === 'string' &&
			configValue.titleGenerationPrompt.trim()
				? configValue.titleGenerationPrompt
				: TITLE_GENERATION.DEFAULT_PROMPT;
		const titlePrompt = titlePromptTemplate
			.replace('{{USER}}', String(userContent || ''))
			.replace('{{ASSISTANT}}', String(assistantContent || ''));
		const titleMessage: ApiChatMessageData = {
			content: titlePrompt,
			role: MessageRole.USER
		};
		const titleResponse = await ChatService.generateTitle(titleMessage, effectiveModel);

		if (!titleResponse) {
			return;
		}

		let cleanTitle = titleResponse.trim();

		cleanTitle = cleanTitle
			.replace(TITLE_GENERATION.PREFIX_PATTERN, '')
			.replace(TITLE_GENERATION.QUOTE_PATTERN, '')
			.trim();

		if (!cleanTitle || cleanTitle.length < TITLE_GENERATION.MIN_LENGTH) {
			const firstLine = userContent.split('\n').find((l) => l.trim().length > 0);

			cleanTitle = firstLine ? firstLine.trim() : TITLE_GENERATION.FALLBACK;
		}

		if (cleanTitle && cleanTitle.length >= TITLE_GENERATION.MIN_LENGTH) {
			await conversationsStore.updateConversationName(convId, cleanTitle);
		}
	}

	private getChatStreamingState(
		convId: string
	): { response: string; messageId: string } | undefined {
		return this.chatStreamingStates.get(convId);
	}

	private async savePartialResponseIfNeeded(convId?: string): Promise<void> {
		const conversationId = convId || conversationsStore.activeConversation?.id;

		if (!conversationId) return;

		const streamingState = this.getChatStreamingState(conversationId);

		if (!streamingState) return;

		const messages =
			conversationId === conversationsStore.activeConversation?.id
				? conversationsStore.activeMessages
				: await conversationsStore.getConversationMessages(conversationId);

		if (!messages.length) return;

		const lastMessage = messages[messages.length - 1];

		if (lastMessage?.role !== MessageRole.ASSISTANT) return;

		const partialContent = streamingState.response;
		const partialReasoning = lastMessage.reasoningContent || '';
		// snapshot the streamed tool calls before clearing so we still know whether
		// anything was captured when deciding to skip the DB write below
		const hadPartialToolCalls = !!lastMessage.toolCalls?.trim();

		// nothing to persist when content, reasoning, and streamed tool calls are all empty
		// (e.g. stop before any token). otherwise drop the partial tool call and write whatever
		// was streamed: incomplete arguments (truncated JSON, missing closing quote) would
		// otherwise be re-sent to the server on the next turn and rejected.
		if (!partialContent.trim() && !partialReasoning.trim() && !hadPartialToolCalls) return;

		try {
			const updateData: {
				content?: string;
				reasoningContent?: string;
				toolCalls?: string;
				timings?: ChatMessageTimings;
			} = {
				toolCalls: ''
			};

			if (partialContent.trim()) updateData.content = partialContent;

			if (partialReasoning.trim()) updateData.reasoningContent = partialReasoning;

			const lastKnownState = this.processing.getState(conversationId);

			if (lastKnownState) {
				updateData.timings = {
					cache_n: lastKnownState.cacheTokens || 0,
					predicted_ms:
						lastKnownState.tokensPerSecond && lastKnownState.tokensDecoded
							? (lastKnownState.tokensDecoded / lastKnownState.tokensPerSecond) * 1000
							: undefined,
					predicted_n: lastKnownState.tokensDecoded || 0,
					prompt_ms: lastKnownState.promptMs,
					prompt_n: lastKnownState.promptTokens || 0
				};
			}

			await DatabaseService.updateMessage(lastMessage.id, updateData);
			lastMessage.content = partialContent;
			// mirror the drop into the in-memory message so the next request sent via
			// sendMessage (queued pending, Send immediately, or manual follow-up) reads
			// the cleared value, not whatever the streaming widget had been showing
			lastMessage.toolCalls = '';

			if (updateData.timings) lastMessage.timings = updateData.timings;
		} catch (error) {
			lastMessage.content = partialContent;
			lastMessage.toolCalls = '';
			console.error('Failed to save partial response:', error);
		}
	}

	/**
	 * Ejecuta la tarea delegando en el sistema de agencia del backend
	 * (`POST /agent_execute` en el servicio `agent`) y traduce el stream SSE del
	 * job a bloques del chat: uno por paso (supervisor, cada especialista y la
	 * síntesis final), con su razonamiento, su texto y sus tool calls dentro de
	 * su propio bloque en lugar de todo dentro del mensaje del supervisor.
	 */
	private async streamSelectedAgencyTask(
		task: string,
		context: Record<string, unknown>,
		agents: string[],
		callbacks: ChatStreamCallbacks,
		signal: AbortSignal,
		parallel: boolean,
		selectionMode: 'manual' | 'automatic' | 'hybrid',
		conversationId: string | null,
		state: AgencyTurnState
	): Promise<void> {
		// Cada ejecución agencial arranca con la timeline limpia y queda ligada a
		// la conversación que la lanzó (plan, tarjetas de agentes y síntesis del
		// mensaje anterior no deben acumularse ni aparecer en otros chats).
		agentStore.beginAgencyTimeline(conversationId);
		const endpoint = agentStore.config.endpoint.replace(/\/$/, '');
		const response = await fetch(`${endpoint}/agent_execute`, {
			body: JSON.stringify({
				agents,
				context,
				// El id permite al backend deduplicar por conversación y localizar el
				// job activo con `GET /agent_jobs?conversation_id=...`.
				conversation_id: conversationId ?? undefined,
				parallel,
				selection_mode: selectionMode,
				task
			}), // Mismo canal que el resto del API de agentes: endpoint de `api.ts`
			// (host actual o proxy) y token si está configurado.
			headers: { 'Content-Type': 'application/json', ...authHeaders() },
			method: 'POST',
			signal
		});

		if (!response.ok) throw new Error(`Agent HTTP error: ${response.status}`);

		const { job_id: jobId } = (await response.json()) as { job_id?: string };

		if (!jobId) throw new Error('El agente no devolvio job_id');

		const run: AgencyChatRun = {
			appliedIndex: 0,
			conversationId,
			endpoint,
			jobId,
			rootMessageId: state.messageId,
			text: ''
		};

		if (conversationId) {
			this.agencyRuns.set(conversationId, run);
		}

		agentStore.applyChatAgencyEvent({ job_id: jobId, type: 'step_start' });

		try {
			await this.consumeAgencyJob(jobId, endpoint, conversationId, callbacks, state, run, signal);
		} finally {
			if (conversationId) this.agencyRuns.delete(conversationId);
			agentStore.finishAgencyTimeline();
		}
	}

	/**
	 * Consume el SSE del job agencial y lo traduce a los mensajes del chat.
	 *
	 * Resuelve tres fallos clásicos del modo agencia en el chat nativo:
	 *
	 * 1. Bloques separados: cada paso del plan abre su propio mensaje asistente
	 *    sellado con `agencyStep`, así el razonamiento de cada agente vive en su
	 *    bloque (con sus tools intercalados) y no dentro del bloque del
	 *    supervisor. Cada turno del paso es su propio mensaje, de modo que el
	 *    renderer muestra razonamiento antes y después de cada tool call.
	 * 2. Cortes del stream: un `onerror` del EventSource ya NO cancela el job.
	 *    Los tools largos y los proxies intermedios cortan la conexión mientras
	 *    el backend sigue trabajando, así que la conexión se reintenta.
	 * 3. Reanudación por índice: cada conexión pide `?from=N` y el backend
	 *    reporta su índice real con `sse_start`; los eventos ya aplicados se
	 *    descartan y los llegados durante el corte se aplican en orden.
	 */
	private async consumeAgencyJob(
		jobId: string,
		endpoint: string,
		conversationId: string | null,
		callbacks: ChatStreamCallbacks,
		state: AgencyTurnState,
		run: AgencyChatRun,
		signal: AbortSignal
	): Promise<void> {
		let toolSequence = 0;
		let pendingCalls: Array<ApiChatCompletionToolCall & { id: string }> = [];
		let latestTimings: ChatMessageTimings | undefined;
		let step: AgencyStepMeta | null = null;
		/** Ids del plan por índice: `step_start` no los manda, `agency_selected` sí. */
		const planStepIds: string[] = [];
		const planStepsById = new SvelteMap<string, Record<string, unknown>>();
		// Totales del job: cada `usage` (especialista o supervisor) suma aquí, y
		// es lo que se persiste y se renderiza como métricas del mensaje final.
		let usageTotals: ChatMessageTimings = {
			cache_n: 0,
			predicted_ms: 0,
			predicted_n: 0,
			prompt_ms: 0,
			prompt_n: 0
		};
		// Último frame aceptado por agente: el backend puede reenviar el mismo
		// uso (usage + timings) y no debe sumarse dos veces.
		const usageSignatures = new Map<string, string>();

		const supervisorStep = (label: string): AgencyStepMeta => ({ label, role: 'supervisor' });

		const textOf = (value: unknown): string => {
			if (typeof value === 'string') return value;
			if (!value || typeof value !== 'object') return '';

			const text = (value as { text?: unknown }).text;

			return typeof text === 'string' ? text : '';
		};

		const recordPlanSteps = (value: unknown): void => {
			const steps = Array.isArray(value) ? value : [];

			if (!steps.length) return;

			planStepIds.length = 0;
			planStepsById.clear();

			steps.forEach((item, index) => {
				const data = (item && typeof item === 'object' ? item : {}) as Record<string, unknown>;

				const id = String(data.id ?? data.task_id ?? `agency_${index}`);
				planStepIds[index] = id;
				planStepsById.set(id, data);
			});
		};

		/**
		 * Identidad del paso del plan. `step_start` trae `idx` y `agent_start`
		 * trae `task_id`: ambos se resuelven al id del plan para que los dos
		 * eventos del MISMO paso abran un único bloque.
		 */
		const resolveStepId = (payload: Record<string, unknown>): string | undefined => {
			const explicit = payload.task_id ?? payload.id;

			if (typeof explicit === 'string' && explicit) return explicit;

			const idx = Number(payload.idx ?? 0);

			return idx > 0 ? planStepIds[idx - 1] : undefined;
		};

		const specialistStep = (payload: Record<string, unknown>): AgencyStepMeta => {
			const agent = String(payload.agent ?? 'agente');
			const subtask = String(payload.subtask ?? '');
			const taskId = resolveStepId(payload);
			const planStep = taskId ? planStepsById.get(taskId) : undefined;
			const total = Number(payload.total ?? planStepIds.length ?? 0);
			const index = taskId ? planStepIds.indexOf(taskId) + 1 : Number(payload.idx ?? 0);
			const label = index > 0 && total > 0 ? `▶ ${agent} · paso ${index}/${total}` : `▶ ${agent}`;

			return {
				agent,
				label,
				phases: Array.isArray(planStep?.phases)
					? planStep.phases.map(String)
					: typeof planStep?.phase === 'string'
						? [planStep.phase]
						: undefined,
				role: 'specialist',
				subtask: subtask || undefined,
				task_id: taskId
			};
		};

		/**
		 * Persiste el turno en curso (texto + razonamiento + tool calls). El
		 * razonamiento va al `reasoningContent` del turno: cada turno es su
		 * propio mensaje, así el renderer muestra un bloque de razonamiento
		 * antes y después de cada uso de tool.
		 */
		const finishTurn = async (): Promise<void> => {
			if (!state.content && !state.reasoning && pendingCalls.length === 0) return;

			await callbacks.onAssistantTurnComplete?.(
				state.content,
				state.reasoning || undefined,
				latestTimings,
				pendingCalls.length ? [...pendingCalls] : undefined,
				step ?? undefined
			);
			state.content = '';
			state.reasoning = '';
		};

		/**
		 * Cambia de bloque: abre un mensaje nuevo por paso (mismo rol y misma
		 * etiqueta = mismo bloque, y el turno continúa donde estaba). Devuelve
		 * true cuando abrió un bloque nuevo.
		 */
		const switchStep = async (nextStep: AgencyStepMeta): Promise<boolean> => {
			if (!step) {
				// El primer bloque reutiliza el mensaje asistente que abrió el
				// envío: es el bloque del supervisor sobre el prompt del usuario.
				step = nextStep;
				await this.stampAgencyStep(conversationId, state.messageId, nextStep);

				return false;
			}

			if (step.role === nextStep.role && step.label === nextStep.label) {
				// Mismo bloque: si el id del plan llega con el segundo evento, se
				// completa sin abrir un mensaje nuevo.
				if (!step.task_id && nextStep.task_id) {
					step = nextStep;
					await this.stampAgencyStep(conversationId, state.messageId, nextStep);
				}

				return false;
			}

			await finishTurn();
			step = nextStep;
			pendingCalls = [];

			const message = await callbacks.createAssistantMessage?.(nextStep);

			if (message) state.messageId = message.id;

			return true;
		};

		const emitInline = (chunk: string): void => {
			if (chunk) callbacks.onChunk?.(chunk);
		};

		const newTurnInStep = async (): Promise<void> => {
			const message = await callbacks.createAssistantMessage?.(step ?? undefined);

			if (message) state.messageId = message.id;
		};

		/**
		 * Traducción frame → mensajes del chat. El transporte (índices,
		 * reanudación, reintentos, frames corruptos y fin del job) vive en
		 * `$lib/utils/agency-stream`; aquí solo se decide qué se ve.
		 */
		const translateFrame = async (frame: AgencySseFrame): Promise<void> => {
			const payload = frame.payload as Record<string, unknown> | string | undefined;
			const payloadObj = (payload && typeof payload === 'object' ? payload : {}) as Record<
				string,
				unknown
			>;
			const meta = frame.meta ?? {};
			const fromSpecialist = meta.role === 'specialist';

			try {
				switch (frame.type) {
					case 'agency_selected': {
						recordPlanSteps(payloadObj.steps);
						await switchStep(supervisorStep('🧠 Supervisor · plan'));
						break;
					}
					case 'plan': {
						recordPlanSteps(payloadObj.steps);
						await switchStep(supervisorStep('🧠 Supervisor · plan'));

						const steps = Array.isArray(payloadObj.steps) ? payloadObj.steps : [];

						if (steps.length) {
							const lines = steps.map((item, index) => {
								const data = (item && typeof item === 'object' ? item : {}) as Record<
									string,
									unknown
								>;
								const agent = String(data.agent_name ?? data.agent ?? 'agente');
								const subtask = String(data.subtask ?? '');

								return `${index + 1}. **${agent}** — ${subtask}`;
							});

							emitInline(
								`\n\n📋 **Plan de trabajo (${steps.length} pasos):**\n${lines.join('\n')}\n`
							);
						}

						break;
					}
					case 'supervisor': {
						await switchStep(supervisorStep('🧠 Supervisor · plan'));

						if (String(payloadObj.status ?? '') === 'planning') {
							emitInline('**🧠 Supervisor** analizando la tarea y creando el plan…\n');
						}

						break;
					}
					case 'handoff': {
						const opened = await switchStep(supervisorStep('🧠 Supervisor · síntesis'));

						if (opened) {
							emitInline('**🤝 Handoff → Supervisor**: integrando los informes…\n\n');
						}

						break;
					}
					case 'agent_start': {
						const nextStep = specialistStep(payloadObj);
						await switchStep(nextStep);

						break;
					}
					case 'step_start': {
						// `_run_agency_mode` emite los `step_start` como
						// previsualización del plan y luego un `agent_start` por
						// paso al arrancar de verdad. Abrir bloque aquí lanzaba
						// una copia de cada paso y reabría el primero al llegar
						// su `agent_start`. El bloque de trabajo lo abre solo
						// `agent_start`; `step_start` queda como metadato.
						break;
					}
					case 'agent_end': {
						const agent = String(payloadObj.agent ?? 'agente');
						const success = payloadObj.success !== false;
						const errorText = String(payloadObj.error ?? '');

						emitInline(
							success
								? `\n✅ **${agent}** completado\n`
								: `\n❌ **${agent}** falló${errorText ? `: ${errorText}` : ''}\n`
						);

						break;
					}
					case 'text': {
						emitInline(textOf(payload));
						break;
					}
					case 'reasoning': {
						callbacks.onReasoningChunk?.(textOf(payload));
						break;
					}
					case 'usage': {
						// Métricas del mensaje = uso TOTAL del job: se acumulan
						// los especialistas y el supervisor.
						const timings: ChatMessageTimings = {
							cache_n: Number(payloadObj.cache_n ?? 0),
							predicted_ms: Number(payloadObj.completion_ms ?? payloadObj.predicted_ms ?? 0),
							predicted_n: Number(payloadObj.completion_tokens ?? payloadObj.predicted_n ?? 0),
							prompt_ms: Number(payloadObj.prompt_ms ?? 0),
							prompt_n: Number(payloadObj.prompt_tokens ?? payloadObj.prompt_n ?? 0)
						};
						const key = typeof meta.agent === 'string' && meta.agent ? meta.agent : 'supervisor';
						const signature = JSON.stringify(timings);

						// Una repetición del mismo frame no suma dos veces; un
						// frame nuevo (otra petición LLM del mismo agente) sí.
						if (usageSignatures.get(key) !== signature) {
							usageSignatures.set(key, signature);
							usageTotals = {
								cache_n: (usageTotals.cache_n ?? 0) + (timings.cache_n ?? 0),
								predicted_ms: (usageTotals.predicted_ms ?? 0) + (timings.predicted_ms ?? 0),
								predicted_n: (usageTotals.predicted_n ?? 0) + (timings.predicted_n ?? 0),
								prompt_ms: (usageTotals.prompt_ms ?? 0) + (timings.prompt_ms ?? 0),
								prompt_n: (usageTotals.prompt_n ?? 0) + (timings.prompt_n ?? 0)
							};
							latestTimings = usageTotals;
							state.timings = usageTotals;
							callbacks.onTimings?.(latestTimings);
						}

						break;
					}
					case 'tool_call':
					case 'native_tool_call': {
						if (pendingCalls.length) await finishTurn();

						const toolCallId = `agency_${jobId}_${toolSequence++}`;
						const call: ApiChatCompletionToolCall & { id: string } = {
							function: {
								arguments: JSON.stringify(payloadObj.arguments ?? {}),
								name: String(payloadObj.name ?? 'tool')
							},
							id: toolCallId,
							type: 'function'
						};

						pendingCalls = [...pendingCalls, call];
						callbacks.onToolCallsStreaming?.([call]);
						await finishTurn();

						break;
					}
					case 'tool_result': {
						const call = pendingCalls[0];

						if (!call) break;

						const result = payloadObj.result;

						await callbacks.createToolResultMessage?.(
							call.id,
							typeof result === 'string' ? result : JSON.stringify(result ?? '')
						);
						pendingCalls = [];

						// Turno nuevo del MISMO paso: el agente sigue
						// razonando en su bloque después de ver el resultado
						// del tool.
						await newTurnInStep();

						break;
					}
					case 'incomplete': {
						// El backend avisa de que el plan se cerró sin que todos los
						// pasos produjeran trabajo (paso cortado por la guarda,
						// artefacto que no apareció, recuperación agotada). No corta
						// el job: el `done` sin meta lo cierra. Antes este evento
						// no tenía case y el usuario no veía nada.
						emitInline(`\n⚠️ **Plan incompleto**: ${String(payloadObj.reason ?? '')}\n`);

						break;
					}
					case 'tool_denied': {
						// La whitelist del agente (`tool_policy`, backend) le negó una
						// herramienta: el paso NO la ejecutó. El modelo ya recibe su
						// propio aviso; aquí se le enseña al usuario, que si no veía
						// desaparecer acciones sin explicación.
						emitInline(
							`\n⛔ **${String(payloadObj.agent ?? 'agente')}** intentó usar ` +
								`\`${String(payloadObj.name ?? 'una herramienta')}\`: no está permitida para él.\n`
						);

						break;
					}
					case 'error': {
						// El fallo de un paso/subagente no aborta el job: el
						// supervisor re-planifica y sintetiza. Solo los errores
						// globales cortan.
						if (fromSpecialist || typeof payloadObj.agent === 'string') break;

						throw new Error(textOf(payload) || String(payloadObj.error ?? 'Error del agente'));
					}
					case 'done': {
						// Los `done` que viajan por el bus (especialistas,
						// síntesis y pasos del plan) llegan con `meta` y son
						// hitos intermedios. El fin real del job es el `done`
						// sin `meta` que el worker appendea en su `finally`:
						// el TRANSPORTE lo detecta y cierra el stream.
						if (fromSpecialist || Object.keys(meta).length > 0) break;

						if (payloadObj.content && !state.content.includes(String(payloadObj.content))) {
							emitInline(String(payloadObj.content));
						}

						if (payloadObj.reasoning && !state.reasoning.includes(String(payloadObj.reasoning))) {
							callbacks.onReasoningChunk?.(String(payloadObj.reasoning));
						}

						await finishTurn();
						callbacks.onFlowComplete?.(latestTimings);

						break;
					}
				}
			} catch (error) {
				// Un fallo al pintar el frame corta el stream: para eso está el catch
				// de `onFrame` en el transporte.
				throw error instanceof Error ? error : new Error(String(error));
			}
		};

		/**
		 * Corta el job en el BACKEND. Sin esto, el botón de parar nativo solo
		 * cerraba el EventSource: el orquestador y los agentes seguían trabajando
		 * (y el modelo generando) hasta terminar.
		 */
		const cancelAgencyJob = () => {
			void fetch(`${endpoint}/agent_execute/${jobId}`, {
				headers: { ...authHeaders() },
				method: 'DELETE'
			}).catch(() => {
				// La cancelación es best-effort: el job ya puede haber terminado.
			});
		};

		await streamAgencyJob({
			endpoint,
			from: run.appliedIndex,
			jobId,
			onAppliedIndex: (appliedIndex) => {
				run.appliedIndex = appliedIndex;
			},
			onCancel: () => cancelAgencyJob(),
			onFrame: async (frame) => {
				await translateFrame(frame);

				// Timeline del panel de agentes: la alimenta el propio stream del
				// chat para no abrir una segunda conexión SSE por job.
				agentStore.applyChatAgencyEvent({
					...frame,
					job_id: jobId,
					type: frame.type ?? 'message'
				});
			},
			onReconnect: (attempt, delayMs) => {
				console.warn(
					`[agency] stream SSE caido; reintento ${attempt}/${AGENCY_RECONNECT_MAX_ATTEMPTS} en ${delayMs}ms`
				);
			},
			signal
		});
	}

	/**
	 * Construye el escritor de turnos del flujo agencial: crea el mensaje de cada
	 * paso, persiste texto/razonamiento/tool calls del turno en curso y vuelca el
	 * stream de forma incremental.
	 *
	 * El volcado incremental (`AGENCY_FLUSH_INTERVAL_MS`) es lo que hace que un
	 * F5 a mitad de ejecución no borre el chat: lo ya generado está en la
	 * conversación y no solo en memoria.
	 */
	private createAgencyTurnWriter(
		convId: string,
		initialMessageId: string
	): { callbacks: ChatStreamCallbacks; dispose: () => void; state: AgencyTurnState } {
		const state: AgencyTurnState = {
			content: '',
			messageId: initialMessageId,
			reasoning: '',
			text: ''
		};
		let disposed = false;

		const mirror = (updates: Partial<DatabaseMessage>): void => {
			if (conversationsStore.activeConversation?.id !== convId) return;

			const idx = conversationsStore.findMessageIndex(state.messageId);

			if (idx >= 0) conversationsStore.updateMessageAtIndex(idx, updates);
		};

		const flush = async (): Promise<void> => {
			if (!state.content && !state.reasoning) return;

			const updates: Partial<DatabaseMessage> = {
				content: state.content,
				reasoningContent: state.reasoning || undefined
			};

			mirror(updates);

			try {
				await DatabaseService.updateMessage(state.messageId, updates);
			} catch (error) {
				console.error('[ChatStore] volcado del stream agencial falló:', error);
			}
		};

		const flushTimer = setInterval(() => void flush(), AGENCY_FLUSH_INTERVAL_MS);

		const dispose = (): void => {
			if (disposed) return;

			disposed = true;
			clearInterval(flushTimer);
			this.setChatLoading(convId, false);
			this.clearChatStreaming(convId, state.messageId);
			this.processing.setState(convId, null);
		};

		/** Encadena el mensaje nuevo en la rama activa y mueve el nodo actual. */
		const appendToBranch = async (message: DatabaseMessage): Promise<void> => {
			if (conversationsStore.activeConversation?.id === convId) {
				conversationsStore.addMessageToActive(message);
				await conversationsStore.updateCurrentNode(message.id);
			} else {
				await DatabaseService.updateCurrentNode(convId, message.id);
			}
		};

		const callbacks: ChatStreamCallbacks = {
			createAssistantMessage: async (agencyStep?: AgencyStepMeta) => {
				const message = await DatabaseService.createMessageBranch(
					{
						children: [],
						content: '',
						convId,
						// El sello marca el bloque agencial del turno: el renderer lo
						// usa para rotular y separar cada paso del plan.
						...(agencyStep ? { agencyStep } : {}),
						role: MessageRole.ASSISTANT,
						timestamp: Date.now(),
						toolCalls: '',
						type: MessageType.TEXT
					},
					state.messageId
				);

				await appendToBranch(message);
				state.content = '';
				state.messageId = message.id;
				state.reasoning = '';
				state.timings = undefined;

				return message;
			},
			createToolResultMessage: async (toolCallId, content, extras, toolCwd) => {
				const message = await DatabaseService.createMessageBranch(
					{
						children: [],
						content,
						convId,
						extra: extras,
						role: MessageRole.TOOL,
						timestamp: Date.now(),
						toolCallId,
						toolCalls: '',
						toolCwd,
						type: MessageType.TEXT
					},
					state.messageId
				);

				await appendToBranch(message);

				return message;
			},
			onAssistantTurnComplete: async (
				content,
				reasoningContent,
				timings,
				toolCalls,
				agencyStep
			) => {
				const updates: Partial<DatabaseMessage> = {
					content,
					reasoningContent: reasoningContent || undefined,
					toolCalls: toolCalls ? JSON.stringify(toolCalls) : ''
				};

				if (timings) updates.timings = timings;

				if (agencyStep) updates.agencyStep = agencyStep;

				mirror(updates);

				try {
					await DatabaseService.updateMessage(state.messageId, updates);
				} catch (error) {
					console.error('[ChatStore] no se pudo persistir el turno agencial:', error);
				}
			},
			onChunk: (chunk: string) => {
				state.content += chunk;
				state.text += chunk;
				this.setChatStreaming(convId, state.content, state.messageId, null);
				this.setChatReasoning(convId, false);
				mirror({ content: state.content });
			},
			onError: async (error: Error) => {
				dispose();
				this.showErrorDialog({ message: error.message, type: ErrorDialogType.SERVER });
			},
			onFlowComplete: () => {
				void flush();
				dispose();
			},
			onReasoningChunk: (chunk: string) => {
				state.reasoning += chunk;
				this.setChatStreaming(convId, state.content, state.messageId, null);
				this.setChatReasoning(convId, true);
				mirror({ reasoningContent: state.reasoning });
			},
			onTimings: (timings, promptProgress) => {
				this.processing.applyStreamTimings(timings, promptProgress, convId);
			},
			onToolCallsStreaming: (toolCalls) => {
				if (conversationsStore.activeConversation?.id !== convId) return;

				const idx = conversationsStore.findMessageIndex(state.messageId);

				if (idx >= 0) {
					conversationsStore.updateMessageAtIndex(idx, {
						toolCalls: JSON.stringify(toolCalls)
					});
				}
			}
		};

		return { callbacks, dispose, state };
	}

	/**
	 * Reengancha el chat a una ejecución agencial que sigue viva en el backend.
	 *
	 * El job y su cola de eventos viven en el backend, así que una recarga (F5)
	 * no tiene por qué perder la ejecución: se busca el job en curso de la
	 * conversación, se rehace la parte parcial de la rama y se vuelve a consumir
	 * el stream completo. Antes, recargar dejaba el chat vacío y la generación se
	 * perdía de vista.
	 */
	async recoverAgencyJob(convId: string): Promise<boolean> {
		if (!convId) return false;

		if (this.agencyRuns.has(convId)) return true;

		const endpoint = agentStore.config.endpoint.replace(/\/$/, '');

		let jobId: string | null = null;

		try {
			const response = await fetch(
				`${endpoint}/agent_jobs?conversation_id=${encodeURIComponent(convId)}`,
				{ headers: { ...authHeaders() } }
			);

			if (!response.ok) return false;

			const data = (await response.json()) as {
				jobs?: Array<{ id?: string; status?: string }>;
			};

			jobId = data.jobs?.find((job) => job.status === 'running' && job.id)?.id ?? null;
		} catch (error) {
			console.warn('[agency] no se pudo consultar el job en curso:', error);

			return false;
		}

		if (!jobId) return false;

		const initialMessage = await this.rebuildAgencyTail(convId);

		if (!initialMessage) return false;

		// El chat es el único dueño del stream del job: la timeline del panel se
		// alimenta desde aquí (una sola conexión SSE por job).
		agentStore.beginAgencyTimeline(convId);
		agentStore.applyChatAgencyEvent({ job_id: jobId, type: 'step_start' });

		const writer = this.createAgencyTurnWriter(convId, initialMessage.id);
		const run: AgencyChatRun = {
			appliedIndex: 0,
			conversationId: convId,
			endpoint,
			jobId,
			rootMessageId: initialMessage.id,
			text: ''
		};

		this.agencyRuns.set(convId, run);
		this.processing.setActiveConversation(convId);
		this.setChatLoading(convId, true);
		this.setChatStreaming(convId, '', initialMessage.id, null);

		const signal = this.getOrCreateAbortController(convId).signal;

		try {
			await this.consumeAgencyJob(
				jobId,
				endpoint,
				convId,
				writer.callbacks,
				writer.state,
				run,
				signal
			);
		} catch (error) {
			agentStore.finishAgencyTimeline();

			if (!isAbortError(error)) {
				await writer.callbacks.onError?.(error instanceof Error ? error : new Error(String(error)));
			}
		} finally {
			this.agencyRuns.delete(convId);
			writer.dispose();
		}

		return true;
	}

	/**
	 * Rehace la cola parcial del job agencial: borra la salida que el job ya
	 * escribió tras el último mensaje del usuario y recrea el mensaje asistente
	 * raíz del flujo, de modo que el replay del stream reconstruya los bloques
	 * sin duplicar nada.
	 */
	private async rebuildAgencyTail(convId: string): Promise<DatabaseMessage | null> {
		const userMessageId = this.findAgencyAnchorId(convId);

		if (!userMessageId) return null;

		const all = await conversationsStore.getConversationMessages(convId);
		const anchor = all.find((message) => message.id === userMessageId);

		if (!anchor) return null;

		for (const childId of [...anchor.children]) {
			try {
				await DatabaseService.deleteMessageCascading(convId, childId);
			} catch (error) {
				console.warn('[agency] no se pudo limpiar la cola parcial del job:', error);
			}
		}

		await conversationsStore.updateCurrentNode(userMessageId);
		await conversationsStore.refreshActiveMessages();

		const initialMessage = await this.createAssistantMessage(userMessageId);

		conversationsStore.addMessageToActive(initialMessage);
		await conversationsStore.updateCurrentNode(initialMessage.id);

		return initialMessage;
	}

	/** Ancla del job: el último mensaje real del usuario en la rama activa. */
	private findAgencyAnchorId(convId: string): string | null {
		if (conversationsStore.activeConversation?.id !== convId) return null;

		const messages = conversationsStore.activeMessages;

		for (let i = messages.length - 1; i >= 0; i--) {
			const message = messages[i];

			if (message.role === MessageRole.USER && !message.isSynthetic) return message.id;
		}

		return null;
	}

	/** Sella un mensaje con el bloque agencial al que pertenece. */
	private async stampAgencyStep(
		conversationId: string | null,
		messageId: string,
		step: AgencyStepMeta
	): Promise<void> {
		if (conversationsStore.activeConversation?.id === conversationId) {
			const idx = conversationsStore.findMessageIndex(messageId);

			if (idx >= 0) conversationsStore.updateMessageAtIndex(idx, { agencyStep: step });
		}

		try {
			await DatabaseService.updateMessage(messageId, { agencyStep: step });
		} catch (error) {
			console.warn('[agency] no se pudo sellar el bloque agencial:', error);
		}
	}

	private async triggerPreEncode(
		allMessages: DatabaseMessage[],
		assistantMessage: DatabaseMessage,
		assistantContent: string,
		model?: string | null,
		excludeReasoning?: boolean
	): Promise<void> {
		this.cancelPreEncode();
		this.preEncodeAbortController = new AbortController();

		const signal = this.preEncodeAbortController.signal;

		try {
			const allIdle = await ChatService.areAllSlotsIdle(model, signal);

			if (!allIdle || signal.aborted) return;

			const messagesWithAssistant: DatabaseMessage[] = [
				...allMessages,
				{ ...assistantMessage, content: assistantContent }
			];

			await ChatService.preEncode(messagesWithAssistant, model, excludeReasoning, signal);
		} catch (err) {
			if (!isAbortError(err)) {
				console.warn('[ChatStore] Pre-encode failed:', err);
			}
		}
	}
}

export const chatStore = new ChatStore();
