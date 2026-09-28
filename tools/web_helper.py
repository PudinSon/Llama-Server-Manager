try:
    from pathlib import Path
    import sys
    _THIS_DIR = Path(__file__).resolve().parent
    _PROJECT_ROOT = _THIS_DIR.parent
    if str(_PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(_PROJECT_ROOT))
except Exception:
    pass

import logging
import time
import threading
from typing import Optional
import tools.runtime as runtime
runtime.add_app_site_packages()

logger = logging.getLogger("llama_manager.web_helper")

try:
    import requests
except Exception:
    requests = None

from http.server import BaseHTTPRequestHandler, HTTPServer
import socket
from threading import Thread
import webbrowser
try:
    from flask import Flask, render_template_string
except Exception:
    Flask = None


def _build_url(host: str, port: int, path: str = "/") -> str:
    use_host = host
    if host in ("0.0.0.0", "::"):
        use_host = "127.0.0.1"
    if not path.startswith("/"):
        path = "/" + path
    return f"http://{use_host}:{port}{path}"


def wait_for_server(host: str, port: int, path: str = "/", timeout: Optional[float] = 30.0, interval: float = 0.5, suppress_warnings: bool = False) -> bool:
    """Poll the server root URL until it responds.

    If `timeout` is None, wait indefinitely until the server responds.
    Returns True when server responds (status < 500). Returns False only
    if a finite timeout is provided and reached.
    """
    if requests is None:
        logger.warning("requests no está disponible; no se puede comprobar estado del servidor")
        return True
    url = _build_url(host, port, path)
    end = float('inf') if timeout is None else (time.time() + timeout)
    while time.time() < end:
        try:
            r = requests.get(url, timeout=2.0)
            if r.status_code < 500:
                logger.info(f"Server responsive at {url} (status={r.status_code})")
                return True
        except Exception:
            pass
        time.sleep(interval)
    # only warn about timeout if a finite timeout was provided
    if timeout is not None and not suppress_warnings:
        logger.warning(f"Timeout esperando servidor en {url}")
        return False
    # If we reached here with timeout=None, loop again (shouldn't happen), but return False defensively
    return False


def _open_embedded_via_flask(target_url: str, title: str = "llama-webui") -> Optional[str]:
    """Start a small Flask wrapper that embeds the target_url in an iframe.

    Returns the wrapper URL opened in the browser or None on failure.
    """
    if Flask is None:
        logger.warning("Flask no está disponible; no se puede abrir ventana embebida")
        # fallback: open target directly
        webbrowser.open_new_tab(target_url)
        return target_url

    # Find a free port for the wrapper
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    wrap_port = sock.getsockname()[1]
    sock.close()

    app = Flask(__name__)

    TEMPLATE = """
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>{{title}}</title>
      <style>html,body,iframe{height:100%;margin:0;border:0;}iframe{width:100%;}</style>
    </head>
    <body>
      <iframe src="{{target}}" frameborder="0" allowfullscreen></iframe>
    </body>
    </html>
    """

    @app.route('/')
    def index():
        return render_template_string(TEMPLATE, title=title, target=target_url)

    def run_app():
        try:
            app.run(host='127.0.0.1', port=wrap_port, debug=False, use_reloader=False)
        except Exception as e:
            logger.exception("Wrapper Flask error: %s", e)

    t = Thread(target=run_app, daemon=True)
    t.start()

    wrapper_url = f"http://127.0.0.1:{wrap_port}/"
    webbrowser.open_new_tab(wrapper_url)
    logger.info(f"Flask wrapper abierto en {wrapper_url} apuntando a {target_url}")
    return wrapper_url


def _open_headless_with_pyppeteer(url: str):
    # Try to start a persistent headless runner as a subprocess (keeps Chromium alive)
    try:
        import sys, subprocess, os
        py = sys.executable
        runner = runtime.resource_path('headless_runner.py')
        if not os.path.exists(runner):
            logger.info("headless_runner.py no encontrado; instalar pyppeteer o crear el runner")
            return False
        # Launch detached subprocess that will keep the headless browser alive until terminated
        try:
            p = subprocess.Popen([py, runner, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=(subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0))
        except Exception:
            p = subprocess.Popen([py, runner, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info("Headless runner iniciado (pid=%s) para %s", getattr(p, 'pid', '?'), url)
        return True
    except Exception as e:
        logger.exception("Error iniciando headless runner: %s", e)
        return False


def wait_and_open(host: str, port: int, path: str = "/", prefer_headless: bool = True, timeout: float = 30.0):
    url = _build_url(host, port, path)
    ok = wait_for_server(host, port, path, timeout=timeout)
    if not ok:
        logger.warning("El servidor no respondió a tiempo; no se abrirá la UI")
        return None

    if prefer_headless:
        try:
            opened = _open_headless_with_pyppeteer(url)
            if opened:
                return url
        except Exception:
            pass

    # Fallback: open embedded via Flask wrapper (or direct browser if Flask missing)
    _open_embedded_via_flask(url)
    return url