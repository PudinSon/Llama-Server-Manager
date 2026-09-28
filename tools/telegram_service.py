"""
Telegram Bot Service Integration
Proporciona un bot de Telegram que actúa como cliente del chatbot llama.cpp

Features:
- Chat privado con historial
- Chat grupal (responde cuando es mencionado)
- Procesamiento de archivos adjuntos (documentos, imágenes, audio)
- Comandos (/model, /reset, /status, /help, /pairing)
- Sistema de pairing para control de acceso
- Integración con llama-server (puerto 8081)
"""

import os
import json
import logging
import asyncio
import threading
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional, Any
from pathlib import Path

# Importaciones de Telegram
try:
    from telegram import Update, Chat
    from telegram.ext import (
        Application, CommandHandler, MessageHandler, 
        filters, ContextTypes, ConversationHandler
    )
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    logging.warning("python-telegram-bot no está instalado. Instala con: pip install python-telegram-bot")

import requests
import logging

# Importaciones locales
try:
    from . import runtime
except ImportError:
    try:
        import runtime
    except ImportError:
        class _DummyRuntime:
            @staticmethod
            def get_data_dir():
                return os.path.join(os.getcwd(), "app_data")
            @staticmethod
            def get_user_data_dir():
                return os.path.join(os.getcwd(), "user_data")
        runtime = _DummyRuntime()

logger = logging.getLogger("telegram_service")

# ============================================================================
# CONFIGURACIÓN
# ============================================================================

TELEGRAM_DB = Path(runtime.get_user_data_dir()) / "telegram_history.db"
TELEGRAM_CONFIG_KEY = "telegram"
LLAMA_SERVER_URL = "http://localhost:8081"
LLAMA_CHAT_ENDPOINT = f"{LLAMA_SERVER_URL}/v1/chat/completions"
LLAMA_TRANSCRIBE_ENDPOINT = f"{LLAMA_SERVER_URL}/v1/audio/transcriptions"


def _get_automation_dispatcher():
    """Dispatcher de automatización (lazy: no debe romper el import del bot)."""
    try:
        from .automation_dispatcher import get_dispatcher
        return get_dispatcher()
    except Exception:
        try:
            from tools.automation_dispatcher import get_dispatcher  # type: ignore
            return get_dispatcher()
        except Exception:
            return None


def _get_shared_system_prompt() -> str:
    """Obtiene el prompt compartido del entorno/configuración del Manager."""
    prompt = os.environ.get("LLAMA_SYSTEM_PROMPT", "").strip()
    if prompt:
        return prompt
    try:
        config_path = Path(runtime.get_user_data_dir()) / "config.json"
        data = json.loads(config_path.read_text(encoding="utf-8"))
        defaults = data.get("defaults", data) if isinstance(data, dict) else {}
        return str(defaults.get("telegram_system_prompt", "")).strip()
    except Exception:
        return ""

# ============================================================================
# BASE DE DATOS DE SESIONES
# ============================================================================

