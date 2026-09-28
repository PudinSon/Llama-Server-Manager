import {
	collectScheduledChats,
	type ScheduledChatDetail,
	type ScheduledChatSummary
} from '$lib/services/scheduled-chats.service';
import { describe, expect, it, vi } from 'vitest';

/**
 * El scheduler del backend ejecuta las tareas programadas aunque la webview
 * esté cerrada y guarda cada ejecución como conversación. Antes de importarlas
 * al historial nativo hay que decidir cuáles son nuevas y descartar las que no
 * se pueden leer, sin que una fallida bloquee a las demás.
 */

const CHAT_ID = 'scheduled-task_1-20260922-083000';

function summary(id: string): ScheduledChatSummary {
	return {
		created_at: '2026-09-22T08:30:00',
		description: 'informe diario',
		id,
		message_count: 3,
		name: `⏰ informe diario · ${id}`,
		status: 'completed',
		task_id: 'task_1'
	};
}

function detail(id: string): ScheduledChatDetail {
	return {
		conv: { currNode: `${id}-assistant`, id, lastModified: 1, name: 'x' },
		id,
		messages: [
			{
				children: [],
				content: 'informe diario',
				convId: id,
				id: `${id}-user`,
				parent: null,
				role: 'user',
				timestamp: 1,
				toolCalls: '',
				type: 'text'
			}
		]
	} as unknown as ScheduledChatDetail;
}

describe('collectScheduledChats', () => {
	it('descarga solo lo que no se ha importado todavía', async () => {
		const fetchDetail = vi.fn(async (id: string) => detail(id));

		const { ids, records } = await collectScheduledChats(
			[summary(CHAT_ID), summary('scheduled-otra')],
			new Set([CHAT_ID]),
			fetchDetail
		);

		expect(fetchDetail).toHaveBeenCalledTimes(1);
		expect(fetchDetail).toHaveBeenCalledWith('scheduled-otra');
		expect(ids).toEqual(['scheduled-otra']);
		expect(records).toHaveLength(1);
		expect(records[0].conv.id).toBe('scheduled-otra');
		expect(records[0].messages[0].content).toBe('informe diario');
	});

	it('ignora los chats ilegibles sin frenar al resto', async () => {
		const fetchDetail = vi.fn(async (id: string) => (id === 'roto' ? null : detail(id)));

		const { ids, records } = await collectScheduledChats(
			[summary('roto'), summary(CHAT_ID)],
			new Set(),
			fetchDetail
		);

		expect(ids).toEqual([CHAT_ID]);
		expect(records.map((record) => record.conv.id)).toEqual([CHAT_ID]);
	});

	it('no hace nada cuando no hay resúmenes', async () => {
		const fetchDetail = vi.fn(async (id: string) => detail(id));

		expect(await collectScheduledChats([], new Set(), fetchDetail)).toEqual({
			ids: [],
			records: []
		});
		expect(fetchDetail).not.toHaveBeenCalled();
	});
});
