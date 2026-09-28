#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Fix 2 completo: Emitir eventos colectados después del streaming del supervisor.
"""
import re

with open('D:/Programas/AI DeepLearning/llama servers/Llama Web UI/tools/agent_core.py', 'r', encoding='utf-8', errors='ignore') as f:
    content = f.read()

# Encontrar la sección de bus.emit para plan
pattern = r'(result = \{"steps": valid_steps.*?\})\s*\n\s*# FIX BUG 2: Emitir plan completo inmediatamente para que la UI lo reciba\s*\n\s*bus\.emit\(AgentEvent\("plan".*?\)\s*\n\s*bus\.emit\(AgentEvent\("supervisor".*?\)\s*\n'
match = re.search(pattern, content, re.DOTALL)

if match:
    old_code = match.group(0)
    new_code = match.group(1) + '''

        # Emitir eventos colectados durante el streaming del razonamiento del supervisor
        for evt in collected_events:
            bus.emit(AgentEvent(evt["type"], evt["payload"], evt.get("meta", {})))

        # FIX BUG 2: Emitir plan completo inmediatamente para que la UI lo reciba
        bus.emit(AgentEvent("plan", result))
        bus.emit(AgentEvent("supervisor", {"status": "planned", "steps": len(valid_steps)})
'''
    content = content.replace(old_code, new_code)
    with open('D:/Programas/AI DeepLearning/llama servers/Llama Web UI/tools/agent_core.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print('SUCCESS: Fix 2 completado - events emitted after plan')
else:
    print('Pattern not found - trying alternative approach')
    # Find the result line and emit lines
    idx = content.find('result = {"steps": valid_steps')
    if idx > 0:
        # Find next emit lines
        section = content[idx:idx+500]
        print(f'Found section at {idx}:')
        print(section[:400])
