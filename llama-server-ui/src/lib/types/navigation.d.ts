import type { Component } from 'svelte';
import type { SidebarAction } from '$lib/enums';

/**
 * A single clickable action in the desktop sidebar icon strip.
 */
export interface DesktopIconStripItem {
	icon: Component;
	tooltip: string;
	route?: string;
	action?: SidebarAction;
	activeRouteId?: string;
	activeRoutePrefix?: string;
	activeUrlIncludes?: string;
	keys?: string[];
	onClick?: string;
}
