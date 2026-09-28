# tools/web_viewer.py
"""
Visor simple de la WebUI basado en wxPython + WebView2.

Historia: antes era un visor con *split view* (chat + panel RAG) y varios
parches de JavaScript inyectados (botones, atajos, prompts MCP). Todo eso se
eliminó: en la UI nativa Svelte la consola, el RAG y el resto de paneles ya
existen como componentes, así que el visor sólo hace falta para mostrar el chat.

Lo único que se conserva del JS inyectado es el permiso de micrófono
(WebView2 no muestra el prompt de permiso y sin ese shim las llamadas de voz
fallan dentro del visor).
"""

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
import os
import platform
import sys
import webbrowser

import tools.runtime as runtime
import wx
import wx.html2

logger = logging.getLogger("llama_manager.web_viewer")
if not logging.getLogger().hasHandlers():
    logging.basicConfig(level=logging.INFO)
logger.setLevel(logging.INFO)

#: JS mínimo: concede permiso de micrófono/cámara dentro del WebView2.
_MIC_PERMISSIONS_JS = r"""
(function () {
    if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
        var original = navigator.mediaDevices.getUserMedia;
        navigator.mediaDevices.getUserMedia = function (constraints) {
            return original.call(this, constraints).catch(function (err) {
                console.warn('[Viewer] getUserMedia fallo, reintentando:', err);
                return original.call(this, constraints);
            });
        };
    }
    if (navigator.permissions && navigator.permissions.query) {
        var origQuery = navigator.permissions.query;
        navigator.permissions.query = function (desc) {
            if (desc && (desc.name === 'microphone' || desc.name === 'camera')) {
                return Promise.resolve({ state: 'granted', onchange: null });
            }
            return origQuery.call(this, desc);
        };
    }
    console.log('[Viewer] Permisos de microfono preparados');
})();
"""


def _setup_fixed_webview2() -> bool:
    """Usa la versión Fixed de WebView2 del SDK local si está disponible."""
    try:
        base_dir = runtime.get_base_dir()
        sdk_root = base_dir / "app_data" / "edgewv2sdk"
        if not sdk_root.exists():
            logger.warning("edgewv2sdk no encontrada. Usando WebView2 del sistema.")
            return False

        machine = platform.machine().lower()
        if machine in ("amd64", "x86_64"):
            arch = "x64"
        elif machine == "x86":
            arch = "x86"
        else:
            logger.warning(f"Arquitectura no soportada: {machine}. Usando WebView2 del sistema.")
            return False

        runtime_path = sdk_root / arch
        if not runtime_path.exists():
            logger.warning(f"Subcarpeta {arch} no encontrada. Usando WebView2 del sistema.")
            return False

        os.environ["WEBVIEW2_BROWSER_EXECUTABLE_FOLDER"] = str(runtime_path)
        logger.info(f"WebView2 Fixed Version configurado en: {runtime_path}")
        return True
    except Exception as e:
        logger.exception(f"Error configurando WebView2 Fixed: {e}")
        return False


class MainFrame(wx.Frame):
    """Ventana única con el chat de la WebUI (sin split view ni inyecciones)."""

    def __init__(self, chat_url: str, profile_dir: str = None):
        super().__init__(None, title="Llama Server Viewer", size=(1200, 800),
                         style=wx.DEFAULT_FRAME_STYLE)

        self.chat_url = chat_url
        self.saved_chat_url = chat_url
        self.profile_dir = profile_dir
        self.permissions_injected = False

        logger.info("Iniciando visor simple...")

        if profile_dir:
            try:
                os.makedirs(profile_dir, exist_ok=True)
                os.environ["WEBVIEW2_USER_DATA_FOLDER"] = profile_dir
                os.environ["WEBVIEW2_CACHE_PATH"] = profile_dir
                logger.info(f"Persistencia en: {profile_dir}")
            except Exception as e:
                logger.warning(f"No se pudo crear carpeta de perfil: {e}")

        try:
            icon_path = runtime.resource_path("app.ico")
            if os.path.exists(icon_path):
                icon = wx.Icon()
                icon.CopyFromBitmap(wx.Bitmap(icon_path, wx.BITMAP_TYPE_ICO))
                self.SetIcon(icon)
        except Exception as e:
            logger.warning(f"No se pudo cargar icono: {e}")

        self.chat_webview = wx.html2.WebView.New(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.chat_webview, 1, wx.EXPAND)
        self.SetSizer(sizer)

        self.chat_webview.Bind(wx.html2.EVT_WEBVIEW_NAVIGATING, self.on_chat_navigating)
        try:
            self.chat_webview.Bind(wx.html2.EVT_WEBVIEW_LOADED, self.on_chat_loaded)
        except Exception:
            pass
        self.Bind(wx.EVT_CLOSE, self.on_close)

        self.chat_webview.LoadURL(chat_url)

        logger.info("Visor simple iniciado")
        self.Show()

    # ------------------------------------------------------------------
    # Eventos
    # ------------------------------------------------------------------
    def on_chat_navigating(self, event):
        url = event.GetURL()
        logger.info(f"Navegando a: {url}")
        if "localhost:8081" in url or "127.0.0.1:8081" in url:
            self.saved_chat_url = url
        event.Skip()

    def on_chat_loaded(self, event):
        """Reinyecta el permiso de micrófono al cargar cada página del chat."""
        try:
            self.chat_webview.RunScript(_MIC_PERMISSIONS_JS)
            self.permissions_injected = True
        except Exception as e:
            logger.debug(f"No se pudo inyectar el permiso de micrófono: {e}")
        event.Skip()

    def on_close(self, event):
        logger.info("Cerrando visor...")
        event.Skip()


def run_qt_viewer(url: str, profile_dir: str = None) -> int:
    """Nombre conservado por compatibilidad: abre el visor wxPython."""
    try:
        _setup_fixed_webview2()
        app = wx.App()
        MainFrame(url, profile_dir)
        app.MainLoop()
        return 0
    except Exception as e:
        logger.exception(f"Error en visor wxPython: {e}")
        webbrowser.open_new_tab(url)
        return 1


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "--run-web-viewer":
        args = args[1:]
    if len(args) < 1:
        print("Usage: web_viewer.py <url> [profile_dir]")
        return 1
    url = args[0]
    profile_dir = args[1] if len(args) > 1 else None
    return run_qt_viewer(url, profile_dir)


if __name__ == "__main__":
    raise SystemExit(main())
