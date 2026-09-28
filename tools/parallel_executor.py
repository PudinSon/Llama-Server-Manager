# tools/parallel_executor.py
# Módulo para ejecución paralela de subagentes.
# Permite ejecutar múltiples agentes en paralelo usando ThreadPoolExecutor.

import concurrent.futures
import threading
import time
import logging
import json
from typing import Dict, List, Optional, Any, Callable
from pathlib import Path

logger = logging.getLogger("parallel_executor")

# ============================================================================
# CLASE PARALLELEXECUTOR
# ============================================================================

class ParallelExecutor:
    """
    Ejecuta múltiples tareas de agentes en paralelo usando ThreadPoolExecutor.
    """
    def __init__(self, max_workers: Optional[int] = None,
                 cancel_event: Optional[threading.Event] = None):
        """
        Inicializa el ejecutor paralelo.
        max_workers: Número máximo de hilos. Por defecto, min(32, os.cpu_count() + 4).
        """
        import os
        if max_workers is None:
            cpu_count = os.cpu_count() or 4
            self.max_workers = min(32, cpu_count + 4)
        else:
            self.max_workers = max_workers
        self._results = {}
        self._errors = {}
        self._lock = threading.Lock()
        self.cancel_event = cancel_event

    def execute_wave(self, tasks: List[Dict], context: Dict = None,
                     agent_factory: Callable = None,
                     timeout: int = 300,
                     cancel_event: Optional[threading.Event] = None,
                     event_bus: Any = None) -> List[Dict]:
        """
        Ejecuta una lista de tareas en paralelo.
        
        tasks: Lista de tareas, cada una con:
            - id: Identificador único
            - subtask: Descripción de la subtarea
            - agent_name: Nombre del agente a usar (opcional)
            - context: Contexto específico para esta tarea (opcional)
        context: Contexto compartido para todas las tareas.
        agent_factory: Función que crea un agente para cada tarea.
        timeout: Tiempo máximo de ejecución por tarea.
        
        Retorna: Lista de resultados con el mismo orden que tasks.
        """
        if not tasks:
            return []

        cancel_event = cancel_event or self.cancel_event

        results = [None] * len(tasks)
        errors = [None] * len(tasks)
        active_agents = []

        def _run_task(index, task):
            try:
                if cancel_event is not None and cancel_event.is_set():
                    return {
                        "task_id": task.get("id", f"task_{index}"),
                        "success": False,
                        "cancelled": True,
                        "error": "Cancelado por usuario",
                        "agent_name": task.get("agent_name", "General"),
                    }
                # Crear contexto específico para esta tarea
                task_context = {
                    "task_id": task.get("id", f"task_{index}"),
                    "subtask": task.get("subtask", ""),
                    "agent_name": task.get("agent_name", "General"),
                    "shared_context": context or {},
                    "task_specific": task.get("context", {})
                }
                
                # Si se proporciona una fábrica de agentes, usarla
                if agent_factory:
                    agent = agent_factory(task_context)
                else:
                    # Crear agente por defecto.
                    # `Agent.__init__` no acepta `dynamic_tools`: el kwarg
                    # lanzaba TypeError en cuanto alguien usaba esta rama sin
                    # fábrica de agentes.
                    from .agent_core import Agent, LLMClient
                    agent = Agent(
                        name=task_context["agent_name"],
                        instructions="Eres un asistente útil. Completa la tarea asignada.",
                        llm_client=LLMClient(),
                        max_iterations=10,
                    )

                with self._lock:
                    active_agents.append(agent)
                if cancel_event is not None and cancel_event.is_set():
                    return {
                        "task_id": task_context["task_id"],
                        "success": False,
                        "cancelled": True,
                        "error": "Cancelado por usuario",
                        "agent_name": task_context["agent_name"],
                    }
                
                # Ejecutar la tarea
                logger.info(f"🔧 Subagente {task_context['task_id']} iniciando: {task_context['subtask'][:50]}...")
                if event_bus is not None:
                    from .agent_core import AgentEvent, EventBus
                    event_bus.emit(AgentEvent("agent_start", {
                        "task_id": task_context["task_id"],
                        "agent": task_context["agent_name"],
                        "subtask": task_context["subtask"],
                    }))
                    scoped_bus = EventBus()
                    def bridge(event):
                        event_bus.emit(AgentEvent(event.type, event.payload, {
                            **event.meta,
                            "agent": task_context["agent_name"],
                            "task_id": task_context["task_id"],
                            "role": "specialist",
                        }))
                    scoped_bus.on(bridge)
                    stream_context = task_context["shared_context"]
                    if not isinstance(stream_context, str):
                        stream_context = json.dumps(stream_context, ensure_ascii=False)
                    result = agent.run_stream(
                        task_context["subtask"],
                        context=stream_context,
                        bus=scoped_bus,
                        reasoning=True,
                    )
                    scoped_bus.off(bridge)
                else:
                    run_context = task_context["shared_context"]
                    if not isinstance(run_context, str):
                        run_context = json.dumps(run_context, ensure_ascii=False)
                    result = agent.run(task_context["subtask"], context=run_context)
                cancelled = (
                    cancel_event is not None and cancel_event.is_set()
                ) or str(result).lower().startswith("cancelado")
                
                # Registrar resultado
                result_entry = {
                        "task_id": task_context["task_id"],
                        "success": not cancelled,
                        "result": result,
                        "agent_name": task_context["agent_name"]
                }
                if cancelled:
                    result_entry["cancelled"] = True
                if event_bus is not None:
                    from .agent_core import AgentEvent
                    event_bus.emit(AgentEvent("agent_end", {
                        "task_id": task_context["task_id"],
                        "agent": task_context["agent_name"],
                        "success": not cancelled,
                    }))
                with self._lock:
                    results[index] = result_entry
                return results[index]
                
            except Exception as e:
                logger.error(f"❌ Subagente {task.get('id', 'unknown')} falló: {e}")
                with self._lock:
                    errors[index] = {
                        "task_id": task.get("id", f"task_{index}"),
                        "success": False,
                        "error": str(e),
                        "agent_name": task.get("agent_name", "General")
                    }
                return errors[index]
            finally:
                with self._lock:
                    if 'agent' in locals() and agent in active_agents:
                        active_agents.remove(agent)

        # Ejecución SECUENCIAL: un subagente a la vez.
        #
        # Antes se lanzaba un ThreadPoolExecutor con todos los subagentes a la
        # vez, lo que provocaba N llamadas simultáneas al modelo (y picos de RAM
        # al cargar varios agentes). Ahora se ejecuta de uno en uno, pero se
        # mantiene un hilo supervisor por tarea para conservar la cancelación
        # inmediata y el timeout por tarea (misma firma y mismos resultados).
        for _index, _task in enumerate(tasks):
            if cancel_event is not None and cancel_event.is_set():
                errors[_index] = {
                    "task_id": _task.get("id", f"task_{_index}"),
                    "success": False,
                    "cancelled": True,
                    "error": "Cancelado por usuario",
                    "agent_name": _task.get("agent_name", "General"),
                }
                continue

            worker = threading.Thread(target=_run_task, args=(_index, _task), daemon=True,
                                      name=f"subagent-{_index}")
            worker.start()
            deadline = time.monotonic() + timeout
            while worker.is_alive():
                if cancel_event is not None and cancel_event.is_set():
                    with self._lock:
                        agents_to_stop = list(active_agents)
                    for agent in agents_to_stop:
                        try:
                            if getattr(agent, "orchestrator", None) is not None:
                                agent.orchestrator.stop()
                            elif getattr(agent, "llm_client", None) is not None:
                                agent.llm_client.stop()
                        except Exception:
                            pass
                if time.monotonic() >= deadline:
                    errors[_index] = {
                        "task_id": _task.get("id", f"task_{_index}"),
                        "success": False,
                        "error": "Timeout",
                        "agent_name": _task.get("agent_name", "General"),
                    }
                    logger.warning("⏱️ Subagente %s excedió el timeout de %ss",
                                   _task.get("id"), timeout)
                    break
                worker.join(timeout=0.2)

        # Combinar resultados y errores
        final_results = []
        for i in range(len(tasks)):
            if results[i] is not None:
                final_results.append(results[i])
            elif errors[i] is not None:
                final_results.append(errors[i])
            else:
                final_results.append({
                    "task_id": tasks[i].get("id", f"task_{i}"),
                    "success": False,
                    "error": "Unknown error",
                    "agent_name": tasks[i].get("agent_name", "General")
                })

        return final_results

    def execute_plan(self, plan: Dict, context: Dict = None,
                     agent_factory: Callable = None,
                     timeout: int = 300,
                     cancel_event: Optional[threading.Event] = None) -> Dict:
        """
        Ejecuta un plan completo con dependencias entre tareas.
        
        plan: Diccionario con:
            - steps: Lista de pasos, cada uno con id, subtask, agent_name, depends_on
            - name: Nombre del plan (opcional)
        """
        if not plan or "steps" not in plan:
            return {"success": False, "error": "Plan inválido", "results": []}

        steps = plan["steps"]
        results_by_id = {}
        all_results = []
        
        # Construir grafo de dependencias
        task_map = {step["id"]: step for step in steps}
        completed = set()
        remaining = set(task_map.keys())
        
        wave = 0
        while remaining:
            if (cancel_event or self.cancel_event) is not None and (cancel_event or self.cancel_event).is_set():
                return {"success": False, "cancelled": True, "error": "Cancelado por usuario", "results": all_results}
            wave += 1
            # Identificar tareas cuyas dependencias ya están completadas
            available = []
            for task_id in remaining:
                task = task_map[task_id]
                deps = task.get("depends_on", [])
                if all(dep in completed for dep in deps):
                    available.append(task)
            
            if not available:
                # No hay tareas disponibles → dependencias cíclicas o bloqueo
                logger.error("❌ Bloqueo detectado: dependencias cíclicas o incompletas")
                return {
                    "success": False,
                    "error": "Dependencias cíclicas o incompletas",
                    "results": all_results,
                    "wave": wave
                }
            
            logger.info(f"🌊 Wave {wave}: {len(available)} tareas en paralelo")
            
            # Ejecutar las tareas disponibles en paralelo
            wave_results = self.execute_wave(available, context, agent_factory, timeout, cancel_event)
            
            # Procesar resultados
            for i, result in enumerate(wave_results):
                task_id = available[i]["id"]
                results_by_id[task_id] = result
                all_results.append(result)
                
                if result.get("success", False):
                    completed.add(task_id)
                else:
                    # Si falla, marcar como completada (para no bloquear) pero registrar error
                    logger.warning(f"⚠️ Tarea {task_id} falló, continuando con el resto")
                    completed.add(task_id)
            
            # Actualizar remaining
            remaining = remaining - completed
        
        # Verificar si todas las tareas fueron exitosas
        all_success = all(r.get("success", False) for r in all_results)
        
        return {
            "success": all_success,
            "results": all_results,
            "results_by_id": results_by_id,
            "waves": wave,
            "total_tasks": len(steps)
        }

    def execute_with_fallback(self, tasks: List[Dict], context: Dict = None,
                              agent_factory: Callable = None,
                              timeout: int = 300,
                              fallback_strategy: str = "continue",
                              cancel_event: Optional[threading.Event] = None) -> List[Dict]:
        """
        Ejecuta tareas en paralelo con estrategia de fallback.
        
        fallback_strategy:
            - "continue": Continúa con el resto si una falla
            - "stop": Detiene todas si una falla
            - "retry": Reintenta las fallidas una vez
        """
        results = self.execute_wave(tasks, context, agent_factory, timeout, cancel_event)
        
        if fallback_strategy == "stop":
            failed = [r for r in results if not r.get("success", False)]
            if failed:
                return results  # Devolver con errores para que el orquestador decida
        
        elif fallback_strategy == "retry":
            failed_indices = [i for i, r in enumerate(results) if not r.get("success", False)]
            if failed_indices:
                failed_tasks = [tasks[i] for i in failed_indices]
                logger.info(f"🔄 Reintentando {len(failed_tasks)} tareas fallidas...")
                retry_results = self.execute_wave(failed_tasks, context, agent_factory, timeout, cancel_event)
                for i, idx in enumerate(failed_indices):
                    results[idx] = retry_results[i]
        
        return results