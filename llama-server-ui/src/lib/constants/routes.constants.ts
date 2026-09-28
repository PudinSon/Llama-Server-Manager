/** Query params the chat routes read from the URL. */
export const URL_PARAMS = {
	/** Load the selected model instead of waiting for the first message. */
	LOAD: 'load',
	/** Model to select. */
	MODEL: 'model',
	/** Start a new chat. */
	NEW_CHAT: 'new_chat',
	/** Prompt to send on arrival. */
	QUERY: 'q'
} as const;

export { SETTINGS_SECTION_SLUGS } from './settings.constants';

export const ROUTES = {
	/** Automatización 24/7 (servicio independiente en el puerto 15001). */
	AUTOMATION: '#/automation',
	/** Chat base — for dynamic chat URLs use RouterService. */
	CHAT: '#/chat',
	/** MCP servers. */
	MCP_SERVERS: '#/mcp-servers',
	/** Legacy new-chat URL kept only for incoming-link compatibility. */
	NEW_CHAT: '#/',
	/** Search — mobile-only full-page conversation search. */
	SEARCH: '#/search',
	/** Settings base — for dynamic settings URLs use RouterService. */
	SETTINGS: '#/settings',
	/** Exit destination for the settings view (fallback when no referrer). */
	SETTINGS_EXIT: '#/',
	/** Root — start of the app. */
	START: '#/'
} as const;
