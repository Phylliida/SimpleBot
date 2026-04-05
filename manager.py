import os
import asyncio
import subprocess
import signal
import shutil
import sys

class WorkerManager:
    def __init__(self):
        self.current_worker_dir = None
        self.current_process = None
        self.lock = asyncio.Lock()

    async def start_worker(self, worker_dir):
        async with self.lock:
            if self.current_worker_dir is not None and self.current_worker_dir != worker_dir:
                print(f"[Manager] Stopping worker {self.current_worker_dir} to start {worker_dir}...")
                if self.current_process:
                    try:
                        # Kill the entire process group
                        os.killpg(os.getpgid(self.current_process.pid), signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    self.current_process = None
                    self.current_worker_dir = None

            if self.current_worker_dir == worker_dir:
                print(f"[Manager] Worker {worker_dir} already running.")
                return True

            print(f"[Manager] Starting worker in directory: {worker_dir}")
            try:
                # start_new_session=True is vital for os.killpg to work
                self.current_process = subprocess.Popen(
                    ["/run/current-system/sw/bin/bash", "run.sh"],
                    cwd=worker_dir,
                    start_new_session=True,
                    stdout=None, # Let it inherit or redirect in run.sh
                    stderr=None
                )
                self.current_worker_dir = worker_dir
                
                # Wait for the worker to boot
                await asyncio.sleep(2.0) 
                print(f"[Manager] Worker {worker_dir} should now be active.")
                return True
            except Exception as e:
                print(f"[Manager] Error starting worker {worker_dir}: {e}")
                return False

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

async def handle_client(client_reader, client_writer, worker_port, worker_manager):
    worker_dir = str(worker_port)
    print(f"[Manager] Request received on port {worker_port + 1}, targeting worker {worker_dir}")
    
    success = await worker_manager.start_worker(worker_dir)
    if not success:
        print(f"[Manager] Failed to ensure worker {worker_dir} was running.")
        client_writer.close()
        return

    try:
        # Connect to the worker on port N
        print(f"[Manager] Proxying connection to 127.0.0.1:{worker_port}")
        server_reader, server_writer = await asyncio.open_connection('127.0.0.1', worker_port)
        
        await asyncio.gather(
            pipe(client_reader, server_writer),
            pipe(server_reader, client_writer)
        )
    except Exception as e:
        print(f"[Manager] Proxy error for port {worker_port}: {e}")
    finally:
        client_writer.close()

async def main():
    worker_manager = WorkerManager()
    
    # Find directories that are numeric
    dirs = [d for d in os.listdir('.') if os.path.isdir(d) and d.isdigit()]
    if not dirs:
        print("[Manager] No numeric directories found. Exiting.")
        return

    servers = []
    for d in dirs:
        port = int(d)
        listen_port = port + 1
        
        # We use a closure to capture the current port value
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

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Manager] Shutting down...")
