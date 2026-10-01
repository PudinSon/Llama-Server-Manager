import { describe, expect, it } from 'vitest';
import { summarizeAssistantTimings } from '$lib/stores/chat/context-stats.svelte';
import { MessageRole, MessageType } from '$lib/enums';
import type { AgencyStepMeta, DatabaseMessage } from '$lib/types';

/**
 * El contador de contexto suma `timings` de cada mensaje asistente. Los turnos
 * del modo agente rompían esa suma: cada mensaje del job lleva el total
 * ACUMULADO del job (hasta ese turno), así que sumarlos multiplicaba el consumo
 * y el gauge marcaba un contexto que el chat nativo nunca tuvo. Con la tarea
 * del trace (7 pasos) el contador quedaba ~7x por encima.
 */

const agencyStep: AgencyStepMeta = { label: 'Agente Código', role: 'specialist' };

let seq = 0;

function message(role: MessageRole, timings?: DatabaseMessage['timings']): DatabaseMessage {
	return {
		children: [],
		content: '',
		convId: 'conv-1',
		id: `m${seq++}`,
		parent: null,
		role,
		timestamp: 0,
		type: MessageType.TEXT,
		...(timings ? { timings } : {})
	} as DatabaseMessage;
}

/** Turno de chat nativo: `timings` del turno, no acumulados. */
function chatTurn(prompt: number, generated: number, cache = 0): DatabaseMessage {
	return message(MessageRole.ASSISTANT, {
		cache_n: cache,
		predicted_n: generated,
		prompt_n: prompt
	});
}

/**
 * Turno de un job agencial: `prompt`/`generated` son los totales ACUMULADOS del
 * job hasta ese turno, que es exactamente lo que persiste el chat.
 */
function agentTurn(prompt: number, generated: number, cache = 0): DatabaseMessage {
	const turn = message(MessageRole.ASSISTANT, {
		cache_n: cache,
		predicted_n: generated,
		prompt_n: prompt
	});

	turn.agencyStep = agencyStep;

	return turn;
}

describe('summarizeAssistantTimings · modo agente', () => {
	it('deja fuera del contador del chat los turnos agenciales', () => {
		const summary = summarizeAssistantTimings([
			chatTurn(1000, 100),
			agentTurn(2000, 200),
			agentTurn(5000, 900)
		]);

		expect(summary.read).toBe(1000);
		expect(summary.output).toBe(100);
		// El gauge por turno tampoco puede leer el último total del job.
		expect(summary.lastTimings?.prompt_n).toBe(1000);
		expect(summary.lastTimings?.predicted_n).toBe(100);
	});

	it('cuenta el consumo de un job con totales acumulados una sola vez', () => {
		const summary = summarizeAssistantTimings([
			agentTurn(2000, 200),
			agentTurn(5000, 900),
			agentTurn(6000, 1000)
		]);

		// El consumo real del job son sus totales finales, no la suma de los
		// parciales (2000+5000+6000 = 13000).
		expect(summary.agentRead).toBe(6000);
		expect(summary.agentOutput).toBe(1000);
		expect(summary.read).toBe(0);
		expect(summary.output).toBe(0);
		expect(summary.lastTimings).toBeUndefined();
	});

	it('suma varios jobs separados por chat nativo', () => {
		const summary = summarizeAssistantTimings([
			chatTurn(100, 10),
			agentTurn(1000, 100),
			agentTurn(2000, 300),
			chatTurn(150, 20),
			agentTurn(3000, 400),
			agentTurn(3500, 500)
		]);

		expect(summary.agentRead).toBe(2000 + 3500);
		expect(summary.agentOutput).toBe(300 + 500);
		// El chat nativo sigue contando solo sus propios turnos y el gauge por
		// turno mira el último turno nativo.
		expect(summary.read).toBe(250);
		expect(summary.output).toBe(30);
		expect(summary.lastTimings?.prompt_n).toBe(150);
	});

	it('detecta un job nuevo cuando su cuenta arranca más baja que la del anterior', () => {
		const summary = summarizeAssistantTimings([agentTurn(5000, 900), agentTurn(1200, 150)]);

		expect(summary.agentRead).toBe(5000 + 1200);
		expect(summary.agentOutput).toBe(900 + 150);
	});

	it('acumula los tokens reutilizados de la KV cache sin contarlos como contexto', () => {
		const summary = summarizeAssistantTimings([agentTurn(2000, 100, 1500), agentTurn(4000, 300, 3600)]);

		expect(summary.agentRead).toBe(4000);
		expect(summary.agentCacheTotal).toBe(3600);
		expect(summary.cacheTotal).toBe(0);
	});

	it('ignora mensajes sin timings y turnos que no son del asistente', () => {
		const summary = summarizeAssistantTimings([
			message(MessageRole.USER),
			message(MessageRole.ASSISTANT),
			message(MessageRole.TOOL)
		]);

		expect(summary.read).toBe(0);
		expect(summary.output).toBe(0);
		expect(summary.agentRead).toBe(0);
		expect(summary.lastTimings).toBeUndefined();
	});
});
