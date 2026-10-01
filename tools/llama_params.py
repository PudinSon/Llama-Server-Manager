"""Lógica compartida de construcción de comandos para ``llama-server``.

Tanto el manager de escritorio (``Llama_Server_Manager.py``) como el panel web
(``tools/web_manager_server.py``) consumen estas constantes y métodos, de forma
que los settings de rendimiento, DSpark y MoE se comporten igual en ambos y no
haya que mantener dos copias de la misma lógica.

La clase ``LlamaCommandMixin`` asume que el objeto que la usa expone:

- ``self.defaults``: dict de configuración.
- ``self.data_dir``: carpeta de datos (donde viven ``mmproj/`` y ``dspark/``).
- ``self.llama_exe`` / ``self.llama_cuda_exe``: rutas de los ejecutables.
- ``self.log(mensaje)``: logger.
"""

from __future__ import annotations

import os
import re
import subprocess

try:
    import psutil

    _PSUTIL_AVAILABLE = True
except Exception:  # pragma: no cover - psutil es opcional en runtime
    psutil = None
    _PSUTIL_AVAILABLE = False


# ── Razonamiento ───────────────────────────────────────────────────────────
#: Valores que acepta `--reasoning-format` de llama-server (una lista cerrada:
#: un valor desconocido aborta el arranque del servidor). Orden = el del
#: selector en las dos interfaces.
#:
#: - `none` — deja los "thoughts" sin parsear dentro de `message.content`.
#: - `deepseek` — los mueve a `message.reasoning_content`, que es lo que lee
#:   `agent_core.chat_stream` (el agente los muestra como razonamiento aparte).
#: - `deepseek-legacy` — mantiene las etiquetas `<think>` en el contenido y
#:   además rellena `message.reasoning_content`.
#: - `auto` — decide la plantilla del modelo (valor por defecto del servidor).
REASONING_FORMATS = ("none", "deepseek", "deepseek-legacy", "auto")
#: Valor por defecto: `auto`, el mismo comportamiento que tenía no pasar el flag
#: (y que, como el resto de defaults del servidor, no se inyecta).
DEFAULT_REASONING_FORMAT = "auto"


def migrate_reasoning_format(defaults: dict) -> None:
    """Migra el antiguo checkbox `reasoning_format_enabled` al selector.

    `reasoning_format_enabled=True` emitía `--reasoning-format deepseek`;
    `False` —el valor por defecto— no emitía el flag, que equivale al `auto`
    del servidor (que sigue sin inyectarse). La migración conserva el comportamiento anterior exacto y
    normaliza el valor guardado: minúsculas, y a `auto` si no está en la lista
    (llama-server no arranca con un formato desconocido).
    """
    if not isinstance(defaults, dict):
        return

    legacy = defaults.pop("reasoning_format_enabled", None)
    current = defaults.get("reasoning_format")

    if isinstance(current, str) and current.strip().lower() in REASONING_FORMATS:
        defaults["reasoning_format"] = current.strip().lower()
        return

    if legacy is None:
        defaults["reasoning_format"] = DEFAULT_REASONING_FORMAT
        return

    defaults["reasoning_format"] = "deepseek" if bool(legacy) else DEFAULT_REASONING_FORMAT


