"""OAuth de Google y credenciales privadas para la integración Gmail 24/7."""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import requests

try:
    from . import runtime
except ImportError:  # pragma: no cover - ejecución suelta
    import runtime  # type: ignore

logger = logging.getLogger("google_mail")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:15001/automation/gmail/oauth/callback"
GOOGLE_FIELDS = {
    "gmail_client_id",
    "gmail_client_secret",
    "gmail_refresh_token",
    "gmail_access_token",
    "gmail_access_token_expiry",
    "gmail_address",
    "gmail_enabled",
    "gmail_search",
    "gmail_poll_interval",
    "gmail_mark_seen",
    "gmail_max_per_poll",
    "gmail_wake_prompt",
    "gmail_agent_template",
}

_config_lock = threading.RLock()
_state_lock = threading.Lock()
_pending_states: Dict[str, tuple[str, float]] = {}


def _config_path() -> Path:
    return Path(runtime.get_user_data_dir()) / "config.json"


def _read_defaults() -> Dict[str, Any]:
    path = _config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        logger.exception("No se pudo leer la configuración de Google")
        return {}
    if not isinstance(data, dict):
        return {}
    defaults = data.get("defaults")
    return defaults if isinstance(defaults, dict) else {}


def save_google_defaults(updates: Dict[str, Any]) -> Dict[str, Any]:
    unknown = set(updates) - GOOGLE_FIELDS
    if unknown:
        raise ValueError(f"campos Google no permitidos: {', '.join(sorted(unknown))}")
    path = _config_path()
    with _config_lock:
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"no se pudo leer config.json: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("config.json debe contener un objeto JSON")
        defaults = data.get("defaults")
        if not isinstance(defaults, dict):
            defaults = {}
            data["defaults"] = defaults
        defaults.update(updates)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
        return dict(defaults)


def public_config(defaults: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    data = _read_defaults() if defaults is None else defaults
    return {
        "enabled": bool(data.get("gmail_enabled", False)),
        "connected": bool(data.get("gmail_refresh_token")),
        "address": str(data.get("gmail_address") or ""),
        "client_id": str(data.get("gmail_client_id") or ""),
        "has_client_secret": bool(data.get("gmail_client_secret")),
        "redirect_uri": DEFAULT_REDIRECT_URI,
        "search": str(data.get("gmail_search") or "is:unread"),
        "poll_interval": int(data.get("gmail_poll_interval") or 60),
        "mark_seen": bool(data.get("gmail_mark_seen", True)),
        "max_per_poll": int(data.get("gmail_max_per_poll") or 10),
        "wake_prompt": str(data.get("gmail_wake_prompt") or ""),
        "agent_template": str(data.get("gmail_agent_template") or ""),
    }


def begin_authorization(client_id: str, client_secret: str) -> Dict[str, str]:
    defaults = _read_defaults()
    client_id = str(client_id or defaults.get("gmail_client_id") or "").strip()
    client_secret = str(client_secret or defaults.get("gmail_client_secret") or "").strip()
    if not client_id or not client_secret:
        raise ValueError("Configura el OAuth Client ID y el Client Secret de Google")
    defaults = save_google_defaults({
        "gmail_client_id": client_id,
        "gmail_client_secret": client_secret,
    })
    state = secrets.token_urlsafe(32)
    with _state_lock:
        now = time.time()
        for expired, (_, deadline) in list(_pending_states.items()):
            if deadline <= now:
                _pending_states.pop(expired, None)
        _pending_states[state] = (DEFAULT_REDIRECT_URI, now + 600)

    query = urlencode({
        "client_id": defaults["gmail_client_id"],
        "redirect_uri": DEFAULT_REDIRECT_URI,
        "response_type": "code",
        "scope": GMAIL_SCOPE,
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
    })
    return {"authorization_url": f"{AUTH_URL}?{query}", "redirect_uri": DEFAULT_REDIRECT_URI}


def complete_authorization(code: str, state: str) -> Dict[str, Any]:
    with _state_lock:
        pending = _pending_states.pop(str(state or ""), None)
    if not pending or pending[1] <= time.time():
        raise ValueError("La solicitud OAuth venció o no coincide; vuelve a vincular la cuenta")
    if not code:
        raise ValueError("Google no devolvió un código de autorización")

    defaults = _read_defaults()
    client_id = str(defaults.get("gmail_client_id") or "")
    client_secret = str(defaults.get("gmail_client_secret") or "")
    if not client_id or not client_secret:
        raise ValueError("Falta la configuración OAuth de Google")

    response = requests.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": pending[0],
            "grant_type": "authorization_code",
        },
        timeout=20,
    )
    response.raise_for_status()
    token_data = response.json()
    access_token = str(token_data.get("access_token") or "")
    if not access_token:
        raise ValueError("Google no devolvió un token de acceso")

    profile_response = requests.get(
        PROFILE_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    profile_response.raise_for_status()
    email_address = str(profile_response.json().get("emailAddress") or "")
    refresh_token = str(token_data.get("refresh_token") or defaults.get("gmail_refresh_token") or "")
    if not refresh_token:
        raise ValueError(
            "Google no entregó un refresh token. Revoca el acceso de la app en Google y vuelve a autorizar."
        )

    expiry = time.time() + max(60, int(token_data.get("expires_in") or 3600))
    save_google_defaults({
        "gmail_access_token": access_token,
        "gmail_access_token_expiry": expiry,
        "gmail_refresh_token": refresh_token,
        "gmail_address": email_address,
        "gmail_enabled": True,
    })
    return {"email": email_address}


def access_token() -> str:
    defaults = _read_defaults()
    token = str(defaults.get("gmail_access_token") or "")
    expiry = float(defaults.get("gmail_access_token_expiry") or 0)
    if token and expiry > time.time() + 60:
        return token

    refresh_token = str(defaults.get("gmail_refresh_token") or "")
    client_id = str(defaults.get("gmail_client_id") or "")
    client_secret = str(defaults.get("gmail_client_secret") or "")
    if not refresh_token or not client_id or not client_secret:
        raise ValueError("Vincula una cuenta Google antes de sondear Gmail")

    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=20,
    )
    response.raise_for_status()
    token_data = response.json()
    token = str(token_data.get("access_token") or "")
    if not token:
        raise ValueError("Google no renovó el token de acceso")
    save_google_defaults({
        "gmail_access_token": token,
        "gmail_access_token_expiry": time.time() + max(60, int(token_data.get("expires_in") or 3600)),
    })
    return token


def disconnect() -> None:
    refresh_token = str(_read_defaults().get("gmail_refresh_token") or "")
    if refresh_token:
        try:
            requests.post(REVOKE_URL, data={"token": refresh_token}, timeout=10)
        except requests.RequestException as exc:
            logger.info("No se pudo revocar Gmail en línea; se limpiará el token local: %s", exc)
    save_google_defaults({
        "gmail_enabled": False,
        "gmail_refresh_token": "",
        "gmail_access_token": "",
        "gmail_access_token_expiry": 0,
        "gmail_address": "",
    })