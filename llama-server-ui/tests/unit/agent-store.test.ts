import { agentStore } from '$lib/stores/agentStore.svelte';
import { describe, expect, it } from 'vitest';

/**
 * El store de agentes ya NO es dueño de ningún stream: el panel recibe cada
 * frame del job por `applyChatAgencyEvent`, el reductor ÚNICO que alimenta el
 * chat que lanzó la tarea. El transporte SSE se prueba en
 * `tests/unit/agency-stream.test.ts`.
 */
describe('agentStore timeline scoping', () => {
	it('liga la timeline a la conversación que lanzó la ejecución', () => {
		agentStore.beginAgencyTimeline('conv-1');

		expect(agentStore.agencyConversationId).toBe('conv-1');
		expect(agentStore.isExecuting).toBe(true);
		expect(agentStore.agencyRuns).toEqual([]);

		// Otra ejecución en otra conversación sustituye la timeline activa: la
		// anterior deja de mostrarse (no se podía ver el mismo bloque en todos
		// los chats).
		agentStore.beginAgencyTimeline('conv-2');
		expect(agentStore.agencyConversationId).toBe('conv-2');
	});

	it('al cerrar la ejecución no queda ningún agente en curso', () => {
		agentStore.beginAgencyTimeline('conv-1');
		agentStore.applyChatAgencyEvent({
			job_id: 'job-1',
			payload: { agent: 'Analista', subtask: 'Investigar', task_id: 's1' },
			type: 'agent_start'
		});
		expect(agentStore.agencyRuns[0].status).toBe('running');

		agentStore.finishAgencyTimeline();

		expect(agentStore.isExecuting).toBe(false);
		expect(agentStore.agencyCurrentAgent).toBeNull();
		expect(agentStore.agencyRuns.every((run) => run.status === 'done')).toBe(true);
	});
});

describe('agentStore AGENCY timeline', () => {
	it('builds the plan, streams each agent into its own card and keeps the synthesis separate', () => {
		agentStore.resetAgencyTimeline();
		// `response` persiste entre ejecuciones (lo limpia el chat al abrir turno).
		const baseline = agentStore.response;

		agentStore.applyChatAgencyEvent({
			job_id: 'job-1',
			payload: {
				steps: [
					{ agent: 'Analista', id: 's1', subtask: 'Investigar' },
					{ agent: 'Redactor', depends_on: ['s1'], id: 's2', subtask: 'Redactar' }
				],
				task: 'Crea una API'
			},
			type: 'plan'
		});
		expect(agentStore.agencyPlan.map((step) => step.id)).toEqual(['s1', 's2']);
		expect(agentStore.agencyGoal).toBe('Crea una API');
		expect(agentStore.agencyPlan[1].agent_name).toBe('Redactor');

		agentStore.applyChatAgencyEvent({
			job_id: 'job-1',
			payload: { agent: 'Analista', subtask: 'Investigar', task_id: 's1' },
			type: 'agent_start'
		});
		agentStore.applyChatAgencyEvent({
			job_id: 'job-1',
			meta: { agent: 'Analista', role: 'specialist', task_id: 's1' },
			payload: { text: 'hallazgo' },
			type: 'text'
		});

		const run = agentStore.agencyRuns.find((item) => item.id === 's1');

		expect(run?.status).toBe('running');
		expect(run?.text).toBe('hallazgo');
		// El texto del subagente no contamina la respuesta final.
		expect(agentStore.response).toBe(baseline);

		agentStore.applyChatAgencyEvent({
			job_id: 'job-1',
			payload: { agent: 'Analista', success: true, task_id: 's1' },
			type: 'agent_end'
		});
		expect(agentStore.agencyRuns.find((item) => item.id === 's1')?.status).toBe('done');

		agentStore.applyChatAgencyEvent({
			job_id: 'job-1',
			meta: { role: 'supervisor' },
			payload: { text: 'síntesis final' },
			type: 'text'
		});
		expect(agentStore.agencySupervisor).toBe('síntesis final');
		expect(agentStore.response).toBe(`${baseline}síntesis final`);
	});

	it('does not finish the job when a single step fails', () => {
		agentStore.resetAgencyTimeline();
		agentStore.applyChatAgencyEvent({
			job_id: 'job-1',
			payload: { agent: 'Analista', subtask: 'Investigar', task_id: 's1' },
			type: 'agent_start'
		});

		agentStore.applyChatAgencyEvent({
			job_id: 'job-1',
			payload: { agent: 'Analista', error: 'boom' },
			type: 'error'
		});
		expect(agentStore.isExecuting).toBe(true);
		expect(agentStore.agencyRuns.find((item) => item.id === 's1')?.status).toBe('error');

		agentStore.applyChatAgencyEvent({ job_id: 'job-1', payload: {}, type: 'done' });
		expect(agentStore.isExecuting).toBe(false);
	});
});

