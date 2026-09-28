export const APP_NAME = import.meta.env?.VITE_PUBLIC_APP_NAME || 'llama-ui';

export const INACTIVE_CONVERSATION = {
	MAX_STATES: 64,
	MAX_AGE_MS: 10 * 60 * 1000
} as const;