class TelegramSessionDB:
    """Gestor de base de datos para sesiones de Telegram."""
    
    def __init__(self, db_path: Path = TELEGRAM_DB):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
    
    def _init_db(self):
        """Inicializa la base de datos."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                chat_id TEXT PRIMARY KEY,
                messages TEXT,
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pairing_requests (
                user_id TEXT PRIMARY KEY,
                code TEXT,
                requested_at TIMESTAMP,
                status TEXT DEFAULT 'pending'
            )
        """)
        
        conn.commit()
        conn.close()
    
    def get_session(self, chat_id: str) -> List[Dict]:
        """Obtiene el historial de una sesión."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT messages FROM sessions WHERE chat_id = ?", (str(chat_id),))
        result = cursor.fetchone()
        conn.close()
        
        if result:
            return json.loads(result[0])
        return []
    
    def save_session(self, chat_id: str, messages: List[Dict]):
        """Guarda el historial de una sesión."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        messages_json = json.dumps(messages, ensure_ascii=False)
        now = datetime.now().isoformat()
        
        cursor.execute("""
            INSERT OR REPLACE INTO sessions (chat_id, messages, created_at, updated_at)
            VALUES (?, ?, ?, ?)
        """, (str(chat_id), messages_json, now, now))
        
        conn.commit()
        conn.close()
    
    def clear_session(self, chat_id: str):
        """Limpia el historial de una sesión."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM sessions WHERE chat_id = ?", (str(chat_id),))
        conn.commit()
        conn.close()
    
    def create_pairing_request(self, user_id: str, code: str):
        """Crea una solicitud de pairing."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        
        cursor.execute("""
            INSERT OR REPLACE INTO pairing_requests (user_id, code, requested_at, status)
            VALUES (?, ?, ?, ?)
        """, (str(user_id), code, now, 'pending'))
        
        conn.commit()
        conn.close()
    
    def get_pairing_requests(self) -> List[Dict]:
        """Obtiene solicitudes de pairing pendientes."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, code, requested_at FROM pairing_requests WHERE status = 'pending'")
        results = cursor.fetchall()
        conn.close()
        
        return [{"user_id": r[0], "code": r[1], "requested_at": r[2]} for r in results]

    def approve_pairing_code(self, code: str) -> Optional[str]:
        """Aprueba un código pendiente y devuelve el Telegram user_id asociado."""
        normalized_code = str(code or "").strip().upper()
        if not normalized_code:
            return None

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT user_id FROM pairing_requests WHERE code = ? AND status = 'pending'",
            (normalized_code,),
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return None

        cursor.execute(
            "UPDATE pairing_requests SET status = 'approved' WHERE code = ? AND status = 'pending'",
            (normalized_code,),
        )
        conn.commit()
        conn.close()
        return str(row[0]).strip()

# ============================================================================
# SERVICIO TELEGRAM
# ============================================================================

class TelegramBotService:
    """Servicio principal del bot de Telegram."""
    
    def __init__(self, token: str, allowed_users: List[str], pairing_required: bool = True):
        self.token = token
        self.allowed_users = {str(user_id).strip() for user_id in (allowed_users or []) if str(user_id).strip()}
        self.pairing_required = pairing_required
        self.db = TelegramSessionDB()
        self.app = None
        self.is_running = False
    
    async def start_bot(self):
        """Inicia el bot de Telegram."""
        if not TELEGRAM_AVAILABLE:
            logger.error("Telegram bot no disponible. Instala python-telegram-bot.")
            return False
        
        try:
            self.app = Application.builder().token(self.token).build()
            
            # Registrar handlers
            self.app.add_handler(CommandHandler("start", self._handle_start))
            self.app.add_handler(CommandHandler("help", self._handle_help))
            self.app.add_handler(CommandHandler("reset", self._handle_reset))
            self.app.add_handler(CommandHandler("model", self._handle_model))
            self.app.add_handler(CommandHandler("status", self._handle_status))
            self.app.add_handler(CommandHandler("pairing", self._handle_pairing))
            # Automatización 24/7: consultar la cola y aprobar/rechazar por chat.
            self.app.add_handler(CommandHandler("cola", self._handle_cola))
            self.app.add_handler(CommandHandler("tareas", self._handle_tareas))
            self.app.add_handler(CommandHandler("aprobar", self._handle_aprobar))
            self.app.add_handler(CommandHandler("rechazar", self._handle_rechazar))

            self.app.add_handler(MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._handle_message
            ))
            
            self.is_running = True
            logger.info("✅ Bot de Telegram iniciado.")
            
            # Iniciar polling
            await self.app.initialize()
            await self.app.start()
            await self.app.updater.start_polling()
            
            return True
        except Exception as e:
            logger.error(f"❌ Error iniciando bot de Telegram: {e}")
            return False
    
    async def stop_bot(self):
        """Detiene el bot de Telegram."""
        if self.app:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
            self.is_running = False
            logger.info("🛑 Bot de Telegram detenido.")
    
    def _is_authorized(self, user_id: str) -> bool:
        """Verifica si el usuario está autorizado."""
        if not self.pairing_required:
            return True
        return str(user_id) in self.allowed_users
    
    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja el comando /start."""
        user_id = str(update.effective_user.id)

        if not self._is_authorized(user_id):
            # Generar código de pairing automáticamente al iniciar
            import random
            import string
            code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
            self.db.create_pairing_request(user_id, code)

            await update.message.reply_text(
                "🤖 ¡Hola! Soy un asistente de IA.\n\n"
                "🔐 Para empezar, necesito que un administrador autorice tu acceso.\n\n"
                f"📋 Tu código de pairing es: `{code}`\n\n"
                "Comparte este código con el administrador desde la sección "
                "Telegram del panel. Una vez aprobado, podrás usar el bot."
            )
            return

        await update.message.reply_text(
            "🤖 ¡Hola! Soy un asistente de IA.\n\n"
            "Puedo ayudarte con:\n"
            "- Chat y responder preguntas\n"
            "- Procesar documentos, imágenes y audio\n"
            "- Ejecutar comandos\n\n"
            "Escribe /help para ver comandos disponibles."
        )
    
    async def _handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja el comando /help."""
        help_text = """
        📋 Comandos disponibles:

        /start - Mensaje de bienvenida
        /help - Esta ayuda
        /reset - Reiniciar conversación
        /model - Ver/cambiar modelo
        /status - Estado del servidor
        /pairing - Código de emparejamiento
        /cola - Resumen de la automatización 24/7
        /tareas - Aprobaciones pendientes (con su token)
        /aprobar <token> - Aprueba una tarea de la cola
        /rechazar <token> - Rechaza una tarea de la cola

        Envía mensajes, documentos, imágenes o audio para interactuar.
        """
        await update.message.reply_text(help_text)
    
    async def _handle_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja el comando /reset."""
        chat_id = update.effective_chat.id
        self.db.clear_session(str(chat_id))
        await update.message.reply_text("🔄 Conversación reiniciada.")
    
    async def _handle_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja el comando /model."""
        await update.message.reply_text("🔧 Cambio de modelo - No implementado en esta versión.")
    
    async def _handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja el comando /status."""
        try:
            # Verificar si llama-server está disponible
            response = requests.get(f"{LLAMA_SERVER_URL}/health", timeout=2)
            if response.status_code == 200:
                status_text = "✅ Servidor conectado y funcionando correctamente."
            else:
                status_text = "⚠️ Servidor respondiendo pero con problemas."
        except:
            status_text = "❌ Servidor no disponible."
        
        await update.message.reply_text(f"📊 Estado del servidor:\n{status_text}")
    
    async def _handle_pairing(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja el comando /pairing."""
        user_id = update.effective_user.id
        
        if self._is_authorized(str(user_id)):
            await update.message.reply_text("✅ Ya estás autorizado.")
        else:
            # Generar código de pairing
            import random
            import string
            code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
            self.db.create_pairing_request(str(user_id), code)
            
            await update.message.reply_text(
                f"🔐 Código de pairing: {code}\n\n"
                f"Comparte este código con el administrador para obtener acceso."
            )
    
    async def _handle_cola(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Resumen de la cola de automatización."""
        if not self._is_authorized(str(update.effective_user.id)):
            await update.message.reply_text("🔒 No tienes permiso.")
            return
        dispatcher = _get_automation_dispatcher()
        if dispatcher is None:
            await update.message.reply_text("❌ La automatización no está disponible.")
            return
        stats = dispatcher.queue.stats()
        await update.message.reply_text(
            "🤖 Automatización 24/7\n"
            f"• Pendientes: {stats.get('pending', 0)}\n"
            f"• En curso: {stats.get('running', 0)}\n"
            f"• Aprobación: {stats.get('awaiting_approval', 0)}\n"
            f"• Hechas: {stats.get('done', 0)}\n"
            f"• Fallidas: {stats.get('failed', 0)}"
        )

    async def _handle_tareas(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Lista las tareas esperando aprobación, con su token."""
        if not self._is_authorized(str(update.effective_user.id)):
            await update.message.reply_text("🔒 No tienes permiso.")
            return
        dispatcher = _get_automation_dispatcher()
        if dispatcher is None:
            await update.message.reply_text("❌ La automatización no está disponible.")
            return
        tasks = dispatcher.queue.list_tasks(status="awaiting_approval", limit=10)
        if not tasks:
            await update.message.reply_text("✅ No hay aprobaciones pendientes.")
            return
        lines = ["🔐 Aprobaciones pendientes:"]
        for task in tasks:
            label = str(task.get("wake_prompt") or task.get("source") or "evento")[:60]
            lines.append(f"• #{task['id']} {label}\n  /aprobar {task.get('approval_token')}")
        await update.message.reply_text("\n".join(lines))

    async def _handle_aprobar(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Aprueba la tarea indicada por token."""
        if not self._is_authorized(str(update.effective_user.id)):
            await update.message.reply_text("🔒 No tienes permiso.")
            return
        token = (context.args[0] if context.args else "").strip()
        if not token:
            await update.message.reply_text("Uso: /aprobar <token> (mira /tareas)")
            return
        dispatcher = _get_automation_dispatcher()
        task_id = dispatcher.approve(token) if dispatcher else None
        if task_id is None:
            await update.message.reply_text("❌ Token no válido o ya resuelto.")
        else:
            await update.message.reply_text(f"✅ Tarea #{task_id} aprobada.")

    async def _handle_rechazar(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Rechaza (cancela) la tarea indicada por token."""
        if not self._is_authorized(str(update.effective_user.id)):
            await update.message.reply_text("🔒 No tienes permiso.")
            return
        token = (context.args[0] if context.args else "").strip()
        if not token:
            await update.message.reply_text("Uso: /rechazar <token> (mira /tareas)")
            return
        dispatcher = _get_automation_dispatcher()
        task_id = dispatcher.reject(token, "rechazada por Telegram") if dispatcher else None
        if task_id is None:
            await update.message.reply_text("❌ Token no válido o ya resuelto.")
        else:
            await update.message.reply_text(f"⛔ Tarea #{task_id} rechazada.")

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Maneja mensajes de texto."""
        chat_id = str(update.effective_chat.id)
        user_id = str(update.effective_user.id)
        
        # Verificar autorización
        if not self._is_authorized(user_id):
            await update.message.reply_text("🔒 No tienes permiso. Usa /pairing para solicitar acceso.")
            return
        
        # Obtener o crear sesión. El system prompt va siempre al inicio para
        # conservar la personalidad configurada aunque se recorte el historial.
        history = [message for message in self.db.get_session(chat_id) if message.get("role") != "system"]
        system_prompt = _get_shared_system_prompt()
        messages = ([{"role": "system", "content": system_prompt}] if system_prompt else []) + history
        
        # Agregar mensaje del usuario
        messages.append({
            "role": "user",
            "content": update.message.text
        })
        
        typing_stop = asyncio.Event()

        async def keep_typing():
            while not typing_stop.is_set():
                try:
                    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(typing_stop.wait(), timeout=4)
                except asyncio.TimeoutError:
                    pass

        typing_task = asyncio.create_task(keep_typing())

        # Enviar a llama-server fuera del event loop para no bloquear el
        # indicador de escritura ni la recepción de otros mensajes.
        request_payload = {
            "model": "default",
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 500,
            "stream": False
        }
        try:
            response = await asyncio.to_thread(
                requests.post,
                LLAMA_CHAT_ENDPOINT,
                json=request_payload,
                timeout=None,
            )
            
            if response.status_code == 200:
                data = response.json()
                assistant_message = data.get("choices", [{}])[0].get("message", {}).get("content", "Error")
                
                # Guardar respuesta
                messages.append({
                    "role": "assistant",
                    "content": assistant_message
                })
                self.db.save_session(chat_id, messages[-10:])  # Guardar sólo últimos 10 mensajes
                
                # Enviar respuesta (dividida si es muy larga)
                if len(assistant_message) > 4000:
                    for i in range(0, len(assistant_message), 4000):
                        await update.message.reply_text(assistant_message[i:i+4000])
                else:
                    await update.message.reply_text(assistant_message)
            else:
                await update.message.reply_text("❌ Error en el servidor de chat.")
        
        except Exception as e:
            logger.error(f"Error en _handle_message: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
        finally:
            typing_stop.set()
            await typing_task

# ============================================================================
# INTERFAZ GLOBAL DEL SERVICIO
# ============================================================================

_telegram_service: Optional[TelegramBotService] = None

async def start_telegram_service(token: str, allowed_users: List[str] = None, pairing_required: bool = True):
    """Inicia el servicio de Telegram (global)."""
    global _telegram_service
    
    if not TELEGRAM_AVAILABLE:
        logger.error("Telegram no está disponible.")
        return False
    
    if _telegram_service:
        logger.warning("Servicio de Telegram ya está en ejecución.")
        return False
    
    _telegram_service = TelegramBotService(token, allowed_users or [], pairing_required)
    return await _telegram_service.start_bot()

async def stop_telegram_service():
    """Detiene el servicio de Telegram (global)."""
    global _telegram_service
    
    if _telegram_service:
        await _telegram_service.stop_bot()
        _telegram_service = None

def get_telegram_service() -> Optional[TelegramBotService]:
    """Obtiene la instancia del servicio de Telegram."""
    return _telegram_service

def is_telegram_running() -> bool:
    """Verifica si el servicio de Telegram está en ejecución."""
    return _telegram_service is not None and _telegram_service.is_running

# ============================================================================
# EJECUCIÓN EN HILO (para arrancar el bot desde el Manager o config_server)
# python-telegram-bot necesita un event loop vivo durante toda la vida del bot.
# ============================================================================

_telegram_thread: Optional[threading.Thread] = None
_telegram_stop_event: Optional[threading.Event] = None
_telegram_thread_lock = threading.Lock()


def _telegram_worker(token: str, allowed_users: List[str], pairing_required: bool):
    """Worker que mantiene vivo el event loop del bot de Telegram."""
    async def _main():
        ok = await start_telegram_service(token=token, allowed_users=allowed_users, pairing_required=pairing_required)
        if not ok:
            logger.error("Telegram worker no pudo iniciar el bot.")
            return
        try:
            while True:
                if _telegram_stop_event and _telegram_stop_event.is_set():
                    break
                await asyncio.sleep(1)
        finally:
            await stop_telegram_service()
    try:
        asyncio.run(_main())
    except Exception as e:
        logger.error(f"Error en worker de Telegram: {e}")


def start_telegram_service_in_thread(token: str, allowed_users: List[str] = None, pairing_required: bool = True) -> bool:
    """Inicia el bot de Telegram en un hilo daemon (compatible con el Manager)."""
    global _telegram_thread, _telegram_stop_event
    with _telegram_thread_lock:
        if _telegram_thread and _telegram_thread.is_alive():
            logger.warning("El hilo de Telegram ya está en ejecución.")
            return False
        if not TELEGRAM_AVAILABLE:
            logger.error("Telegram no está disponible. Instala python-telegram-bot.")
            return False
        _telegram_stop_event = threading.Event()
        _telegram_thread = threading.Thread(
            target=_telegram_worker,
            args=(token, allowed_users or [], pairing_required),
            daemon=True,
        )
        _telegram_thread.start()
        return True


def stop_telegram_service_in_thread(timeout: float = 5.0) -> bool:
    """Detiene el bot de Telegram iniciado en hilo."""
    global _telegram_thread, _telegram_stop_event
    with _telegram_thread_lock:
        if _telegram_stop_event:
            _telegram_stop_event.set()
        if _telegram_thread and _telegram_thread.is_alive():
            _telegram_thread.join(timeout=timeout)
            if _telegram_thread.is_alive():
                logger.warning("El hilo de Telegram no terminó a tiempo.")
                return False
        _telegram_thread = None
        _telegram_stop_event = None
    return True
