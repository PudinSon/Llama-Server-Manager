import type { ApiChatCompletionToolCall } from '$lib/types/api';
import type { ChatStreamCallbacks, ChatMessagePromptProgress, ChatMessageTimings } from '$lib/types';

export async function streamAgencyTask(
	endpoint: string,
	task: string,
	agents: string[],
	parallel: boolean,
	selectionMode: 'manual' | 'automatic' | 'hybrid',
	callbacks: ChatStreamCallbacks,
	signal: AbortSignal,
	conversationId?: string,
	daemon = false,
	existingJobId?: string,
	context?: unknown
): Promise<void> {
	let jobId = existingJobId;
	if (!jobId) {
		const response = await fetch(`${endpoint.replace(/\/$/, '')}/agent_execute`, {
			body: JSON.stringify({
				agents,
				conversation_id: conversationId,
				context,
				daemon,
				parallel,
				selection_mode: selectionMode,
				task
			}),
			headers: { 'Content-Type': 'application/json' },
			method: 'POST',
			signal
		});
		if (!response.ok) throw new Error(`Agent HTTP error: ${response.status}`);
		({ job_id: jobId } = (await response.json()) as { job_id?: string });
	}
	if (!jobId) throw new Error('El agente no devolvio job_id');

	let content = '';
	let reasoning = '';
	let toolIndex = 0;
	let activeTool: { id: string; call: ApiChatCompletionToolCall } | null = null;
	let timings: ChatMessageTimings | undefined;

	await new Promise<void>((resolve, reject) => {
		const source = new EventSource(`${endpoint.replace(/\/$/, '')}/agent_events/${jobId}`);
		let settled = false;
		const settle = (error?: Error) => {
			if (settled) return;
			settled = true;
			source.close();
			error ? reject(error) : resolve();
		};
		const textValue = (value: unknown): string => {
			if (typeof value === 'string') return value;
			if (!value || typeof value !== 'object') return '';
			const text = (value as { text?: unknown }).text;
			return typeof text === 'string' ? text : '';
		};
		const handle = async (message: MessageEvent) => {
			try {
				const parsed = JSON.parse(message.data) as { type?: string; payload?: any; meta?: Record<string, unknown> };
				if (typeof parsed.type !== 'string') return;
				const event = { type: parsed.type, payload: parsed.payload, meta: parsed.meta ?? {} };
				const payload = event.payload;
				callbacks.onAgencyEvent?.({
					...event,
					...event.meta,
					job_id: jobId,
					conversation_id: conversationId
				});
				switch (event.type) {
					case 'text': {
						const chunk = textValue(payload);
						content += chunk;
						callbacks.onChunk?.(chunk);
						break;
					}
					case 'reasoning': {
						const chunk = textValue(payload);
						reasoning += chunk;
						callbacks.onReasoningChunk?.(chunk);
						break;
					}
					case 'usage':
						timings = {
							cache_n: Number(payload?.cache_n ?? 0),
							predicted_ms: Number(payload?.completion_ms ?? payload?.predicted_ms ?? 0),
							predicted_n: Number(payload?.completion_tokens ?? payload?.predicted_n ?? 0),
							prompt_ms: Number(payload?.prompt_ms ?? 0),
							prompt_n: Number(payload?.prompt_tokens ?? payload?.prompt_n ?? 0)
						};
						callbacks.onTimings?.(timings);
						break;
					case 'progress': {
						const progress: ChatMessagePromptProgress = {
							cache: Number(payload?.cache ?? 0),
							processed: Number(payload?.processed ?? payload?.n_past ?? 0),
							time_ms: Number(payload?.time_ms ?? (payload?.elapsed ?? 0) * 1000),
							total: Number(payload?.total ?? payload?.prompt_tokens ?? 0)
						};
						callbacks.onTimings?.(timings, progress);
						break;
					}
					case 'plan':
						break;
					case 'step_start':
					case 'agent_start':
					case 'agent_end':
					case 'agency_selected':
					case 'handoff':
					case 'supervisor':
						break;
					case 'tool_call': {
						const id = `agency_${jobId}_${toolIndex++}`;
						const name = String(payload?.name ?? 'tool');
						let argumentsValue = payload?.arguments;
						if (typeof argumentsValue === 'string') {
							try {
								argumentsValue = JSON.parse(argumentsValue);
							} catch {
								argumentsValue = {};
							}
						}
						if (!argumentsValue || typeof argumentsValue !== 'object') argumentsValue = {};
						const call: ApiChatCompletionToolCall = {
							id,
							type: 'function',
							function: { arguments: JSON.stringify(argumentsValue), name }
						};
						activeTool = { id, call };
						callbacks.onToolCallsStreaming?.([call]);
						break;
					}
					case 'tool_result':
						if (activeTool) {
							await callbacks.onAssistantTurnComplete?.(content, reasoning || undefined, timings, [
								activeTool.call
							]);
							await callbacks.createToolResultMessage?.(
								activeTool.id,
									typeof payload?.result === 'string'
										? payload.result
										: JSON.stringify(payload?.result ?? '') || ''
							);
							content = '';
							reasoning = '';
							activeTool = null;
							await callbacks.createAssistantMessage?.();
						}
						break;
					case 'error':
						settle(new Error(textValue(payload?.error ?? payload)));
						break;
					case 'done': {
						const result = payload?.result;
						const resultContent =
							typeof result === 'string'
								? result
								: typeof result?.result === 'string'
									? result.result
									: typeof result?.content === 'string'
										? result.content
										: '';
						const finalContent = textValue(payload?.content) || resultContent;
						const finalReasoning = textValue(payload?.reasoning);
						if (finalContent && !content) {
							content = finalContent;
							callbacks.onChunk?.(finalContent);
						}
						if (finalReasoning && !reasoning) {
							reasoning = finalReasoning;
							callbacks.onReasoningChunk?.(finalReasoning);
						}
						if (content || reasoning || activeTool)
							await callbacks.onAssistantTurnComplete?.(
								content,
								reasoning || undefined,
								timings,
								activeTool ? [activeTool.call] : undefined
							);
						callbacks.onFlowComplete?.(timings);
						settle();
						break;
					}
				}
			} catch (error) {
				settle(error instanceof Error ? error : new Error(String(error)));
			}
		};
		for (const type of [
			'text',
			'reasoning',
			'usage',
			'progress',
			'plan',
			'step_start',
			'tool_call',
			'native_tool_call',
			'tool_result',
			'error',
			'done'
		])
			source.addEventListener(type, (event) => void handle(event as MessageEvent));
		source.onerror = () => settle(new Error('Conexion SSE del agente perdida'));
		signal.addEventListener(
			'abort',
			() => {
				void fetch(`${endpoint.replace(/\/$/, '')}/agent_execute/${jobId}`, { method: 'DELETE' });
				settle(new DOMException('Aborted', 'AbortError'));
			},
			{ once: true }
		);
	});
}
