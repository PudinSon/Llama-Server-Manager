# tools/skill_learner.py
# Módulo para el aprendizaje automático: reflexión, almacenamiento de lecciones y generación de skills.

import json
import time
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime

logger = logging.getLogger("skill_learner")

try:
    from . import runtime
except:
    runtime = None

# ============================================================================
# CONSTANTES
# ============================================================================

# Umbral de uso de un skill para considerarlo "probado"
PROMOTION_THRESHOLD = 3
# Número mínimo de lecciones similares para generar un skill
PATTERN_THRESHOLD = 2

# ============================================================================
# CLASE SKILLLEARNER
# ============================================================================

class SkillLearner:
    """
    Aprendizaje automático para el agente.
    Analiza resultados, genera reflexiones, guarda lecciones y promueve skills.
    """
    def __init__(self, workspace_path: str, skill_manager):
        self.workspace_path = workspace_path
        self.skill_manager = skill_manager
        skills_root = (
            Path(runtime.get_data_dir()) / "skills"
            if runtime is not None
            else Path(workspace_path) / ".skills"
        )
        self.learnings_file = skills_root / "LEARNINGS.md"
        self.learnings_file.parent.mkdir(parents=True, exist_ok=True)
        self.learnings = self._load_learnings()

    def _load_learnings(self) -> List[Dict]:
        """Carga las lecciones almacenadas desde LEARNINGS.md."""
        if not self.learnings_file.exists():
            return []
        try:
            content = self.learnings_file.read_text(encoding='utf-8')
            # Parsear el archivo Markdown con secciones
            # Formato: ## Lección YYYY-MM-DD HH:MM:SS
            lessons = []
            current = {}
            for line in content.split('\n'):
                if line.startswith('## Lección '):
                    if current:
                        lessons.append(current)
                    timestamp = line.replace('## Lección ', '').strip()
                    current = {"timestamp": timestamp, "task": "", "outcome": "", "reflection": "", "tool_calls": []}
                elif line.startswith('- **Tarea**: '):
                    current["task"] = line.replace('- **Tarea**: ', '').strip()
                elif line.startswith('- **Resultado**: '):
                    current["outcome"] = line.replace('- **Resultado**: ', '').strip()
                elif line.startswith('- **Reflexión**: '):
                    current["reflection"] = line.replace('- **Reflexión**: ', '').strip()
                elif line.startswith('- **Herramientas**: '):
                    try:
                        current["tool_calls"] = json.loads(line.replace('- **Herramientas**: ', '').strip())
                    except:
                        pass
            if current:
                lessons.append(current)
            return lessons
        except Exception as e:
            logger.error(f"Error cargando learnings: {e}")
            return []

    def _save_learnings(self):
        """Guarda las lecciones en LEARNINGS.md."""
        content = "# Registro de Aprendizaje del Agente\n\n"
        for lesson in self.learnings:
            content += f"## Lección {lesson.get('timestamp', '')}\n"
            content += f"- **Tarea**: {lesson.get('task', '')}\n"
            content += f"- **Resultado**: {lesson.get('outcome', '')}\n"
            content += f"- **Reflexión**: {lesson.get('reflection', '')}\n"
            if lesson.get('tool_calls'):
                content += f"- **Herramientas**: {json.dumps(lesson['tool_calls'], ensure_ascii=False)}\n"
            content += "\n"
        self.learnings_file.write_text(content, encoding='utf-8')

    def analyze_outcome(self, task: str, attempt_log: List[Dict], outcome: str) -> Dict:
        """
        Analiza el resultado de una tarea y genera una reflexión.
        attempt_log: lista de acciones realizadas con {tool, arguments, result, success}.
        outcome: "success" o "failure".
        """
        reflection = {
            "task": task,
            "outcome": outcome,
            "timestamp": datetime.now().isoformat(),
            "tool_calls": attempt_log,
            "reflection": ""
        }

        # Usar el LLM para generar una reflexión si falló
        if outcome == "failure":
            reflection["reflection"] = self._generate_reflection(task, attempt_log)
        else:
            reflection["reflection"] = f"Tarea completada con éxito usando {len(attempt_log)} herramientas."

        return reflection

    def _generate_reflection(self, task: str, attempt_log: List[Dict]) -> str:
        """
        Usa el LLM (chat_completion) para generar una reflexión sobre el fracaso.
        """
        try:
            from .dev_agent import chat_completion
            # Construir prompt
            log_text = "\n".join([
                f"- {entry.get('tool', '')}({json.dumps(entry.get('arguments', {}), ensure_ascii=False)}) → {entry.get('result', '')[:100]}"
                for entry in attempt_log
            ])
            prompt = f"""
La siguiente tarea falló:
Tarea: {task}

Registro de intentos:
{log_text}

Analiza el fracaso y escribe una reflexión breve (1-2 frases) que explique:
1. Qué salió mal.
2. Qué podría hacerse diferente la próxima vez.
3. Qué herramienta o paso adicional podría resolver el problema.

Reflexión:
"""
            messages = [{"role": "user", "content": prompt}]
            response = chat_completion(messages)
            # Limpiar respuesta
            if response.startswith("Error"):
                return "No se pudo generar reflexión. Error en LLM."
            return response.strip()
        except Exception as e:
            logger.error(f"Error generando reflexión: {e}")
            return f"Error generando reflexión: {e}"

    def learn(self, task: str, attempt_log: List[Dict], outcome: str):
        """
        Punto de entrada para el aprendizaje.
        Analiza, guarda la lección y promueve skills si procede.
        """
        reflection = self.analyze_outcome(task, attempt_log, outcome)
        self.learnings.append(reflection)
        self._save_learnings()
        logger.info(f"Lección guardada para tarea '{task[:30]}...' ({outcome})")

        # Si falló, generar skill de corrección si hay patrón
        if outcome == "failure":
            self._check_pattern_and_generate_skill(task, reflection)

    def _skill_already_exists(self, skill_name: str) -> bool:
        """Verifica si ya existe un skill con ese nombre en cualquier scope."""
        if self.skill_manager.get_skill(skill_name):
            return True
        auto_skills = self.skill_manager.list_skills(include_auto=True)
        for s in auto_skills:
            if s["name"] == skill_name:
                return True
        return False

    def _check_pattern_and_generate_skill(self, task: str, reflection: Dict):
        """
        Busca lecciones similares y, si se repite un patrón, genera un skill.
        """
        recent = self.learnings[-10:]
        similar = [l for l in recent if l.get("task") == task or l.get("task", "").strip() == task.strip()]

        if len(similar) >= PATTERN_THRESHOLD:
            skill_name = f"fix-{task.replace(' ', '-')[:30].lower()}"
            if self._skill_already_exists(skill_name):
                logger.info(f"Skill '{skill_name}' ya existe. No se regenera.")
                return
            logger.info(f"Patrón detectado: {len(similar)} lecciones similares para '{task}'. Generando skill...")
            description = f"Skill auto-generado para resolver: {task}"
            # Usar el LLM para generar los pasos
            steps = self._generate_skill_steps(task, similar)
            if steps:
                self.skill_manager.create_skill(
                    name=skill_name,
                    description=description,
                    steps=steps,
                    tags=["auto-generated", "fix"],
                    auto_generated=True
                )
                logger.info(f"Skill '{skill_name}' generado en auto-generated/")

    def _generate_skill_steps(self, task: str, similar_lessons: List[Dict]) -> List[Dict]:
        """
        Usa el LLM para generar una secuencia de pasos a partir de lecciones similares.
        """
        try:
            from .dev_agent import chat_completion
            # Construir contexto
            context = "\n".join([
                f"- Intento {i+1}: {l.get('reflection', '')}"
                for i, l in enumerate(similar_lessons)
            ])
            prompt = f"""
La siguiente tarea ha fallado varias veces:
Tarea: {task}

Lecciones de intentos anteriores:
{context}

Genera una secuencia de pasos (en formato JSON) para resolver esta tarea.
Cada paso debe tener: tool (nombre de la herramienta), arguments (diccionario), description (texto).
Ejemplo:
[
  {{"tool": "list_directory", "arguments": {{"path": "."}}, "description": "Listar el directorio actual"}},
  {{"tool": "create_directory", "arguments": {{"path": "nuevo"}}, "description": "Crear carpeta nuevo"}}
]

Responde SOLO con el JSON válido, sin comentarios.
"""
            messages = [{"role": "user", "content": prompt}]
            response = chat_completion(messages)
            if response.startswith("Error"):
                logger.error(f"Error generando steps: {response}")
                return []
            # Extraer JSON
            import re
            json_match = re.search(r'\[.*\]', response, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(0))
            else:
                logger.warning("No se encontró JSON en la respuesta.")
                return []
        except Exception as e:
            logger.error(f"Error generando steps de skill: {e}")
            return []