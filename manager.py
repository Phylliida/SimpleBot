import os
import asyncio
import subprocess
import socket
from datetime import datetime

ONE_AT_A_TIME_DIR = 'one-at-a-time-routes'
PERSISTENT_DIR = 'persistent-routes'

KIND_ONE_AT_A_TIME = 'one_at_a_time'
KIND_PERSISTENT = 'persistent'


async def probe_ready(worker, connect_timeout=1.0, http_timeout=3.0):
    """Return True if the worker is ready to serve.

    If the worker has a `health_path`, issue an HTTP GET and require a 200
    response. Otherwise just verify TCP accepts a connection.
    """
    port = worker['port']
    health_path = worker.get('health_path')
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection('127.0.0.1', port),
            timeout=connect_timeout,
        )
    except (ConnectionRefusedError, OSError, asyncio.TimeoutError):
        return False
    try:
        if not health_path:
            return True
        req = (f"GET {health_path} HTTP/1.1\r\n"
               f"Host: 127.0.0.1\r\nConnection: close\r\n\r\n").encode()
        writer.write(req)
        await writer.drain()
        response = await asyncio.wait_for(reader.read(1024), timeout=http_timeout)
        return b"200 OK" in response
    except Exception:
        return False
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def wait_for_ready(worker, timeout=180.0, interval=0.5):
    """Poll probe_ready until it succeeds or the timeout elapses."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await probe_ready(worker):
            return True
        await asyncio.sleep(interval)
    return False


def discover_workers():
    workers = []
    for kind, base in [(KIND_ONE_AT_A_TIME, ONE_AT_A_TIME_DIR), (KIND_PERSISTENT, PERSISTENT_DIR)]:
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            worker_dir = os.path.join(base, name)
            if not os.path.isdir(worker_dir):
                continue
            port_file = os.path.join(worker_dir, 'port.txt')
            if not os.path.isfile(port_file):
                print(f"[Manager] Skipping {worker_dir}: no port.txt")
                continue
            try:
                with open(port_file, 'r') as f:
                    port = int(f.read().strip())
            except Exception as e:
                print(f"[Manager] Skipping {worker_dir}: invalid port.txt ({e})")
                continue
            health_path = None
            health_file = os.path.join(worker_dir, 'health_path.txt')
            if os.path.isfile(health_file):
                try:
                    with open(health_file, 'r') as f:
                        health_path = f.read().strip() or None
                except Exception as e:
                    print(f"[Manager] Ignoring health_path.txt in {worker_dir}: {e}")
            workers.append({
                'name': name,
                'dir': worker_dir,
                'port': port,
                'kind': kind,
                'health_path': health_path,
            })
    return workers


class WorkerManager:
    def __init__(self):
        # one-at-a-time
        self.current_worker = None  # name of currently running one-at-a-time worker
        self.current_process = None
        self.one_at_a_time_lock = asyncio.Lock()
        self.one_at_a_time_health_task = None

        # persistent
        self.persistent_processes = {}  # name -> Popen
        self.persistent_health_tasks = {}  # name -> asyncio.Task

        # shared
        self.last_request_times = {}  # name -> datetime

    # ---------- one-at-a-time ----------

    async def stop_one_at_a_time(self):
        if self.one_at_a_time_health_task:
            self.one_at_a_time_health_task.cancel()
            try:
                await self.one_at_a_time_health_task
            except asyncio.CancelledError:
                pass
            self.one_at_a_time_health_task = None
        if self.current_process:
            try:
                self.current_process.terminate()
            except ProcessLookupError:
                pass
            self.current_process = None
        self.current_worker = None

    async def start_one_at_a_time(self, worker, force=False):
        async with self.one_at_a_time_lock:
            name = worker['name']
            if (self.current_worker is not None and self.current_worker != name) or force:
                print(f"[Manager] Stopping worker {self.current_worker} to start {name}...")
                await self.stop_one_at_a_time()

            if self.current_worker == name and not force:
                print(f"[Manager] Worker {name} already running.")
                return True

            print(f"[Manager] Starting one-at-a-time worker {name} (dir: {worker['dir']})")
            try:
                self.current_process = subprocess.Popen(
                    ["/run/current-system/sw/bin/bash", "run.sh"],
                    cwd=worker['dir'],
                    stdout=None,
                    stderr=None,
                )
                self.current_worker = name
            except Exception as e:
                print(f"[Manager] Error starting worker {name}: {e}")
                self.current_process = None
                self.current_worker = None
                return False

            # Block until the backend is actually ready (TCP accept, or HTTP 200
            # on health_path if configured) so the first proxied request doesn't
            # hit a not-yet-listening port or a loading server.
            probe_desc = f"GET {worker['health_path']}" if worker.get('health_path') else "TCP accept"
            print(f"[Manager] Waiting for {name} to become ready on port {worker['port']} ({probe_desc})...")
            ready = await wait_for_ready(worker, timeout=180.0)
            if not ready:
                print(f"[Manager] Worker {name} never started listening. Tearing down.")
                try:
                    self.current_process.terminate()
                except ProcessLookupError:
                    pass
                self.current_process = None
                self.current_worker = None
                return False

            self.one_at_a_time_health_task = asyncio.create_task(self.one_at_a_time_health_loop(worker))
            print(f"[Manager] Worker {name} is accepting connections.")
            return True

    async def one_at_a_time_health_loop(self, worker):
        name = worker['name']
        while True:
            await asyncio.sleep(90)
            if await probe_ready(worker, connect_timeout=5.0, http_timeout=5.0):
                continue
            print(f"[Manager] Health check failed for {name}. Restarting...")
            asyncio.create_task(self.start_one_at_a_time(worker, force=True))
            break

    # ---------- persistent ----------

    def start_persistent(self, worker):
        name = worker['name']
        print(f"[Manager] Starting persistent worker {name} (dir: {worker['dir']})")
        try:
            proc = subprocess.Popen(
                ["/run/current-system/sw/bin/bash", "run.sh"],
                cwd=worker['dir'],
                stdout=None,
                stderr=None,
            )
            self.persistent_processes[name] = proc
            return True
        except Exception as e:
            print(f"[Manager] Error starting persistent worker {name}: {e}")
            return False

    async def persistent_health_loop(self, worker):
        name = worker['name']
        # Give the process a moment to come up before the first probe.
        await asyncio.sleep(5.0)
        while True:
            await asyncio.sleep(30)
            proc = self.persistent_processes.get(name)
            alive = proc is not None and proc.poll() is None
            reachable = alive and await probe_ready(worker, connect_timeout=5.0, http_timeout=5.0)
            if alive and reachable:
                continue
            reason = "process exited" if not alive else "health probe failed"
            print(f"[Manager] Persistent worker {name} unhealthy ({reason}). Restarting...")
            if proc is not None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
            self.start_persistent(worker)
            await asyncio.sleep(5.0)

    # ---------- lifecycle ----------

    def is_running(self, worker):
        if worker['kind'] == KIND_ONE_AT_A_TIME:
            return self.current_worker == worker['name']
        proc = self.persistent_processes.get(worker['name'])
        return proc is not None and proc.poll() is None

    async def stop_all(self):
        await self.stop_one_at_a_time()
        for name, task in list(self.persistent_health_tasks.items()):
            task.cancel()
        for name, task in list(self.persistent_health_tasks.items()):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self.persistent_health_tasks.clear()
        for name, proc in list(self.persistent_processes.items()):
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        self.persistent_processes.clear()


async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, ConnectionError, asyncio.IncompleteReadError):
        pass
    except Exception as e:
        print(f"[Manager] pipe error: {e}")
    finally:
        # Half-close the write side so the other direction can keep flowing.
        try:
            if writer.can_write_eof():
                writer.write_eof()
        except Exception:
            pass


def tune_socket(writer):
    """Apply TCP_NODELAY + keepalive so small frames (WS/SSE/chat tokens)
    forward immediately and dead peers are detected."""
    sock = writer.get_extra_info('socket')
    if not sock:
        return
    try:
        # Disable Nagle's algorithm — forward each chunk immediately instead of
        # coalescing with up-to-40ms delay. Critical for streaming responses.
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, 'TCP_KEEPIDLE'):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
        elif hasattr(socket, 'TCP_KEEPALIVE'):  # macOS
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, 60)
        if hasattr(socket, 'TCP_KEEPINTVL'):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
        if hasattr(socket, 'TCP_KEEPCNT'):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5)
    except Exception as e:
        print(f"[Manager] Warning: Could not tune socket: {e}")


def format_relative_time(last_req):
    if not last_req:
        return "Never"
    diff = datetime.now() - last_req
    seconds = int(diff.total_seconds())
    if seconds < 1:
        return "Just now"
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''} ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = seconds // 3600
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = seconds // 86400
    return f"{days} day{'s' if days != 1 else ''} ago"


async def handle_client(client_reader, client_writer, worker, worker_manager):
    name = worker['name']
    port = worker['port']
    print(f"[Manager] Request received on port {port + 1}, targeting worker {name}")

    worker_manager.last_request_times[name] = datetime.now()

    tune_socket(client_writer)

    if worker['kind'] == KIND_ONE_AT_A_TIME:
        success = await worker_manager.start_one_at_a_time(worker)
        if not success:
            print(f"[Manager] Failed to ensure worker {name} was running.")
            try:
                client_writer.close()
            except Exception:
                pass
            return

    server_writer = None
    try:
        print(f"[Manager] Proxying connection to 127.0.0.1:{port}")
        # limit sets the StreamReader buffer ceiling — bigger = fewer pause/resume
        # cycles when the backend bursts response data (e.g. tile blobs, token streams).
        server_reader, server_writer = await asyncio.open_connection(
            '127.0.0.1', port, limit=1024 * 1024
        )
        tune_socket(server_writer)

        await asyncio.gather(
            pipe(client_reader, server_writer),
            pipe(server_reader, client_writer),
            return_exceptions=True,
        )
    except Exception as e:
        print(f"[Manager] Proxy error for {name}: {e}")
    finally:
        for w in (server_writer, client_writer):
            if w is None:
                continue
            try:
                w.close()
            except Exception:
                pass


async def handle_index_page(reader, writer, worker_manager, workers):
    print("[Manager] Index page requested")

    html = ["<html><head><title>Worker Manager</title>"
            "<meta http-equiv=\"refresh\" content=\"20\">"
            "<style>body{font-family:sans-serif;padding:2em;line-height:1.6;}"
            " h2{margin-top:2em;}"
            " .worker{margin-bottom:1em; padding:1em; border:1px solid #ccc; border-radius:8px;}"
            " .running{background:#e8f5e9; border-color:#4caf50;}"
            " .stopped{background:#fff;}</style></head><body>"]
    html.append("<h1>Routed Workers</h1>")

    sections = [
        ("One-at-a-time (GPU/VRAM)", KIND_ONE_AT_A_TIME),
        ("Persistent (always running)", KIND_PERSISTENT),
    ]

    for title, kind in sections:
        section_workers = [w for w in workers if w['kind'] == kind]
        if not section_workers:
            continue
        html.append(f"<h2>{title}</h2>")
        for worker in section_workers:
            name = worker['name']
            listen_port = worker['port'] + 1

            is_running = worker_manager.is_running(worker)
            last_req = worker_manager.last_request_times.get(name)
            time_str = format_relative_time(last_req)

            status_class = "running" if is_running else "stopped"
            status_text = "RUNNING" if is_running else "Stopped"

            html.append(f'<div class="worker {status_class}">')
            html.append(f'<strong>{name}</strong> (Port {listen_port})<br>')
            html.append(f'Status: {status_text}<br>')
            html.append(f'Last Request: {time_str}<br>')
            html.append(f'<a href="http://172.22.146.1:{listen_port}" target="_blank">Open Link</a>')
            html.append('</div>')

    html.append("</body></html>")

    body = "".join(html).encode()
    header = f"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
    writer.write(header + body)
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def main():
    worker_manager = WorkerManager()
    try:
        workers = discover_workers()
        if not workers:
            print(f"[Manager] No workers found. Exiting.")
            return

        # Boot all persistent workers up front.
        for worker in workers:
            if worker['kind'] == KIND_PERSISTENT:
                worker_manager.start_persistent(worker)
                worker_manager.persistent_health_tasks[worker['name']] = asyncio.create_task(
                    worker_manager.persistent_health_loop(worker)
                )

        servers = []

        # Index server on 8099
        index_server = await asyncio.start_server(
            lambda r, w: handle_index_page(r, w, worker_manager, workers),
            '0.0.0.0',
            8099,
        )
        print("[Manager] Index page listening on port 8099")
        servers.append(index_server)

        for worker in workers:
            listen_port = worker['port'] + 1

            def create_handler(wk):
                return lambda r, w: handle_client(r, w, wk, worker_manager)

            server = await asyncio.start_server(
                create_handler(worker),
                '0.0.0.0',
                listen_port,
            )
            kind_label = worker['kind'].replace('_', '-')
            print(f"[Manager] Listening on port {listen_port} -> {worker['port']} "
                  f"({worker['name']}, {kind_label})")
            servers.append(server)

        await asyncio.gather(*[s.serve_forever() for s in servers])
    finally:
        print("[Manager] Cleaning up workers...")
        await worker_manager.stop_all()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Manager] Shutting down...")
