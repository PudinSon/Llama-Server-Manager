import subprocess
import psutil
import time
import os
import signal
import sys

def is_port_in_use(port):
    try:
        for conn in psutil.net_connections():
            if conn.laddr and conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
                return True
        return False
    except Exception:
        return False

def kill_process_tree(pid, timeout=3.0):
    """Mata un proceso y todos sus hijos (Windows/Linux). Retorna True si se logró."""
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        # Primero intentar terminar suavemente
        for child in children:
            try:
                child.terminate()
            except:
                pass
        try:
            parent.terminate()
        except:
            pass
        # Esperar a que terminen
        start = time.time()
        while time.time() - start < timeout:
            alive = []
            for pr in [parent] + children:
                try:
                    if pr.is_running():
                        alive.append(pr)
                except:
                    pass
            if not alive:
                return True
            time.sleep(0.1)
        # Forzar kill
        for pr in [parent] + children:
            try:
                pr.kill()
            except:
                pass
        return True
    except Exception:
        return False

def _terminate_proc(proc, timeout=3.0):
    """Termina un proceso (Popen o PID) y sus hijos."""
    pid = None
    if hasattr(proc, 'pid'):
        pid = proc.pid
    elif isinstance(proc, int):
        pid = proc
    if pid:
        return kill_process_tree(pid, timeout)
    else:
        try:
            proc.terminate()
            start = time.time()
            while proc.poll() is None and (time.time() - start) < timeout:
                time.sleep(0.1)
            if proc.poll() is None:
                proc.kill()
            return True
        except Exception:
            return False

def start_server(cmd, port=None, creationflags=None, env=None, capture_output=True, cwd=None):
    """Inicia un servidor. Si se da puerto, verifica que esté libre."""
    if port and is_port_in_use(port):
        raise RuntimeError(f"Puerto {port} ya en uso")

    kwargs = {}
    if creationflags is not None and os.name == 'nt':
        kwargs["creationflags"] = creationflags
    if env is not None:
        kwargs["env"] = env
    if cwd is not None:
        kwargs["cwd"] = cwd
    # En Linux/Unix, usar start_new_session para que los hijos sean independientes
    if os.name != 'nt':
        kwargs["start_new_session"] = True

    if capture_output:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
    else:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    return proc

def stop_server(proc=None, port=None, wait=2.0):
    """Detiene un servidor dado su proceso o puerto. Fuerza la terminación del árbol."""
    if proc:
        try:
            pid = getattr(proc, 'pid', None)
            if pid:
                return kill_process_tree(pid, wait)
            else:
                try:
                    proc.terminate()
                    start = time.time()
                    while proc.poll() is None and (time.time() - start) < wait:
                        time.sleep(0.1)
                    if proc.poll() is None:
                        proc.kill()
                    return True
                except Exception:
                    return False
        except Exception:
            return False
    elif port:
        try:
            for conn in psutil.net_connections():
                if conn.laddr and conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
                    try:
                        return kill_process_tree(conn.pid, wait)
                    except Exception:
                        continue
            return False
        except Exception:
            return False
    return False

def close_process_gracefully(proc, timeout=5.0):
    """Cierra un proceso y sus hijos (prioriza terminación suave)."""
    if proc is None:
        return
    if hasattr(proc, 'poll') and proc.poll() is not None:
        return
    pid = getattr(proc, 'pid', None)
    if pid:
        kill_process_tree(pid, timeout)
    else:
        try:
            proc.terminate()
            proc.wait(timeout=timeout)
        except:
            try:
                proc.kill()
            except:
                pass