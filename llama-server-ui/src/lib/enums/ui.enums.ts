export enum ColorMode {
	DARK = 'dark',
	LIGHT = 'light',
	SYSTEM = 'system'
}

export enum TooltipSide {
	BOTTOM = 'bottom',
	LEFT = 'left',
	RIGHT = 'right',
	TOP = 'top'
}

export enum ScrollCarouselVariant {
	CENTER = 'center',
	TOP = 'top'
}

export enum SidebarAction {
	NEW_CHAT = 'new-chat'
}

/**
 * MCP prompt display variant
 */
export enum McpPromptVariant {
	ATTACHMENT = 'attachment',
	MESSAGE = 'message'
}

/**
 * URL prefixes for protocol detection
 */
export enum UrlProtocol {
	DATA = 'data:',
	FILE = 'file:',
	HTTP = 'http:',
	HTTPS = 'https:',
	WEBSOCKET = 'ws:',
	WEBSOCKET_SECURE = 'wss:'
}

export enum HtmlInputType {
	FILE = 'file'
}

/**
 * Alert level that drives the context gauge dial color.
 */
export enum ColorLevel {
	CRITICAL = 'critical',
	NEUTRAL = 'neutral',
	OK = 'ok',
	WARNING = 'warning'
}
