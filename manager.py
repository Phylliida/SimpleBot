import os
import asyncio
import subprocess
import signal
import shutil
import sys
import socket
from datetime import datetime

class WorkerManager:
    def __init__(self):
        self.current_worker_dir = None
        self.current_process = None
        self.lock = asyncio.Lock()
        self.health_task = None
        self.last_request_times = {} # port -> datetime

    async def stop_worker(self):
        if self.health_task:
            self.health_task.cancel()
            try:
                await self.health_task
            except asyncio.CancelledError:
                pass
            self.health_task = None
        if self.current_process:
            try:
                self.current_process.terminate()
            except ProcessLookupError:
                pass
            self.current_process = None
        self.current_worker_dir = None

    async def start_worker(self, worker_dir, force=False):
        async with self.lock:
            if (self.current_worker_dir is not None and self.current_worker_dir != worker_dir) or force:
                print(f"[Manager] Stopping worker {self.current_worker_dir} to start {worker_dir}...")
                await self.stop_worker()

            if self.current_worker_dir == worker_dir and not force:
                print(f"[Manager] Worker {worker_dir} already running.")
                return True

            print(f"[Manager] Starting worker in directory: {worker_dir}")
            try:
                self.current_process = subprocess.Popen(
                    ["/run/current-system/sw/bin/bash", "run.sh"],
                    cwd=worker_dir,
                    stdout=None,
                    stderr=None
                )
                self.current_worker_dir = worker_dir
                
                await asyncio.sleep(2.0) 
                self.health_task = asyncio.create_task(self.health_loop(worker_dir))
                print(f"[Manager] Worker {worker_dir} should now be active.")
                return True
            except Exception as e:
                print(f"[Manager] Error starting worker {worker_dir}: {e}")
                return False

    async def health_loop(self, worker_dir):
        port = int(worker_dir)
        while True:
            try:
                await asyncio.sleep(90)
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection('127.0.0.1', port), 
                    timeout=5.0
                )
                writer.write(b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
                await writer.drain()
                response = await reader.read(1024)
                writer.close()
                await writer.wait_closed()
                if b"200 OK" not in response:
                    raise Exception("Health check response not 200 OK")
            except Exception as e:
                print(f"[Manager] Health check failed for {worker_dir}: {e}. Restarting...")
                asyncio.create_task(self.start_worker(worker_dir, force=True))
                break

async def pipe(reader, writer):
    try:
        while not reader.at_eof():
            data = await reader.read(8192)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()

def enable_keepalive(writer):
    sock = writer.get_extra_info('socket')
    if sock:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            # Linux constants for keepalive
            if hasattr(socket, 'TCP_KEEPIDLE'):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
            elif hasattr(socket, 'TCP_KEEPALIVE'): # macOS
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, 60)
            
            if hasattr(socket, 'TCP_KEEPINTVL'):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
            if hasattr(socket, 'TCP_KEEPCNT'):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5)
        except Exception as e:
            print(f"[Manager] Warning: Could not set keepalive: {e}")

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

async def handle_client(client_reader, client_writer, worker_port, worker_manager):
    worker_dir = str(worker_port)
    print(f"[Manager] Request received on port {worker_port + 1}, targeting worker {worker_dir}")
    
    worker_manager.last_request_times[worker_port] = datetime.now()
    
    enable_keepalive(client_writer)
    
    success = await worker_manager.start_worker(worker_dir)
    if not success:
        print(f"[Manager] Failed to ensure worker {worker_dir} was running.")
        client_writer.close()
        return

    try:
        # Connect to the worker on port N
        print(f"[Manager] Proxying connection to 127.0.0.1:{worker_port}")
        server_reader, server_writer = await asyncio.open_connection('127.0.0.1', worker_port)
        enable_keepalive(server_writer)
        
        await asyncio.gather(
            pipe(client_reader, server_writer),
            pipe(server_reader, client_writer)
        )
    except Exception as e:
        print(f"[Manager] Proxy error for port {worker_port}: {e}")
    finally:
        client_writer.close()

async def handle_index_page(reader, writer, worker_manager, dirs):
    print("[Manager] Index page requested")
    
    html = ["<html><head><title>Worker Manager</title><style>body{font-family:sans-serif;padding:2em;line-height:1.6;} .worker{margin-bottom:1em; padding:1em; border:1px solid #ccc; border-radius:8px;} .running{background:#e8f5e9; border-color:#4caf50;} .stopped{background:#fff;}</style></head><body>"]
    html.append("<h1>Routed Workers</h1>")
    
    for d in dirs:
        port = int(d)
        listen_port = port + 1
        
        # Read name.txt
        name = d
        try:
            with open(os.path.join(d, 'name.txt'), 'r') as f:
                name = f.read().strip()
        except Exception:
            pass
            
        is_running = worker_manager.current_worker_dir == d
        last_req = worker_manager.last_request_times.get(port)
        time_str = format_relative_time(last_req)
        
        status_class = "running" if is_running else "stopped"
        status_text = "RUNNING" if is_running else "Stopped"
        
        html.append(f'<div class="worker {status_class}">')
        html.append(f'<strong>{name}</strong> (Port {listen_port})<br>')
        html.append(f'Status: {status_text}<br>')
        html.append(f'Last Request: {time_str}<br>')
        html.append(f'<a href="http://{socket.gethostname()}:{listen_port}">Open Link</a>')
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
        # Find directories that are numeric
        dirs = [d for d in os.listdir('.') if os.path.isdir(d) and d.isdigit()]
        if not dirs:
            print("[Manager] No numeric directories found. Exiting.")
            return

        servers = []
        
        # Index server on 8099
        index_server = await asyncio.start_server(
            lambda r, w: handle_index_page(r, w, worker_manager, dirs),
            '0.0.0.0',
            8099
        )
        print("[Manager] Index page listening on port 8099")
        servers.append(index_server)

        for d in dirs:
            port = int(d)
            listen_port = port + 1
            
            def create_handler(p):
                return lambda r, w: handle_client(r, w, p, worker_manager)

            server = await asyncio.start_server(
                create_handler(port), 
                '0.0.0.0', 
                listen_port
            )
            print(f"[Manager] Listening on port {listen_port} -> Proxying to {port}")
            servers.append(server)

        await asyncio.gather(*[s.serve_forever() for s in servers])
    finally:
        print("[Manager] Cleaning up worker...")
        await worker_manager.stop_worker()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Manager] Shutting down...")
