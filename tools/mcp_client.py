# tools/mcp_client.py
"""Cliente MCP HTTP para el servidor de dev_agent (http://127.0.0.1:15000/mcp).

El servidor FastMCP expone EXACTAMENTE dos herramientas — `query_tools` y
`execute_tool` — y es el mismo punto que usa la Web UI. El agente (core/CLI)
ejecuta TODAS sus herramientas a través de este cliente, en vez de importar
dev_agent en proceso. Eso evita los problemas del mecanismo anterior:
nombres duplicados query/execute, y consultas de descubrimiento vacías.

Protocolo: JSON-RPC 2.0 sobre HTTP (Streamable HTTP). El servidor asigna un
`Mcp-Session-Id` en la respuesta de `initialize`; las llamadas siguientes lo
envían en el header. Las respuestas pueden venir como SSE o JSON plano.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger("mcp_client")

MCP_BASE_URL = os.environ.get("MCP_BASE_URL", "http://127.0.0.1:15000").rstrip("/")
MCP_PATH = os.environ.get("MCP_PATH", "/mcp")
MCP_ENDPOINT = f"{MCP_BASE_URL}{MCP_PATH}"
MCP_TIMEOUT = float(os.environ.get("MCP_TIMEOUT", "60"))

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

_PROTOCOL_VERSION = "2024-11-05"

# Esquemas estáticos de respaldo por si el servidor MCP no responde
# (list_tools lo reemplaza con los esquemas reales cuando está disponible).
_STATIC_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "query_tools",
        "description": (
            "Descubre qué herramienta interna ejecutar para una tarea. Busca en el "
            "catálogo de herramientas (list_directory, read_file, "
            "write_file, exec_shell_command, etc.) y devuelve las más relevantes "
            "con sus argumentos. ÚSALA cuando no sepas qué herramienta ejecutar o "
            "no la veas disponible. "
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "La tarea o consulta en lenguaje natural.",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "execute_tool",
        "description": (
            "Ejecuta una herramienta interna por nombre (p.ej. list_directory, "
            "read_file, write_file, exec_shell_command, sqlite_query, "
            "rag_search). Argumentos: tool_name (nombre exacto) y arguments "
            "(dict de argumentos). Si no sabes el nombre exacto, llama primero a "
            "query_tools para descubrirlo."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": "Nombre exacto de la herramienta interna.",
                },
                "arguments": {
                    "type": "object",
                    "description": "Argumentos de la herramienta.",
                },
            },
            "required": ["tool_name"],
        },
    },
]


def _parse_sse_or_json(resp: Any) -> Any:
    """Extrae el mensaje JSON-RPC de una respuesta SSE o JSON plano."""
    ctype = resp.headers.get("Content-Type", "")
    body = resp.text or ""
    if "text/event-stream" in ctype or body.lstrip().startswith("event:"):
        data = None
        for block in body.split("\n\n"):
            for line in block.splitlines():
                if line.startswith("data:"):
                    try:
                        data = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
        if data is None:
            raise RuntimeError(f"Respuesta SSE sin data JSON-RPC: {body[:200]}")
        return data
    return resp.json()


class MCPClient:
    """Cliente JSON-RPC minimalista para el servidor MCP de dev_agent."""

    def __init__(self, endpoint: Optional[str] = None, timeout: float = MCP_TIMEOUT):
        self.endpoint = endpoint or MCP_ENDPOINT
        self.timeout = timeout
        self._session = requests.Session() if requests is not None else None
        self._session_id: Optional[str] = None
        self._request_id = 0
        self._initialized = False
        self._tools_cache: Optional[List[Dict[str, Any]]] = None

    # ------------------------------------------------------------------
    # Bajo nivel
    # ------------------------------------------------------------------
    def _post(self, payload: Dict[str, Any], timeout: Optional[float] = None) -> Any:
        headers = {"Accept": "application/json, text/event-stream"}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        resp = self._session.post(
            self.endpoint,
            json=payload,
            headers=headers,
            timeout=timeout or (5, self.timeout),
        )
        resp.raise_for_status()
        if not self._session_id:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self._session_id = sid
        return _parse_sse_or_json(resp)

    def _rpc(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Dict[str, Any]:
        self._request_id += 1
        msg = {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params or {}}
        data = self._post(msg, timeout=timeout)
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            raise RuntimeError(f"MCP {method} error: {err.get('message', err)}")
        return data

    def initialize(self) -> None:
        if self._initialized:
            return
        self._rpc("initialize", {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "agent-core", "version": "3.0"},
        })
        self._initialized = True
        # Notificación (sin id) para confirmar la sesión; errores no fatales.
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=10)
        except Exception as e:
            logger.debug(f"notifications/initialized falló (no crítico): {e}")

    def reset_session(self) -> None:
        self._session_id = None
        self._initialized = False
        try:
            self._session.close()
        except Exception:
            pass
        if requests is not None:
            self._session = requests.Session()

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------
    def list_tools(self, use_cache: bool = True) -> List[Dict[str, Any]]:
        if use_cache and self._tools_cache is not None:
            return self._tools_cache
        self.initialize()
        data = self._rpc("tools/list")
        result = data.get("result", {}) if isinstance(data, dict) else {}
        tools = result.get("tools", []) if isinstance(result, dict) else []
        self._tools_cache = tools or None
        return tools or []

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> str:
        """Llama a una herramienta del MCP y devuelve su texto de resultado.

        Devuelve un string SIEMPRE (nunca lanza): los errores se devuelven
        como 'Error: ...' para que el agente los detecte con sus heurísticas.
        """
        try:
            self.initialize()
            data = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        except Exception as e:
            # Sesión expirada/caída del servidor: reintentar una vez con sesión nueva.
            try:
                self.reset_session()
                self.initialize()
                data = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
            except Exception as e2:
                return f"Error: MCP no disponible en {self.endpoint} ({name}): {e2}"
        result = data.get("result", {}) if isinstance(data, dict) else {}
        if not isinstance(result, dict):
            return str(result)
        if result.get("isError"):
            return f"Error: {_extract_text(result)}"
        return _extract_text(result)

    def query_tools(self, query: str) -> str:
        """Descubrimiento semántico: devuelve las herramientas relevantes."""
        return self.call_tool("query_tools", {"query": query or ""})

    def execute_tool(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> str:
        """Ejecuta una herramienta interna por nombre vía el MCP."""
        return self.call_tool("execute_tool", {"tool_name": tool_name, "arguments": arguments or {}})


def _extract_text(result: Dict[str, Any]) -> str:
    content = result.get("content", []) if isinstance(result, dict) else []
    texts = []
    for c in content:
        if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
            texts.append(str(c["text"]))
        elif isinstance(c, dict) and c.get("text"):
            texts.append(str(c["text"]))
    if texts:
        return "\n".join(texts)
    try:
        return json.dumps(result, ensure_ascii=False)
    except Exception:
        return str(result)


# ---------------------------------------------------------------------------
# Singleton + funciones de módulo (compatibles con el uso del core)
# ---------------------------------------------------------------------------
_client: Optional[MCPClient] = None
_client_lock = threading.Lock()


def get_client() -> MCPClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = MCPClient()
    return _client


def mcp_available() -> bool:
    """True si el endpoint MCP responde (best-effort, con caché)."""
    try:
        tools = get_client().list_tools(use_cache=True)
        return bool(tools)
    except Exception:
        return False


def query_tools(query: str) -> str:
    """Versión de módulo de query_tools (descubrimiento de herramientas)."""
    try:
        return get_client().query_tools(query)
    except Exception as e:
        return f"Error: MCP query_tools no disponible: {e}"


def execute_tool(tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> str:
    """Versión de módulo de execute_tool (ejecución vía MCP)."""
    try:
        return get_client().execute_tool(tool_name, arguments)
    except Exception as e:
        return f"Error: MCP execute_tool no disponible: {e}"


def list_tool_names() -> List[str]:
    try:
        return [t.get("name", "") for t in get_client().list_tools() if t.get("name")]
    except Exception:
        return ["query_tools", "execute_tool"]


def get_openai_tools() -> List[Dict[str, Any]]:
    """Devuelve los esquemas OpenAI de las 2 herramientas MCP (query_tools y
    execute_tool), tomados del propio servidor si responde (como los ve la
    Web UI), con respaldo estático si está caído."""
    try:
        tools = get_client().list_tools(use_cache=True)
        if tools:
            out = []
            for t in tools:
                name = t.get("name", "")
                if not name:
                    continue
                schema = t.get("inputSchema") or {"type": "object", "properties": {}, "required": []}
                out.append({
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": t.get("description", ""),
                        "parameters": schema,
                    },
                })
            if out:
                return out
    except Exception as e:
        logger.debug(f"get_openai_tools: MCP no respondió, usando esquema estático ({e})")
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["inputSchema"],
            },
        }
        for t in _STATIC_TOOLS
    ]
