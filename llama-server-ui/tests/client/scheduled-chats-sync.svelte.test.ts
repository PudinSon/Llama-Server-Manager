import { MessageRole, MessageType } from '$lib/enums';
import { DatabaseService } from '$lib/services/database.service';
import { ScheduledChatsService } from '$lib/services/scheduled-chats.service';
import { afterEach, describe, expect, it, vi } from 'vitest';

/**
 * El scheduler del backend ejecuta las tareas programadas aunque la webview
 * esté cerrada y guarda cada ejecución como conversación. Este servicio es el
 * puente que las publica en el historial nativo (IndexedDB), así que el test
 * comprueba el recorrido completo: endpoints del backend → import → historial.
 */

const CHAT_ID = 'scheduled-task_1-20260922-083000';

function makeChatRecord() {
	const base = Date.parse('2026-09-22T08:30:00');

	return {
		conv: {
			currNode: `${CHAT_ID}-assistant`,
			id: CHAT_ID,
			lastModified: base + 2,
			name: '⏰ informe diario · 22/09 08:30'
		},
		created_at: new Date(base + 2).toISOString(),
		id: CHAT_ID,
		messages: [
			{
				children: [`${CHAT_ID}-user`],
				content: '',
				convId: CHAT_ID,
				id: `${CHAT_ID}-root`,
				parent: null,
				role: MessageRole.SYSTEM,
				timestamp: base,
				toolCalls: '',
				type: MessageType.ROOT
			},
			{
				children: [`${CHAT_ID}-assistant`],
				content: 'informe diario',
				convId: CHAT_ID,
				id: `${CHAT_ID}-user`,
				parent: `${CHAT_ID}-root`,
				role: MessageRole.USER,
				timestamp: base + 1,
				toolCalls: '',
				type: MessageType.TEXT
			},
			{
				children: [],
				content: 'Total: 35 entradas revisadas',
				convId: CHAT_ID,
				id: `${CHAT_ID}-assistant`,
				parent: `${CHAT_ID}-user`,
				role: MessageRole.ASSISTANT,
				timestamp: base + 2,
				toolCalls: '',
				type: MessageType.TEXT
			}
		]
	};
}

afterEach(async () => {
	vi.unstubAllGlobals();
	localStorage.clear();

	const conversations = await DatabaseService.getAllConversations();

	await DatabaseService.bulkDeleteConversations(conversations.map((conversation) => conversation.id));
});

describe('ScheduledChatsService.sync', () => {
	it('publica en el historial nativo los chats generados por las tareas programadas', async () => {
		const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);

			if (url.includes('/daemon_chats/')) {
				return new Response(JSON.stringify({ chat: makeChatRecord(), success: true }), {
					status: 200
				});
			}

			if (url.includes('/daemon_chats')) {
				return new Response(
					JSON.stringify({
						chats: [
							{
								created_at: new Date().toISOString(),
								description: 'informe diario',
								id: CHAT_ID,
								message_count: 3,
								name: '⏰ informe diario · 22/09 08:30',
								status: 'completed',
								task_id: 'task_1'
							}
						],
						success: true
					}),
					{ status: 200 }
				);
			}

			throw new Error(`Petición inesperada: ${url}`);
		});

		vi.stubGlobal('fetch', fetchMock);

		expect(await ScheduledChatsService.sync()).toBe(1);

		const conversation = await DatabaseService.getConversation(CHAT_ID);

		expect(conversation?.name).toContain('informe diario');
		expect(conversation?.currNode).toBe(`${CHAT_ID}-assistant`);

		const messages = await DatabaseService.getConversationMessages(CHAT_ID);

		expect(messages.map((message) => message.role)).toEqual([
			MessageRole.SYSTEM,
			MessageRole.USER,
			MessageRole.ASSISTANT
		]);
		expect(messages[2].content).toContain('35 entradas revisadas');

		// Segunda pasada: ya está en el historial, no se vuelve a pedir ni se duplica.
		const calls = fetchMock.mock.calls.length;

		expect(await ScheduledChatsService.sync()).toBe(0);
		expect(fetchMock.mock.calls.length).toBe(calls);
		expect(
			(await DatabaseService.getAllConversations()).filter(
				(item) => item.id === CHAT_ID
			)
		).toHaveLength(1);
	});

	it('no rompe el arranque si el backend no responde', async () => {
		vi.stubGlobal(
			'fetch',
			vi.fn(async () => {
				throw new Error('conexión rechazada');
			})
		);

		expect(await ScheduledChatsService.sync()).toBe(0);
	});
});
