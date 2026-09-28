"""Deteccion portable de disponibilidad de entorno grafico."""

from __future__ import annotations

import os
import sys


def has_gui_environment() -> bool:
    """Devuelve True si es posible crear una GUI local."""
    if sys.platform.startswith("linux"):
        if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            return False
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        root.update_idletasks()
        root.destroy()
        return True
    except Exception:
        return False


def get_environment_info() -> dict:
    return {
        "platform": sys.platform,
        "has_gui": has_gui_environment(),
        "display": os.environ.get("DISPLAY"),
        "wayland_display": os.environ.get("WAYLAND_DISPLAY"),
        "xdg_session_type": os.environ.get("XDG_SESSION_TYPE"),
        "ssh_connection": bool(os.environ.get("SSH_CONNECTION")),
    }


if __name__ == "__main__":
    print(get_environment_info())
