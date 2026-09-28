# tools/job_manager.py
"""
Gestor de jobs en segundo plano.

Objetivo: que las herramientas del agente puedan lanzar comandos largos
(pip install, servidores, builds, scripts de horas) SIN:

  * abrir una consola externa (CREATE_NO_WINDOW en Windows),
  * bloquear la conversación con el modelo,
  * perder la salida (todo se escribe en app_data/jobs/<job_id>.log).

El modelo recibe un `job_id` inmediato y consulta después con
job_status / job_output / job_kill. No hay TTY interactiva: solo visor de
salida + control del proceso.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # pragma: no cover - import relativo dentro del paquete tools
    from . import process_manager
except ImportError:  # pragma: no cover
    import process_manager  # type: ignore

# ---------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

#: Número máximo de jobs simultáneos en estado "running".
MAX_RUNNING_JOBS = 8
#: Número máximo de jobs recordados en memoria (los más antiguos se olvidan).
MAX_KEPT_JOBS = 50
#: Tiempo máximo que `start` espera a que un comando rápido termine.
FAST_WAIT_SECONDS = 1.5
#: Líneas de salida devueltas como preview en `start`.
PREVIEW_LINES = 20
#: Líneas de salida cruda que se conservan en memoria por job.
MAX_RAW_LINES = 5000


def _state_root() -> Path:
    """Directorio donde viven los logs y metadatos de los jobs."""
    try:
        try:
            from . import runtime
        except ImportError:
            import runtime  # type: ignore
        base = Path(runtime.get_data_dir())
    except Exception:
        base = Path(os.getcwd()) / "app_data"
    root = base / "jobs"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return root


def _no_window_kwargs() -> Dict[str, Any]:
    """Evita consolas externas y huérfanos según la plataforma."""
    if os.name == "nt":
        return {"creationflags": CREATE_NO_WINDOW}
    return {"start_new_session": True}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def resolve_shell(command: str, shell: str = "auto") -> List[str]:
    """
    Convierte (command, shell) en la lista argv a ejecutar.

    shell:
      * "auto"  -> PowerShell en Windows, sh en POSIX
      * "powershell" / "pwsh" / "cmd" / "bash" / "sh" -> forzado
      * "none"  -> no usa shell, parte el comando con shlex
    """
    requested = (shell or "auto").strip().lower()
    if requested in ("auto", ""):
        requested = "powershell" if os.name == "nt" else "sh"

    if requested == "none":
        if os.name == "nt":
            # `shlex` en modo no-posix CONSERVA las comillas en cada token, y
            # Popen (sin shell) no las quiere cuando ejecuta el binario
            # directamente: `"C:\ruta con espacios\py.exe"` fallaría.
            return [token.strip('"') for token in shlex.split(command, posix=False)]
        return shlex.split(command, posix=True)

    if requested in ("powershell", "pwsh"):
        exe = "pwsh" if requested == "pwsh" else "powershell.exe"
        # PowerShell NO invoca un ejecutable entrecomillado sin el operador de
        # llamada: `"C:\ruta app\app.exe" --flag` es un error de parseo
        # ("Token '--flag' inesperado en la expresión o la instrucción"),
        # mientras que cmd.exe sí lo acepta. Por eso el mismo comando funcionaba
        # o fallaba según el shell que tocara.
        script = f"& {command}" if _starts_with_quoted_command(command) else command
        return [exe, "-NoProfile", "-Command", script]

    if requested == "cmd":
        return ["cmd.exe", "/c", command]

    if requested in ("bash", "sh"):
        exe = "bash" if requested == "bash" else "sh"
        # -c mantiene el comando tal cual; sin TTY interactiva.
        return [exe, "-c", command]

    # Shell desconocido: dejamos que el SO lo resuelva.
    return [command] if os.name == "nt" else ["sh", "-c", command]


def _starts_with_quoted_command(command: str) -> bool:
    """True si el comando empieza invocando un binario entrecomillado."""
    return str(command or "").lstrip()[:1] in ('"', "'")


def resolve_cwd(cwd: Optional[str] = None) -> str:
    if cwd:
        try:
            return str(Path(cwd).expanduser())
        except Exception:
            pass
    return os.getcwd()


class Job:
    """Un proceso en segundo plano con su log."""

    def __init__(self, job_id: str, argv: List[str], command: str, shell: str,
                 cwd: str, log_path: Path, meta_path: Path, label: Optional[str] = None):
        self.id = job_id
        self.argv = argv
        self.command = command
        self.shell = shell
        self.cwd = cwd
        self.label = label or ""
        self.log_path = log_path
        self.meta_path = meta_path
        # Salida CRUDA del proceso (stdout+stderr), sin la cabecera ni el pie
        # que el visor escribe en el .log. Es lo único que debe llegar al
        # modelo: el log decorado solo estorba en la conversación.
        self.stdout_path = log_path.with_suffix(".out")
        self.stdout_lines: List[str] = []
        self.pid: Optional[int] = None
        self.status = "starting"          # starting|running|exited|killed|error
        self.exit_code: Optional[int] = None
        self.started_at = _now()
        self.ended_at: Optional[str] = None
        self.error: Optional[str] = None
        self.process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    # ---------- serialización ----------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.id,
            "label": self.label,
            "command": self.command,
            "shell": self.shell,
            "cwd": self.cwd,
            "pid": self.pid,
            "status": self.status,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "elapsed": self.elapsed(),
            "log_path": str(self.log_path),
            "error": self.error,
        }

    def elapsed(self) -> float:
        try:
            start = datetime.strptime(self.started_at, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return 0.0
        end = datetime.now() if self.status in ("starting", "running") else datetime.strptime(
            self.ended_at or self.started_at, "%Y-%m-%d %H:%M:%S")
        return round((end - start).total_seconds(), 1)

    def save_meta(self) -> None:
        try:
            self.meta_path.write_text(
                json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass


class JobManager:
    """Registro de jobs + hilos lectores que escriben la salida a disco."""

    def __init__(self) -> None:
        self._jobs: "OrderedDict[str, Job]" = OrderedDict()
        self._lock = threading.RLock()
        self._root = _state_root()

    # ---------------------------------------------------------
    # Arranque
    # ---------------------------------------------------------
    def start(self, command: str, shell: str = "auto", cwd: Optional[str] = None,
              env: Optional[Dict[str, str]] = None, label: Optional[str] = None,
              wait_seconds: float = FAST_WAIT_SECONDS) -> Dict[str, Any]:
        if not command or not str(command).strip():
            return {"status": "error", "error": "Comando vacío"}

        with self._lock:
            running = sum(1 for j in self._jobs.values() if j.status in ("starting", "running"))
            if running >= MAX_RUNNING_JOBS:
                return {
                    "status": "error",
                    "error": f"Demasiados jobs en ejecución ({running}). "
                             f"Usa job_kill o job_list antes de lanzar otro.",
                }

        job_id = uuid.uuid4().hex[:12]
        log_path = self._root / f"{job_id}.log"
        meta_path = self._root / f"{job_id}.json"
        argv = resolve_shell(str(command), shell)
        workdir = resolve_cwd(cwd)
        job = Job(job_id, argv, str(command), shell, workdir, log_path, meta_path, label)

        child_env = os.environ.copy()
        child_env.setdefault("PYTHONUNBUFFERED", "1")
        child_env.setdefault("PYTHONIOENCODING", "utf-8")
        if env:
            child_env.update({str(k): str(v) for k, v in env.items()})

        try:
            with open(log_path, "w", encoding="utf-8", errors="replace") as log_fh:
                log_fh.write(f"[{_now()}] $ {command}\n")
                log_fh.write(f"[{_now()}] shell={shell} cwd={workdir}\n")
                log_fh.flush()
                job.process = subprocess.Popen(
                    argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    cwd=workdir,
                    env=child_env,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    **_no_window_kwargs(),
                )
        except FileNotFoundError as e:
            return {"status": "error", "error": f"No se encontró el ejecutable: {e}"}
        except Exception as e:
            return {"status": "error", "error": f"No se pudo lanzar el job: {e}"}

        job.pid = job.process.pid
        job.status = "running"
        with self._lock:
            self._jobs[job_id] = job
            while len(self._jobs) > MAX_KEPT_JOBS:
                old_id, old_job = self._jobs.popitem(last=False)
                if old_job.status in ("starting", "running"):
                    self._jobs[old_id] = old_job
                    self._jobs.move_to_end(old_id)
                    break
        job.save_meta()

        threading.Thread(target=self._reader, args=(job,), daemon=True,
                         name=f"job-reader-{job_id}").start()

        # Si el comando es rápido, devolvemos ya el resultado completo.
        deadline = time.time() + max(0.0, float(wait_seconds or 0))
        while job.status in ("starting", "running") and time.time() < deadline:
            time.sleep(0.05)

        result: Dict[str, Any] = {
            "job_id": job.id,
            "status": job.status,
            "pid": job.pid,
            "log_path": str(job.log_path),
            "command": job.command,
        }
        if job.status in ("exited", "error", "killed"):
            result.update({
                "exit_code": job.exit_code,
                "elapsed": job.elapsed(),
                # Solo la salida del proceso: el modelo no necesita el "$ comando"
                # ni el cwd ni el pie de "proceso finalizado".
                "output": self._read_stdout(job.id, tail=None),
            })
            if job.status == "error":
                # El motivo viaja junto a la salida. Sin esta clave los llamadores
                # (exec_shell_command, run_python_code, bash) leían
                # `res.get("error")` como None y el modelo solo veía
                # «Error: None» en el chat, sin el traceback ni el stderr.
                result["error"] = job.error or f"Proceso terminado con código {job.exit_code}"
        else:
            result["preview"] = self._read_stdout(job.id, tail=PREVIEW_LINES)
            result["hint"] = ("Sigue en ejecución. Usa job_output(job_id) para leer más "
                              "salida o job_kill(job_id) para detenerlo.")
        return result

    # ---------------------------------------------------------
    # Hilo lector
    # ---------------------------------------------------------
    def _reader(self, job: Job) -> None:
        assert job.process is not None
        try:
            with open(job.log_path, "a", encoding="utf-8", errors="replace") as log_fh, \
                    open(job.stdout_path, "a", encoding="utf-8", errors="replace") as raw_fh:
                stdout = job.process.stdout
                if stdout is not None:
                    for line in stdout:
                        text = line if line.endswith("\n") else line + "\n"
                        log_fh.write(text)
                        log_fh.flush()
                        raw_fh.write(text)
                        raw_fh.flush()
                        with job._lock:
                            job.stdout_lines.append(line.rstrip("\n"))
                            # Un proceso charlatán no debe comerse la memoria del
                            # servidor; el fichero sigue teniendo todo.
                            if len(job.stdout_lines) > MAX_RAW_LINES:
                                del job.stdout_lines[: len(job.stdout_lines) - MAX_RAW_LINES]
                job.process.wait()
                code = job.process.returncode
                if job.status != "killed":
                    job.status = "exited" if code == 0 else "error"
                    if code != 0:
                        job.error = f"El proceso terminó con código {code}"
                job.exit_code = code
                job.ended_at = _now()
                log_fh.write(f"\n[{_now()}] --- proceso finalizado (status={job.status}, "
                             f"exit_code={code}) ---\n")
                log_fh.flush()
        except Exception as e:  # pragma: no cover
            job.status = "error"
            job.error = str(e)
            job.ended_at = _now()
        finally:
            job.save_meta()

    # ---------------------------------------------------------
    # Jobs externos (creados por otro proceso, p.ej. MCP-stdio del CLI):
    # solo queda su meta en disco; se consultan y matan por PID.
    # ---------------------------------------------------------
    def _meta_dicts(self) -> "OrderedDict[str, Dict[str, Any]]":
        metas: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        try:
            for meta_path in sorted(self._root.glob("*.json")):
                try:
                    data = json.loads(meta_path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if isinstance(data, dict) and data.get("job_id"):
                    metas[str(data["job_id"])] = data
        except Exception:
            pass
        return metas

    @staticmethod
    def _pid_alive(pid: Optional[int]) -> bool:
        if not pid:
            return False
        try:
            if os.name == "nt":
                import ctypes
                SYNCHRONIZE = 0x00100000
                handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
                if not handle:
                    return False
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            os.kill(int(pid), 0)
            return True
        except Exception:
            return False

    def _write_meta(self, job_id: str, data: Dict[str, Any]) -> None:
        try:
            (self._root / f"{job_id}.json").write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    def _external_dict(self, job_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
        """Dict de un job externo; si el meta dice running pero el PID ya no
        vive, corrige el estado a exited (y persiste la corrección)."""
        data = dict(meta)
        if data.get("status") in ("starting", "running"):
            if not self._pid_alive(data.get("pid")):
                data["status"] = "exited"
                self._write_meta(job_id, data)
        data["external"] = True
        return data

    def _count_lines_path(self, path: Path) -> int:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return sum(1 for _ in fh)
        except Exception:
            return 0

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, include_finished: bool = True) -> List[Dict[str, Any]]:
        with self._lock:
            jobs = list(self._jobs.values())
            known = set(self._jobs.keys())
        result = [j.to_dict() for j in jobs]
        if not include_finished:
            result = [d for d in result if d.get("status") in ("starting", "running")]
        for job_id, meta in self._meta_dicts().items():
            if job_id in known:
                continue
            d = self._external_dict(job_id, meta)
            if not include_finished and d.get("status") not in ("starting", "running"):
                continue
            result.append(d)
        return result

    def _external_job_meta(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Meta de un job externo (creado por otro proceso) si existe."""
        if self.get(job_id):
            return None
        meta = self._meta_dicts().get(job_id)
        return self._external_dict(job_id, meta) if meta else None

    def status(self, job_id: str) -> Dict[str, Any]:
        job = self.get(job_id)
        if job:
            data = job.to_dict()
            data["output_lines"] = self._count_lines(job)
            return data
        meta = self._external_job_meta(job_id)
        if meta:
            meta["output_lines"] = self._count_lines_path(
                self._root / f"{job_id}.log")
            return meta
        return {"status": "error", "error": f"Job '{job_id}' no encontrado."}

    def output(self, job_id: str, tail: Optional[int] = 200, offset: int = 0,
               raw: bool = False) -> Dict[str, Any]:
        """Salida de un job.

        `raw=False` (visor/consola) devuelve el log completo, con la cabecera de
        comando/cwd y el pie de "proceso finalizado". `raw=True` (herramientas
        del modelo) devuelve solo lo que imprimió el proceso.
        """
        hint = "El job sigue en ejecución; vuelve a llamar a job_output para ver más."
        job = self.get(job_id)
        if job:
            tail_n = None if tail in (None, 0) else int(tail)
            if raw:
                lines = self._read_stdout(job_id, tail=tail_n, offset=offset)
                total = self._count_raw_lines(job)
            else:
                lines = self._read_lines(job_id, tail=tail_n, offset=offset)
                total = self._count_lines(job)
            return {
                "job_id": job.id,
                "status": job.status,
                "exit_code": job.exit_code,
                "elapsed": job.elapsed(),
                "log_path": str(job.log_path),
                "total_lines": total,
                "returned_lines": len(lines),
                "lines": lines,
                "hint": (None if job.status in ("exited", "error", "killed") else hint),
            }
        meta = self._external_job_meta(job_id)
        if meta:
            tail_n = None if tail in (None, 0) else int(tail)
            log_path = self._root / f"{job_id}.log"
            if raw:
                lines = self._read_stdout(job_id, tail=tail_n, offset=offset)
                total = len(self._read_stdout(job_id, tail=None))
            else:
                lines = self._read_lines_from_path(log_path, tail=tail_n, offset=offset)
                total = self._count_lines_path(log_path)
            status = str(meta.get("status") or "running")
            return {
                "job_id": job_id,
                "status": status,
                "exit_code": meta.get("exit_code"),
                "elapsed": meta.get("elapsed"),
                "log_path": meta.get("log_path"),
                "total_lines": total,
                "returned_lines": len(lines),
                "lines": lines,
                "hint": (None if status in ("exited", "error", "killed") else hint),
            }
        return {"status": "error", "error": f"Job '{job_id}' no encontrado."}

    # ---------------------------------------------------------
    # Salida cruda (sin decoración del visor)
    # ---------------------------------------------------------
    def _read_stdout(self, job_id: str, tail: Optional[int] = None,
                     offset: int = 0) -> List[str]:
        """Líneas impresas por el proceso, sin cabecera ni pie."""
        job = self.get(job_id)
        raw_path = job.stdout_path if job else (self._root / f"{job_id}.out")
        if raw_path.exists():
            lines = self._read_lines_from_path(raw_path)
        elif job is not None:
            with job._lock:
                lines = list(job.stdout_lines)
        else:
            # Job antiguo (sin `.out`): se deduce del log quitando la decoración.
            lines = self._strip_log_decorations(
                self._read_lines_from_path(self._root / f"{job_id}.log"))
        if offset:
            lines = lines[int(offset):]
        if tail:
            lines = lines[-int(tail):]
        return lines

    def _count_raw_lines(self, job: Job) -> int:
        try:
            if job.stdout_path.exists():
                return self._count_lines_path(job.stdout_path)
        except Exception:
            pass
        with job._lock:
            return len(job.stdout_lines)

    @staticmethod
    def _strip_log_decorations(lines: List[str]) -> List[str]:
        """Quita la cabecera (`$ comando` + `shell=... cwd=...`) y el pie final."""
        body = list(lines)
        if len(body) >= 2 and body[0].startswith("[") and " $ " in body[0] and "shell=" in body[1]:
            body = body[2:]
        while body and (not body[-1].strip() or "--- proceso finalizado" in body[-1]):
            body.pop()
        return body

    def _read_lines_from_path(self, path: Path, tail: Optional[int] = None,
                              offset: int = 0) -> List[str]:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                all_lines = fh.read().splitlines()
        except Exception:
            return []
        if offset:
            all_lines = all_lines[int(offset):]
        if tail is not None:
            all_lines = all_lines[-int(tail):] if int(tail) > 0 else []
        return all_lines

    def kill(self, job_id: str, timeout: float = 3.0) -> Dict[str, Any]:
        job = self.get(job_id)
        if not job:
            # Job externo: matar por PID desde su meta.
            meta = self._meta_dicts().get(job_id)
            if not meta:
                return {"status": "error", "error": f"Job '{job_id}' no encontrado."}
            meta = self._external_dict(job_id, meta)
            if meta.get("status") not in ("starting", "running"):
                return {"status": meta.get("status"), "job_id": job_id,
                        "message": "El job ya había terminado.",
                        "exit_code": meta.get("exit_code")}
            pid = meta.get("pid")
            ok = False
            try:
                process_manager.kill_process_tree(int(pid), timeout=float(timeout))
                ok = True
            except Exception as e:
                return {"status": "error", "job_id": job_id, "error": str(e)}
            meta.update({"status": "killed", "ended_at": _now()})
            self._write_meta(job_id, meta)
            return {"status": "killed", "job_id": job_id,
                    "message": "Proceso externo detenido por PID." if ok else "Sin confirmación.",
                    "pid": pid}
        if job.status not in ("starting", "running"):
            return {"status": job.status, "job_id": job.id,
                    "message": "El job ya había terminado.", "exit_code": job.exit_code}
        job.status = "killed"
        job.ended_at = _now()
        killed = False
        try:
            if job.pid:
                killed = bool(process_manager.kill_process_tree(int(job.pid), timeout=timeout))
        except Exception:
            killed = False
        if not killed and job.process is not None:
            try:
                job.process.kill()
                killed = True
            except Exception:
                pass
        job.save_meta()
        return {"status": "killed", "job_id": job.id, "pid": job.pid, "killed": killed}

    def stop_all(self) -> int:
        """Mata todos los jobs activos (se llama al cerrar la app)."""
        count = 0
        for data in self.list(include_finished=False):
            try:
                self.kill(data["job_id"])
                count += 1
            except Exception:
                pass
        return count

    def clear_finished(self, include_failed: bool = True) -> Dict[str, Any]:
        """Borra del registro los jobs que ya terminaron (memoria y disco).

        Es imprescindible borrar los `.json`/`.log` de `_state_root()`: son la
        fuente de los jobs "externos" que `list()` mezcla, así que limpiando solo
        la memoria el listado se repoblaba al instante y "Limpiar terminados"
        parecía no hacer nada.
        """
        finished_states = ("exited", "killed", "error") if include_failed else ("exited",)
        removed: List[str] = []
        with self._lock:
            for job_id in list(self._jobs.keys()):
                job = self._jobs.get(job_id)
                if job is not None and job.status in finished_states:
                    self._jobs.pop(job_id, None)
                    removed.append(job_id)
        for meta_path in list(self._root.glob("*.json")):
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            job_id = str(data.get("job_id") or "")
            if not job_id or data.get("status") not in finished_states:
                continue
            for path in (meta_path, self._root / f"{job_id}.log", self._root / f"{job_id}.out"):
                try:
                    path.unlink()
                except Exception:
                    pass
            if job_id not in removed:
                removed.append(job_id)
        return {"status": "ok", "count": len(removed), "cleared": removed}

    # ---------------------------------------------------------
    # Helpers de log
    # ---------------------------------------------------------
    def _read_lines(self, job_id: str, tail: Optional[int] = 200,
                    offset: int = 0) -> List[str]:
        job = self.get(job_id)
        path = job.log_path if job else (self._root / f"{job_id}.log")
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return []
        lines = content.splitlines()
        if offset:
            lines = lines[int(offset):]
        if tail:
            lines = lines[-int(tail):]
        return lines

    def _count_lines(self, job: Job) -> int:
        try:
            with open(job.log_path, "r", encoding="utf-8", errors="replace") as fh:
                return sum(1 for _ in fh)
        except Exception:
            return 0