# ── Rendimiento, DSpark y MoE ──────────────────────────────────────────────
# Defaults conservadores: no alteran el comportamiento previo de llama.cpp.
LLAMA_PERFORMANCE_DEFAULTS = {
    # Rendimiento
    "batch_size": 2048,          # -b
    "ubatch_size": 512,          # -ub
    "parallel": 1,              # -np (auto)
    "poll": 50,                  # --poll
    "threads_http": -1,          # --threads-http (auto)
    "timeout": 3600,             # -to
    "perf_enabled": False,       # --perf (diagnóstico)
    "no_kv_offload": False,      # -nkvo
    "load_mode": "auto",         # -lm: auto/mmap/mlock/mmap+mlock/dio
    # Razonamiento (Configuraciones Avanzadas → Rendimiento, tras --perf).
    # `reasoning_preserve` mantiene la traza de razonamiento en TODO el
    # historial (no solo en el último mensaje del asistente); la plantilla debe
    # declarar 'supports_preserve_reasoning' o llama.cpp lo ignora.
    # `reasoning_format` es el valor que se pasa a `--reasoning-format`.
    "reasoning_preserve": False,
    "reasoning_format": DEFAULT_REASONING_FORMAT,
    # Context checkpoints: reutilización de prefijo KV en pasos sucesivos.
    # A diferencia de --cache-reuse, NO se ve afectado por multimodal
    # (--mmproj). Sin esto, cada paso de AGENCY reprocesa el prompt entero.
    # 0 = deshabilitado; 32 = valor recomendado por llama.cpp.
    "ctx_checkpoints": 32,
    # DSpark (speculative decoding ligero)
    "dspark_enabled": True,
    "gpu_for_dspark": False,
    "n_gpu_layers_draft": "auto",   # "auto", "all" o entero
    "spec_draft_n_max": 3,          # default de llama.cpp
    "spec_draft_n_min": 0,
    "spec_draft_type_k": "f16",     # -ctkd
    "spec_draft_type_v": "f16",     # -ctvd
    "spec_draft_threads": None,     # -td (None = heredar)
    "spec_draft_threads_batch": None,  # -tbd (None = heredar)
    # MoE
    "moe_enabled": False,
    "gpu_for_moe": False,
    "cpu_moe": False,             # -cmoe
    "n_cpu_moe": 0,               # -ncmoe
    "override_tensor": "",        # -ot
}


MOE_NAME_TOKENS = ("moe", "mixtral", "deepseek", "gpt-oss", "hunyuan", "exaone")


