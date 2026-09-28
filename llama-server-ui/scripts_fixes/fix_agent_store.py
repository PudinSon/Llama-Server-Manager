import sys

# Read the file
with open('C:/msys64/home/PudinSon/llama-cpp-dev/llama.cpp/tools/ui/src/lib/stores/agentStore.svelte.ts', 'r') as f:
    content = f.read()

# Define the old and new code
old_code = '''		case 'agent_end': {
			const agent = String(valueObj.agent ?? agentName ?? 'agente');
			const success = valueObj.success !== false;
			const taskId = String(valueObj.task_id ?? '');
			const index = agencyRuns.findIndex(
				(run) => (taskId && run.id === taskId) || (!taskId && run.agent === agent && run.status === 'running')
			);
			if (index >= 0) {
				const runs = [...agencyRuns];
				runs[index] = {
					...runs[index],
					status: success ? 'done' : 'error',
					error: success ? undefined : String(valueObj.error ?? 'Error del agente'),
					durationMs: Date.now() - runs[index].startedAt
				};
				agencyRuns = runs;
			}
			agencyCurrentAgent = null;
			break;
		}'''

new_code = '''	case 'agent_end': {
			const agent = String(valueObj.agent ?? agentName ?? 'agente');
			const success = valueObj.success !== false;
			const taskId = String(valueObj.task_id ?? '');
			const index = agencyRuns.findIndex(
				(run) => (taskId && run.id === taskId) || (!taskId && run.agent === agent && run.status === 'running')
			);
			if (index >= 0) {
				const runs = [...agencyRuns];
				runs[index] = {
					...runs[index],
					status: success ? 'done' : 'error',
					error: success ? undefined : String(valueObj.error ?? 'Error del agente'),
					durationMs: Date.now() - runs[index].startedAt
				};
				agencyRuns = runs;
			}
			// FIX Bug silencioso: verificar si hay más agentes ejecutándose antes de cerrar stream
			const activeRuns = agencyRuns.filter((r) => r.status === 'running');
			if (activeRuns.length === 0) {
				agencyCurrentAgent = null;
			} else {
				// Mantener stream visible para el siguiente agente
				agencyCurrentAgent = activeRuns[0].agent;
			}
			break;
		}'''

# Replace and write back
if old_code in content:
    content = content.replace(old_code, new_code)
    with open('C:/msys64/home/PudinSon/llama-cpp-dev/llama.cpp/tools/ui/src/lib/stores/agentStore.svelte.ts', 'w') as f:
        f.write(content)
    print('Success: Bug 3 fixed - agent_end now checks for active runs')
elif 'activeRuns' in content:
    print('Already fixed: Bug 3 correction already present')
else:
    print('Error: Could not find old_code pattern to replace')
    # Debug: show the relevant section
    import re
    match = re.search(r'case .agent_end.*?break;\s*\}', content, re.DOTALL)
    if match:
        print('Found pattern:', repr(match.group()[:200]))
