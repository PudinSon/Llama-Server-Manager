# Servicio de automatización 24/7 (puerto 15001)

El modo autónomo (cola persistente, dispatchers, aprobaciones y webhooks) vive en
su **propio proceso**, no dentro de la UI ni del agente HTTP:

| Módulo | Papel |
|---|---|
| `tools/automation_service.py` | El servicio: dispatcher + pool de workers + janitor, listener IMAP, ingesta de webhooks y API HTTP en `127.0.0.1:15001`. |
| `tools/automation_process.py` | Supervisor del proceso: `start()`, `stop()`, `status()`. Única puerta de arranque y parada, compartida por los dos managers. |
| `tools/automation_dispatcher.py`, `cron_dispatcher.py`, `filewatch_dispatcher.py`, `webhook_dispatcher.py` | Fuentes de eventos (los cuatro dispatchers del panel). |
| `llama-server-ui/src/lib/components/app/automation/` | Pestaña que consulta y configura el servicio. La UI no ejecuta nada. |

## Ciclo de vida

```
Manager (Tk)                      Servicio 24/7 (proceso hijo)
─────────────                     ────────────────────────────
_start_services_thread
  └─ start_automation_service() ─► python -m tools.automation_service
                                     --port 15001 --parent-pid <manager>
on_close
  └─ stop_automation_service()  ─► kill del árbol de procesos
                                     (+ libera el puerto)

kill del manager (sin cierre limpio)
                                  ◄─ watchdog: el padre ya no existe → sale
```

1. **Carga con el manager.** `_start_services_thread` arranca el servicio tras
   dev_agent (15000) y el workspace server (19000), gated por
   `automation_enabled` (por defecto activo).
2. **Sobrevive a la web view.** El visor embebido y el panel web son clientes
   HTTP: cerrarlos no toca el proceso, así que la cola y los dispatchers siguen.
3. **Muere con el manager.** `on_close` lo detiene; si el manager desaparece sin
   cerrar limpio, el watchdog `--parent-pid` del servicio lo cierra en ≤5 s
   (parando antes sus backends). Sin manager no hay modelo cargado y ningún
   dispatcher tendría nada que ejecutar.
4. **Idempotente.** `start()` no lanza una segunda instancia si el puerto ya
   responde (`already_running`). Si el proceso vive pero el puerto aún no
   escucha, devuelve `starting` en vez de matarlo: cargar el dispatcher tarda.

## Áreas de activación

| Interfaz | Dónde |
|---|---|
| Manager de escritorio | **Configuración Avanzada** → *Activar servicio de automatización 24/7 (dispatchers, cola y webhooks)*. Guardar arranca (o para) el servicio en el momento. |
| Panel web | **Servicios** → tarjeta *automatización 24/7* con Iniciar/Detener, y el mismo interruptor en **Configuración Avanzada**. |
| Sin GUI (`--web-only` o entorno headless) | El propio panel es el dueño: `start_web_server(..., ensure_automation=True)` lo arranca al abrir. |

El modo con GUI **no** duplica el arranque: allí el dueño es el manager Tk y el
panel solo muestra el estado.

## API y log

- Estado: `GET http://127.0.0.1:15001/automation/status` (dispatcher, cola, IMAP,
  crons, filewatch, webhooks).
- Tareas: `/automation/tasks`, aprobaciones `/automation/approve|reject/<token>`,
  auditoría `/automation/audit`, webhooks `POST /webhook/<nombre>`.
- Log del proceso: `user_data/automation_service.log` (el servicio no tiene
  consola: el supervisor redirige ahí stdout/stderr).

## Arranque manual (sin manager)

```bash
python -m tools.automation_service            # 127.0.0.1:15001, sin watchdog
python -m tools.automation_service --parent-pid 1234
```

Sin `--parent-pid` el watchdog no vigila a nadie: útil para depurar, pero
recuerda que sin manager no hay modelo y las tareas quedan esperando.