class LlamaCommandMixin:
    """Métodos compartidos para armar el comando de llama-server."""

    _MOE_NAME_TOKENS = MOE_NAME_TOKENS

    # ------------------------------------------------------------------
    # Utilidades básicas
    # ------------------------------------------------------------------
    def _has_param(self, cmd, prefix):
        """Verifica si algún elemento de cmd comienza con prefix."""
        return any(str(arg).startswith(prefix) for arg in cmd)

    def _has_flag_exact(self, cmd, *names):
        """True si cmd ya contiene alguno de los flags indicados (coincidencia exacta)."""
        return any(arg in names for arg in cmd)

    def _safe_int(self, value, default=None):
        """Convierte a int de forma tolerante (acepta '12', 12.0, None...)."""
        try:
            return int(value)
        except Exception:
            try:
                return int(float(value))
            except Exception:
                return default

    def _safe_float(self, value, default=None):
        """Convierte a float de forma tolerante (acepta '0.95', 1, None...)."""
        try:
            return float(value)
        except Exception:
            return default

    def _normalize_ngl(self, value, default="0"):
        """Normaliza capas GPU admitiendo int, 'auto', 'all' o números en texto."""
        if value is None or isinstance(value, bool):
            return str(default)
        if isinstance(value, (int, float)):
            return str(int(value))
        text = str(value).strip().lower()
        if text in ("auto", "all"):
            return text
        try:
            return str(int(float(text)))
        except Exception:
            return str(default)

    def _get_optimal_threads(self):
        """Hilos óptimos para generación (dejando núcleos para el sistema)."""
        cpu_count = None
        if _PSUTIL_AVAILABLE:
            cpu_count = psutil.cpu_count(logical=False) or psutil.cpu_count()
        if cpu_count is None:
            cpu_count = os.cpu_count()
        if cpu_count is None:
            return 4
        return max(1, cpu_count - 2)

    def _get_optimal_batch_threads(self):
        """Hilos óptimos para prefill (batch)."""
        cpu_count = None
        if _PSUTIL_AVAILABLE:
            cpu_count = psutil.cpu_count(logical=False) or psutil.cpu_count()
        if cpu_count is None:
            cpu_count = os.cpu_count()
        if cpu_count is None:
            return 6
        return max(1, min(16, int(cpu_count * 1.5)))

    # ------------------------------------------------------------------
    # Detección de flags soportados por la build de llama.cpp
    # ------------------------------------------------------------------
    def _get_supported_flags(self):
        """Ejecuta `llama-server --help` una sola vez y cachea los flags soportados."""
        cached = getattr(self, "_supported_flags", None)
        if cached is not None:
            return cached
        flags = set()
        try:
            exe = self.llama_cuda_exe if os.path.exists(self.llama_cuda_exe) else self.llama_exe
            if os.path.exists(exe):
                kwargs = {}
                if os.name == "nt":
                    kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                result = subprocess.run(
                    [exe, "--help"],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    **kwargs,
                )
                text = f"{result.stdout or ''}\n{result.stderr or ''}"
                flags = set(re.findall(r"(?<![\w-])(--?[A-Za-z][A-Za-z0-9-]*)", text))
                if flags:
                    self.log(f"🔎 Flags soportados detectados en llama-server: {len(flags)}")
        except Exception as error:
            self.log(f"⚠️ No se pudieron verificar los flags de llama-server: {error}")
            flags = set()
        self._supported_flags = flags
        return flags

    def _flag_supported(self, *candidates):
        """True si al menos uno de los flags existe en la build (o si no pudo verificarse)."""
        flags = self._get_supported_flags()
        if not flags:
            return True  # Sin información fiable: no bloquear la inyección.
        return any(flag in flags for flag in candidates)

    # ------------------------------------------------------------------
    # Detección MoE
    # ------------------------------------------------------------------
    def _is_moe_model(self, model_name_or_filename):
        """Heurística por nombre para detectar arquitecturas MoE."""
        try:
            name = os.path.splitext(os.path.basename(model_name_or_filename))[0].lower()
        except Exception:
            return False
        if any(token in name for token in self._MOE_NAME_TOKENS):
            return True
        # Qwen MoE: sufijos tipo -A3B / -A22B (parámetros activos)
        if re.search(r"qwen.*a\d+b", name):
            return True
        # Familia Ling (MoE) u otros nombres que empiezan por "ling"
        if re.search(r"(^|[^a-z])ling([^a-z]|$)", name):
            return True
        return False

    # ------------------------------------------------------------------
    # Selector de ejecutable (CPU vs CUDA)
    # ------------------------------------------------------------------
    def _select_executable(self):
        """Elige el ejecutable según si el modelo, el draft o los expertos MoE piden GPU."""
        wants_gpu = bool(self.defaults.get("gpu_for_chat", False))
        if self.defaults.get("dspark_enabled", True) and self.defaults.get("gpu_for_dspark", False):
            wants_gpu = True
        if self.defaults.get("moe_enabled", False) and self.defaults.get("gpu_for_moe", False):
            wants_gpu = True
        if wants_gpu:
            if os.path.exists(self.llama_cuda_exe):
                return self.llama_cuda_exe, True
            self.log("⚠️ Se solicitó GPU pero no se encontró llama-server CUDA; usando CPU.")
            return self.llama_exe, False
        return self.llama_exe, False

    # ------------------------------------------------------------------
    # Inyección de parámetros
    # ------------------------------------------------------------------
    def _add_optimization_params(self, cmd):
        """Inyecta parámetros de optimización en el comando si no existen."""
        if not self._has_param(cmd, "--flash-attn"):
            cmd.extend(["--flash-attn", "on"])
        if not self._has_param(cmd, "--cache-prompt"):
            cmd.append("--cache-prompt")
        if not self._has_param(cmd, "--cache-reuse"):
            cmd.extend(["--cache-reuse", "256"])
        if not self._has_param(cmd, "-ctk") and not self._has_param(cmd, "--cache-type-k"):
            cmd.extend(["-ctk", "f16"])
        if not self._has_param(cmd, "-ctv") and not self._has_param(cmd, "--cache-type-v"):
            cmd.extend(["-ctv", "f16"])
        if not self._has_param(cmd, "--cache-ram"):
            cmd.extend(["--cache-ram", "8192"])
        if not self._has_param(cmd, "--prio"):
            cmd.extend(["--prio", "0"])

        # Ajustar threads si no están especificados
        if not self._has_param(cmd, "-t") and not self._has_param(cmd, "--threads"):
            cmd.extend(["-t", str(self._get_optimal_threads())])
        if not self._has_param(cmd, "-tb") and not self._has_param(cmd, "--threads-batch"):
            cmd.extend(["-tb", str(self._get_optimal_batch_threads())])
        if not self._has_param(cmd, "--ctx-checkpoints"):
            checkpoints = self._safe_int(self.defaults.get("ctx_checkpoints", 32), 32)
            if checkpoints > 0:
                cmd.extend(["--ctx-checkpoints", str(checkpoints)])

        return cmd

    def _inject_sampling_params(self, cmd):
        """Añade el muestreo generalista que va tras `--repeat-penalty`.

        `--top-k 40` y `--top-p 0.95` son los valores generalistas por defecto y
        se emiten SIEMPRE (no condicionados a "distinto del default"): así el
        comando real y lo que muestra la configuración coinciden. La puerta es
        única para que el manager de escritorio y el panel web no puedan
        divergir en el muestreo.
        """
        if not self._has_flag_exact(cmd, "--top-k"):
            top_k = self._safe_int(self.defaults.get("top_k", 40), 40)
            cmd.extend(["--top-k", str(top_k)])
        if not self._has_flag_exact(cmd, "--top-p"):
            top_p = self._safe_float(self.defaults.get("top_p", 0.95), 0.95)
            cmd.extend(["--top-p", f"{top_p:g}"])
        return cmd

    def _inject_performance_params(self, cmd):
        """Inyecta parámetros de rendimiento solo si difieren de los defaults de llama.cpp."""
        if not self._has_flag_exact(cmd, "-b", "--batch-size"):
            batch = self._safe_int(self.defaults.get("batch_size", 2048), 2048)
            if batch != 2048:
                cmd.extend(["-b", str(batch)])
        if not self._has_flag_exact(cmd, "-ub", "--ubatch-size"):
            ubatch = self._safe_int(self.defaults.get("ubatch_size", 512), 512)
            if ubatch != 512:
                cmd.extend(["-ub", str(ubatch)])
        if not self._has_flag_exact(cmd, "-np", "--parallel"):
            parallel = self._safe_int(self.defaults.get("parallel", -1), -1)
            if parallel != -1:
                cmd.extend(["-np", str(parallel)])
        if not self._has_flag_exact(cmd, "--poll"):
            poll = self._safe_int(self.defaults.get("poll", 50), 50)
            if poll != 50:
                cmd.extend(["--poll", str(poll)])
        if not self._has_flag_exact(cmd, "--threads-http"):
            threads_http = self._safe_int(self.defaults.get("threads_http", -1), -1)
            if threads_http != -1:
                cmd.extend(["--threads-http", str(threads_http)])
        if not self._has_flag_exact(cmd, "-to", "--timeout"):
            timeout = self._safe_int(self.defaults.get("timeout", 3600), 3600)
            if timeout != 3600:
                cmd.extend(["-to", str(timeout)])
        if self.defaults.get("perf_enabled", False) and not self._has_flag_exact(cmd, "--perf"):
            cmd.append("--perf")
        # Razonamiento: va justo después de --perf, como en la configuración.
        if (
            self.defaults.get("reasoning_preserve", False)
            and not self._has_flag_exact(cmd, "--reasoning-preserve", "--no-reasoning-preserve")
            and self._flag_supported("--reasoning-preserve")
        ):
            cmd.append("--reasoning-preserve")
        reasoning_format = str(
            self.defaults.get("reasoning_format", DEFAULT_REASONING_FORMAT)
            or DEFAULT_REASONING_FORMAT
        ).strip().lower()
        # Solo valores de la lista (llama-server aborta el arranque con
        # "Unknown reasoning format" y aquí no hay terminal que lo explique) y
        # nunca el valor por defecto del servidor: `auto` no se inyecta, igual
        # que el resto de parámetros que coinciden con los defaults.
        if (
            reasoning_format in REASONING_FORMATS
            and reasoning_format != DEFAULT_REASONING_FORMAT
            and not self._has_flag_exact(cmd, "--reasoning-format")
            and self._flag_supported("--reasoning-format")
        ):
            # `deepseek` deja los "thoughts" en `message.reasoning_content` en
            # vez de mezclarlos en el contenido: es lo que lee `agent_core`.
            cmd.extend(["--reasoning-format", reasoning_format])
        if self.defaults.get("no_kv_offload", False) and not self._has_flag_exact(cmd, "-nkvo", "--no-kv-offload"):
            cmd.append("-nkvo")
        load_mode = str(self.defaults.get("load_mode", "auto") or "auto").strip().lower()
        if load_mode not in ("", "auto") and not self._has_flag_exact(cmd, "-lm", "--load-mode"):
            if self._flag_supported("-lm", "--load-mode"):
                cmd.extend(["-lm", load_mode])
        return cmd

    def _apply_llama_optimizations(
        self,
        cmd,
        model_name=None,
        include_performance=True,
        include_speculation=True,
        include_moe=True,
    ):
        """Aplica muestreo, optimización, rendimiento, DSpark y MoE de forma segura."""
        self._inject_sampling_params(cmd)
        self._add_optimization_params(cmd)
        if include_performance:
            self._inject_performance_params(cmd)
        if include_speculation and model_name:
            try:
                cmd.extend(self._dspark_args(model_name))
            except Exception as error:
                self.log(f"⚠️ Error resolviendo DSpark: {error}")
        if include_moe and model_name:
            try:
                cmd.extend(self._moe_args(model_name))
            except Exception as error:
                self.log(f"⚠️ Error resolviendo MoE: {error}")
        return cmd

    # ------------------------------------------------------------------
    # DSpark
    # ------------------------------------------------------------------
    def _dspark_dir(self, model_basename):
        return os.path.join(self.data_dir, "dspark", model_basename)

    def _find_dspark_gguf(self, model_name_or_filename):
        """Devuelve la ruta del draft DSpark asociado al modelo, si existe."""
        try:
            model_basename = os.path.splitext(os.path.basename(model_name_or_filename))[0].lower()
        except Exception:
            return None
        draft_dir = self._dspark_dir(model_basename)
        candidates = []
        try:
            if os.path.exists(draft_dir):
                for f in sorted(os.listdir(draft_dir)):
                    if f.lower().endswith(".gguf"):
                        candidates.append(os.path.join(draft_dir, f))
        except Exception:
            candidates = []
        if not candidates:
            return None
        candidates.sort(key=lambda p: (0 if "dspark" in os.path.basename(p).lower() else 1, p))
        return candidates[0]

    def _dspark_available(self, model_name_or_filename):
        return self._find_dspark_gguf(model_name_or_filename) is not None

    def _dspark_args(self, model_name):
        """Argumentos de DSpark (speculative decoding) para el modelo indicado."""
        if not self.defaults.get("dspark_enabled", True):
            return []
        draft_path = self._find_dspark_gguf(model_name)
        if not draft_path:
            return []
        if not self._flag_supported("--spec-type"):
            self.log("⚠️ DSpark: esta build de llama-server no soporta --spec-type; se omite.")
            return []

        gpu_draft = bool(self.defaults.get("gpu_for_dspark", False))
        args = ["-md", draft_path, "--spec-type", "draft-dspark"]

        n_max = self._safe_int(self.defaults.get("spec_draft_n_max", 3), 3)
        if n_max != 3:
            args.extend(["--spec-draft-n-max", str(n_max)])
        n_min = self._safe_int(self.defaults.get("spec_draft_n_min", 0), 0)
        if n_min > 0:
            args.extend(["--spec-draft-n-min", str(n_min)])

        type_k = str(self.defaults.get("spec_draft_type_k", "f16") or "f16")
        if type_k.lower() != "f16":
            args.extend(["-ctkd", type_k])
        type_v = str(self.defaults.get("spec_draft_type_v", "f16") or "f16")
        if type_v.lower() != "f16":
            args.extend(["-ctvd", type_v])

        if self._flag_supported("-ngld", "--n-gpu-layers-draft"):
            if gpu_draft:
                args.extend(["-ngld", self._normalize_ngl(self.defaults.get("n_gpu_layers_draft"), "auto")])
            else:
                args.extend(["-ngld", "0"])

        draft_threads = self.defaults.get("spec_draft_threads")
        if draft_threads not in (None, "", "None"):
            args.extend(["-td", str(draft_threads)])
        draft_threads_batch = self.defaults.get("spec_draft_threads_batch")
        if draft_threads_batch not in (None, "", "None"):
            args.extend(["-tbd", str(draft_threads_batch)])

        self.log(f"⚡ DSpark: {model_name} | GPU: {'sí' if gpu_draft else 'no'} | n_max: {n_max}")
        return args

    # ------------------------------------------------------------------
    # MoE
    # ------------------------------------------------------------------
    def _moe_args(self, model_name):
        """Argumentos de MoE (expertos en CPU/GPU) para el modelo indicado."""
        if not self.defaults.get("moe_enabled", False):
            return []
        if not self._is_moe_model(model_name):
            self.log(f"🧠 MoE: '{model_name}' no parece un modelo MoE; se omiten flags.")
            return []

        cpu_moe = bool(self.defaults.get("cpu_moe", False))
        n_cpu_moe = self._safe_int(self.defaults.get("n_cpu_moe", 0), 0)
        override = str(self.defaults.get("override_tensor", "") or "").strip()

        args = []
        if cpu_moe and self._flag_supported("-cmoe", "--cpu-moe"):
            args.append("-cmoe")
        elif n_cpu_moe > 0 and self._flag_supported("-ncmoe", "--n-cpu-moe"):
            args.extend(["-ncmoe", str(n_cpu_moe)])
        if override and self._flag_supported("-ot", "--override-tensor"):
            args.extend(["-ot", override])

        self.log(f"🧠 MoE: {model_name} | CPU-MoE: {'sí' if cpu_moe else 'no'} | ncmoe: {n_cpu_moe}")
        return args

    # ------------------------------------------------------------------
    # Resumen
    # ------------------------------------------------------------------
    def _log_llama_summary(self, ngl_value, model_name=None):
        """Loguea la configuración efectiva CPU/GPU de modelo, draft y expertos."""
        gpu_chat = bool(self.defaults.get("gpu_for_chat", False))
        gpu_dspark = bool(self.defaults.get("gpu_for_dspark", False))
        gpu_moe = bool(self.defaults.get("gpu_for_moe", False))
        self.log(
            f"📊 Modelo: {'GPU' if gpu_chat else 'CPU'} (ngl={ngl_value}) | "
            f"DSpark: {'GPU' if gpu_dspark else 'CPU'} | "
            f"MoE: {'GPU' if gpu_moe else 'CPU'}"
            + (f" | {model_name}" if model_name else "")
        )

    # ------------------------------------------------------------------
    # Etiquetas de modelos (DSpark / MoE / visual)
    # ------------------------------------------------------------------
    def _model_suffixes(self, model_filename, mmproj_exists=False):
        """Devuelve la lista de sufijos informativos para un modelo."""
        suffixes = []
        if mmproj_exists:
            suffixes.append("+✅visual-tool")
        try:
            if self._dspark_available(model_filename):
                suffixes.append("+⚡DSpark")
        except Exception:
            pass
        try:
            if self._is_moe_model(model_filename):
                suffixes.append("+🧠MoE")
        except Exception:
            pass
        return suffixes

    # ------------------------------------------------------------------
    # Proyector visual (mmproj)
    #
    # Antes vivía SOLO en el manager de escritorio: el panel web no cargaba
    # mmproj aunque tuviera `use_mmproj`/`mmproj_path` en sus defaults (claves
    # que no leía NADIE). Ahora la lógica es compartida y la única fuente de
    # verdad es `defaults["enable_multimodal"]`, así que ambos paneles arman el
    # mismo comando para el mismo modelo.
    # ------------------------------------------------------------------
    def multimodal_enabled(self) -> bool:
        """¿Se debe cargar el proyector visual (mmproj)?

        Con mmproj, llama.cpp DESACTIVA `--cache-reuse` (aviso: "cache_reuse is
        not supported by multimodal"), así que cada paso de AGENCY/PLAN
        reprocesa el prompt completo desde el token 0 y el modo agente se vuelve
        5-10× más lento en CPU pura. Desactivarlo si los agentes solo procesan
        texto, RAG o código.
        """
        return bool(self.defaults.get("enable_multimodal", True))

    def _mmproj_args(self, model_name_or_filename: str):
        """Argumentos `--mmproj` para el modelo, o [] si no hay proyector.

        Orden de búsqueda: carpeta por modelo (`data_dir/mmproj/<modelo>/`),
        luego `embed_dir` buscando el nombre del modelo, y por último la ruta
        explícita en `defaults["mmproj_path"]`.
        """
        model_basename = os.path.splitext(os.path.basename(model_name_or_filename))[0].lower()
        per_model_dir = os.path.join(self.data_dir, "mmproj", model_basename)
        try:
            os.makedirs(per_model_dir, exist_ok=True)
        except Exception:
            pass
        candidates = []
        try:
            if os.path.exists(per_model_dir):
                for f in sorted(os.listdir(per_model_dir)):
                    if f.lower().endswith('.gguf'):
                        candidates.append(os.path.join(per_model_dir, f))
        except Exception:
            candidates = []
        chosen = None
        if candidates:
            candidates.sort(key=lambda p: (0 if 'mmproj' in os.path.basename(p).lower() else 1, p))
            chosen = candidates[0]
        else:
            try:
                if os.path.exists(self.embed_dir):
                    for f in sorted(os.listdir(self.embed_dir)):
                        if f.lower().endswith('.gguf') and model_basename in f.lower():
                            chosen = os.path.join(self.embed_dir, f)
                            break
            except Exception:
                pass
        if not chosen:
            try:
                mmproj_path = self.defaults.get("mmproj_path", "")
                if mmproj_path and os.path.exists(mmproj_path):
                    chosen = mmproj_path
            except Exception:
                pass
        if chosen and os.path.exists(chosen):
            self.log(f"📷 Usando mmproj para {model_basename}: {chosen}")
            return ["--mmproj", chosen]
        self.log(f"⚠️ No se encontró mmproj para {model_basename}. Coloca el .gguf en: {per_model_dir}")
        return []

    def append_mmproj_args(self, cmd, model_name=None):
        """Añade `--mmproj` si el usuario lo permite y hay proyector para el modelo.

        Puerta ÚNICA compartida: el manager de escritorio y el panel web no
        pueden divergir en si cargan el proyector ni en el aviso que emiten.
        """
        if not model_name:
            return cmd
        if not self.multimodal_enabled():
            self.log("⏭️ mmproj desactivado (Configuración Básica) — cache_reuse ACTIVO")
            return cmd
        try:
            mm_args = self._mmproj_args(model_name)
            if mm_args:
                cmd.extend(mm_args)
                self.log("📷 mmproj habilitado — cache_reuse DESACTIVADO por llama.cpp (más lento en modo agente)")
            else:
                self.log("⚠️ mmproj solicitado pero no se encontró para este modelo")
        except Exception as error:
            self.log(f"Error resolviendo mmproj: {error}")
        return cmd