# ---------------------------------------------------------------
# Singleton + API de módulo (la usan las tools de dev_agent)
# ---------------------------------------------------------------
_manager: Optional[JobManager] = None
_manager_lock = threading.Lock()


def get_manager() -> JobManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = JobManager()
    return _manager


def start_job(command: str, shell: str = "auto", cwd: Optional[str] = None,
              env: Optional[Dict[str, str]] = None, label: Optional[str] = None,
              wait_seconds: float = FAST_WAIT_SECONDS) -> Dict[str, Any]:
    return get_manager().start(command, shell=shell, cwd=cwd, env=env,
                               label=label, wait_seconds=wait_seconds)


def list_jobs(include_finished: bool = True) -> List[Dict[str, Any]]:
    return get_manager().list(include_finished=include_finished)


def job_status(job_id: str) -> Dict[str, Any]:
    return get_manager().status(job_id)


def job_output(job_id: str, tail: Optional[int] = 200, offset: int = 0,
               raw: bool = False) -> Dict[str, Any]:
    return get_manager().output(job_id, tail=tail, offset=offset, raw=raw)


def job_kill(job_id: str) -> Dict[str, Any]:
    return get_manager().kill(job_id)


def stop_all_jobs() -> int:
    return get_manager().stop_all()


def clear_finished_jobs(include_failed: bool = True) -> Dict[str, Any]:
    """Elimina del listado (y del disco) los jobs ya terminados."""
    return get_manager().clear_finished(include_failed=include_failed)