describe('agentStore plan (eventos reales del backend)', () => {
	/** Pasos como los emite `pipeline_templates.build_plan`. */
	const planSteps = [
		{
			agent: 'Backend Architect',
			artifact: 'app/main.py',
			id: 'setup',
			phase: 'setup+core',
			phases: ['setup', 'core'],
			subtask: 'Andamiaje'
		},
		{
			agent: 'API Tester',
			artifact: 'tests/',
			depends_on: ['setup'],
			id: 'tests',
			phase: 'tests',
			subtask: 'Pruebas'
		}
	];

	it('mueve el estado de cada paso con step_start / agent_start / step_end', () => {
		agentStore.resetAgencyTimeline();

		agentStore.applyChatAgencyEvent({
			job_id: 'job-1',
			payload: { steps: planSteps },
			type: 'agency_selected'
		});

		expect(agentStore.agencyPlan.map((step) => step.phase)).toEqual(['setup+core', 'tests']);
		expect(agentStore.agencyPlan.map((step) => step.status)).toEqual(['pending', 'pending']);
		expect(agentStore.agencyPlan[0].artifact).toBe('app/main.py');
		expect(agentStore.agencyPlan[0].phases).toEqual(['setup', 'core']);
		expect(agentStore.agencyPlan[1].depends_on).toEqual(['setup']);

		// `step_start` previsualiza el paso por índice (1-based), sin abrirlo.
		agentStore.applyChatAgencyEvent({
			job_id: 'job-1',
			payload: { idx: 1, subtask: 'Andamiaje' },
			type: 'step_start'
		});
		expect(agentStore.agencyPlan[0].status).toBe('running');

		// `step_end` cierra la fila: antes no tenía case y el plan se quedaba
		// "pendiente" toda la ejecución.
		agentStore.applyChatAgencyEvent({
			job_id: 'job-1',
			payload: { agent: 'Backend Architect', task_id: 'setup' },
			type: 'step_end'
		});
		expect(agentStore.agencyPlan[0].status).toBe('done');

		// Un paso sin trabajo marca error, no "done".
		agentStore.applyChatAgencyEvent({
			job_id: 'job-1',
			payload: { agent: 'API Tester', error: 'sin artefacto', task_id: 'tests' },
			type: 'step_end'
		});
		expect(agentStore.agencyPlan[1].status).toBe('error');
	});

	it('conserva el trabajo hecho cuando el plan se replanifica', () => {
		agentStore.resetAgencyTimeline();
		agentStore.applyChatAgencyEvent({
			job_id: 'job-1',
			payload: { steps: planSteps },
			type: 'agency_selected'
		});
		agentStore.applyChatAgencyEvent({
			job_id: 'job-1',
			payload: { agent: 'Backend Architect', task_id: 'setup' },
			type: 'step_end'
		});

		// Recuperación: el backend reescribe el plan con el paso correctivo.
		agentStore.applyChatAgencyEvent({
			job_id: 'job-1',
			payload: {
				steps: [
					...planSteps,
					{ agent: 'API Tester', id: 'testsR1', subtask: '[CORRECCIÓN] Pruebas' }
				]
			},
			type: 'plan'
		});

		expect(agentStore.agencyPlan.map((step) => step.id)).toEqual(['setup', 'tests', 'testsR1']);
		expect(agentStore.agencyPlan[0].status).toBe('done');
		expect(agentStore.agencyPlan[2].status).toBe('pending');
	});

	it('muestra el plan incompleto y las herramientas denegadas', () => {
		agentStore.resetAgencyTimeline();
		agentStore.applyChatAgencyEvent({
			job_id: 'job-1',
			payload: { steps: planSteps },
			type: 'agency_selected'
		});
		agentStore.applyChatAgencyEvent({
			job_id: 'job-1',
			payload: { agent: 'API Tester', subtask: 'Pruebas', task_id: 'tests' },
			type: 'agent_start'
		});
		agentStore.applyChatAgencyEvent({
			job_id: 'job-1',
			meta: { agent: 'API Tester', role: 'specialist', task_id: 'tests' },
			payload: { agent: 'API Tester', name: 'kill_process', reason: 'perfil "testing"' },
			type: 'tool_denied'
		});

		expect(agentStore.agencyDenials).toEqual([
			{ agent: 'API Tester', name: 'kill_process', reason: 'perfil "testing"' }
		]);
		expect(agentStore.agencyRuns.find((run) => run.id === 'tests')?.denied).toEqual([
			'kill_process'
		]);

		// `incomplete` es un AVISO (no cierra el job) y este evento no tenía case
		// en ningún switch: el usuario no veía nada.
		agentStore.applyChatAgencyEvent({
			job_id: 'job-1',
			payload: { agent: 'API Tester', reason: 'paso sin artefacto' },
			type: 'incomplete'
		});

		expect(agentStore.agencyIncomplete).toBe('paso sin artefacto');
		expect(agentStore.agencyPlan[1].status).toBe('error');
		expect(agentStore.isExecuting).toBe(true);

		agentStore.resetAgencyTimeline('conv-1');
		expect(agentStore.agencyIncomplete).toBeNull();
		expect(agentStore.agencyDenials).toEqual([]);
		expect(agentStore.agencyPlan).toEqual([]);
	});
});
