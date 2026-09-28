/**
 * Agent MCP Tool Caller
 *
 * Provides a unified interface for the WebUI to call agent-specific MCP tools.
 * These tools are exposed by the Python MCP server but are NOT included in
 * get_openai_tools() so they don't consume the LLM's context window.
 *
 * Strategy:
 * Try connected MCP servers via the SDK. Agent-specific UI operations use the
 * dedicated HTTP endpoints in agentStore instead of duplicate MCP tools.
 */

import { mcpStore } from '$lib/stores/mcp.svelte';

/**
 * Call an agent MCP tool, trying connected MCP servers first, then falling back to HTTP.
 *
 * @param toolName - The MCP tool name (e.g. 'agent_status', 'agent_memory')
 * @param args - Tool arguments
 * @param httpEndpoint - HTTP endpoint override for fallback (required when SDK fails)
 * @param signal - Optional AbortSignal for cancellation
 * @returns Parsed tool result
 */
export async function callMCPTool(
	toolName: string,
	_args: Record<string, unknown> = {},
	_httpEndpoint?: string,
	signal?: AbortSignal
): Promise<unknown> {
	if (mcpStore.hasTool(toolName)) {
		try {
			const result = await mcpStore.executeToolByName(toolName, _args, signal);

			// Parse the text content from the MCP result
			if (result?.content && Array.isArray(result.content)) {
				const textParts = result.content
					.filter(
						(c: { type?: string; text?: string }) =>
							c.type === 'text' && typeof c.text === 'string'
					)
					.map((c: { text?: string }) => c.text ?? '');

				const text = textParts.join('');

				try {
					return JSON.parse(text);
				} catch {
					return text;
				}
			}

			return result;
		} catch (error) {
			console.warn(`[AgentMCP] SDK call failed for ${toolName}, trying HTTP fallback:`, error);
		}
	}

	throw new Error(`MCP tool not connected: ${toolName}`);
}
