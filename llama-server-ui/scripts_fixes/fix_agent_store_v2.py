import re

# Read the file
with open('C:/msys64/home/PudinSon/llama-cpp-dev/llama.cpp/tools/ui/src/lib/stores/agentStore.svelte.ts', 'r') as f:
    content = f.read()

# Fix 3: Replace the specific line that causes bug silencioso
# Find the exact pattern with flexible whitespace
old_pattern = r'(case .agent_end.:\s*\{[^}]*agencyRuns = runs;\s*\}\s*)agencyCurrentAgent = null;'
new_code = r'''\1            // FIX Bug silencioso: verificar si hay más agentes ejecutándose antes de cerrar stream
            const activeRuns = agencyRuns.filter((r) => r.status === 'running');
            if (activeRuns.length === 0) {
                agencyCurrentAgent = null;
            } else {
                // Mantener stream visible para el siguiente agente
                agencyCurrentAgent = activeRuns[0].agent;
            }'''

match = re.search(old_pattern, content, re.DOTALL)
if match:
    # Use a simpler approach - just find and replace the single line
    old_line = 'agencyCurrentAgent = null;'
    new_line = '''            // FIX Bug silencioso: verificar si hay más agentes ejecutándose antes de cerrar stream
            const activeRuns = agencyRuns.filter((r) => r.status === 'running');
            if (activeRuns.length === 0) {
                agencyCurrentAgent = null;
            } else {
                // Mantener stream visible para el siguiente agente
                agencyCurrentAgent = activeRuns[0].agent;
            }'''
    
    if old_line in content:
        content = content.replace(old_line, new_line, 1)
        print('Fix 3 applied: replaced agencyCurrentAgent = null with active runs check')
    else:
        print('Fix 3 partial: line not found as expected')
else:
    print('Fix 3 - pattern not found, trying line-based approach')

# Write back
with open('C:/msys64/home/PudinSon/llama-cpp-dev/llama.cpp/tools/ui/src/lib/stores/agentStore.svelte.ts', 'w') as f:
    f.write(content)

print('Agent store fixed.')
