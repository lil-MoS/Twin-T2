import os
import pty
import asyncio
import signal
import subprocess
import fcntl
import struct
import termios
import json
import time
import posixpath
import shutil

from aiohttp import web, WSMsgType


# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

PORT = int(os.environ.get("PORT", "7681"))
PASSWORD = os.environ.get("TWIN_PASSWORD", "changeme")

INDEX_FILE = "/app/index.html"
DATA_DIR = os.environ.get("DATA_DIR", "/data")
TRAFFIC_FILE = os.path.join(DATA_DIR, ".traffic.json")

TRAFFIC_LIMIT = 100 * 1024 ** 3          # 100 GB / month
MAX_FILE_SIZE = 5 * 1024 * 1024          # 5 MB read/write limit
MAX_TERMINALS = 8

START_TIME = time.monotonic()

os.makedirs(DATA_DIR, exist_ok=True)


# ─────────────────────────────────────────────
# Traffic accounting (per calendar month)
# ─────────────────────────────────────────────

def current_month():
    t = time.gmtime()
    return f"{t.tm_year}-{t.tm_mon:02d}"


class Traffic:
    def __init__(self, path):
        self.path = path
        self.month = current_month()
        self.bytes = 0
        self.dirty = False
        self._load()

    def _load(self):
        try:
            with open(self.path, "r") as f:
                d = json.load(f)
            if d.get("month") == self.month:
                self.bytes = int(d.get("bytes", 0))
            else:
                self.bytes = 0
        except Exception:
            self.bytes = 0

    def save(self):
        if not self.dirty:
            return
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"month": self.month, "bytes": self.bytes}, f)
            os.replace(tmp, self.path)
            self.dirty = False
        except Exception:
            pass

    def add(self, n):
        if n <= 0:
            return
        m = current_month()
        if m != self.month:
            self.month = m
            self.bytes = 0
        self.bytes += n
        self.dirty = True

    def snapshot(self):
        return {
            "used": self.bytes,
            "limit": TRAFFIC_LIMIT,
            "month": self.month,
            "exceeded": self.bytes >= TRAFFIC_LIMIT,
        }


traffic = Traffic(TRAFFIC_FILE)


async def traffic_flusher():
    while True:
        await asyncio.sleep(10)
        traffic.save()


# ─────────────────────────────────────────────
# CPU tracker (cgroup v2, then fallback)
# ─────────────────────────────────────────────

class CpuTracker:
    def __init__(self):
        self.prev = None
        self.prev_t = None

    def _read_usage(self):
        try:
            with open("/sys/fs/cgroup/cpu.stat", "r") as f:
                for line in f:
                    if line.startswith("usage_usec"):
                        return int(line.split()[1])
        except Exception:
            pass
        return None

    def sample(self):
        now = time.monotonic()
        usage = self._read_usage()

        if usage is None:
            try:
                return max(0.0, os.getloadavg()[0] * 100.0)
            except Exception:
                return 0.0

        if self.prev is None:
            self.prev = usage
            self.prev_t = now
            return 0.0

        dt = now - self.prev_t
        du = (usage - self.prev) / 1_000_000.0
        self.prev = usage
        self.prev_t = now

        if dt <= 0:
            return 0.0
        return max(0.0, (du / dt) * 100.0)


cpu = CpuTracker()


def read_memory():
    used = None
    limit = None

    # cgroup v2
    try:
        with open("/sys/fs/cgroup/memory.current", "r") as f:
            used = int(f.read().strip())
    except Exception:
        pass
    try:
        with open("/sys/fs/cgroup/memory.max", "r") as f:
            v = f.read().strip()
            if v and v != "max":
                limit = int(v)
    except Exception:
        pass

    # cgroup v1 fallback
    if used is None:
        try:
            with open("/sys/fs/cgroup/memory/memory.usage_in_bytes", "r") as f:
                used = int(f.read().strip())
        except Exception:
            pass
    if limit is None:
        try:
            with open("/sys/fs/cgroup/memory/memory.limit_in_bytes", "r") as f:
                v = int(f.read().strip())
                if v < (1 << 60):
                    limit = v
        except Exception:
            pass

    # /proc/meminfo fallback
    if used is None:
        try:
            vals = {}
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            vals[parts[0].rstrip(":")] = int(parts[1])
                        except ValueError:
                            pass
            total = vals.get("MemTotal", 0) * 1024
            avail = vals.get("MemAvailable", 0) * 1024
            used = max(0, total - avail)
            if limit is None:
                limit = total
        except Exception:
            pass

    return used or 0, limit or 0


# ─────────────────────────────────────────────
# HTTP handlers
# ─────────────────────────────────────────────

async def index(request):
    return web.FileResponse(INDEX_FILE)


async def api_login(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Bad JSON"}, status=400)

    if data.get("password") == PASSWORD:
        return web.json_response({"ok": True})

    # slow down brute-force
    await asyncio.sleep(0.5)
    return web.json_response({"ok": False, "error": "Invalid password"}, status=401)


async def api_stats(request):
    mem_used, mem_limit = read_memory()
    cpu_pct = cpu.sample()

    return web.json_response({
        "memory": {"used": mem_used, "limit": mem_limit},
        "cpu": {"percent": cpu_pct},
        "traffic": traffic.snapshot(),
        "uptime": int(time.monotonic() - START_TIME),
        "terminals": MAX_TERMINALS,
        "status": "online",
    })


async def health(request):
    return web.json_response({
        "status": "ok",
        "service": "Twin Terminal",
        "version": "2.0",
    })


# ─────────────────────────────────────────────
# File API
# ─────────────────────────────────────────────

def safe_path(rel):
    rel = (rel or "/").strip()
    if not rel.startswith("/"):
        rel = "/" + rel

    parts = []
    for p in rel.split("/"):
        if p in ("", "."):
            continue
        if p == "..":
            if parts:
                parts.pop()
            continue
        parts.append(p)

    norm = "/" + "/".join(parts)
    full = os.path.join(DATA_DIR, *parts) if parts else DATA_DIR
    return full, norm


async def api_files_list(request):
    rel = request.query.get("path", "/")
    full, norm = safe_path(rel)

    if not os.path.isdir(full):
        return web.json_response({"error": "Not a directory"}, status=400)

    items = []
    try:
        with os.scandir(full) as it:
            for entry in it:
                try:
                    st = entry.stat()
                    items.append({
                        "name": entry.name,
                        "path": posixpath.join(norm, entry.name),
                        "isDir": entry.is_dir(),
                        "size": st.st_size,
                        "mtime": int(st.st_mtime),
                    })
                except Exception:
                    pass
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

    items.sort(key=lambda x: (not x["isDir"], x["name"].lower()))

    return web.json_response({"path": norm, "items": items})


async def api_files_read(request):
    rel = request.query.get("path", "")
    full, norm = safe_path(rel)

    if not os.path.isfile(full):
        return web.json_response({"error": "Not a file"}, status=404)

    try:
        size = os.path.getsize(full)
        if size > MAX_FILE_SIZE:
            return web.json_response(
                {"error": f"File too large (>{MAX_FILE_SIZE} bytes)"},
                status=413,
            )

        with open(full, "rb") as f:
            data = f.read()

        try:
            text = data.decode("utf-8")
            binary = False
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
            binary = True

        return web.json_response({
            "path": norm,
            "content": text,
            "binary": binary,
            "size": size,
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_files_write(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Bad JSON"}, status=400)

    rel = data.get("path", "")
    content = data.get("content", "")
    full, norm = safe_path(rel)

    if norm == "/":
        return web.json_response({"error": "Invalid path"}, status=400)

    try:
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return web.json_response({"ok": True, "path": norm})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_files_mkdir(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Bad JSON"}, status=400)

    full, norm = safe_path(data.get("path", ""))
    if norm == "/":
        return web.json_response({"error": "Invalid path"}, status=400)

    try:
        os.makedirs(full, exist_ok=True)
        return web.json_response({"ok": True, "path": norm})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_files_create(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Bad JSON"}, status=400)

    full, norm = safe_path(data.get("path", ""))
    if norm == "/":
        return web.json_response({"error": "Invalid path"}, status=400)

    try:
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if not os.path.exists(full):
            with open(full, "w", encoding="utf-8") as f:
                f.write("")
        return web.json_response({"ok": True, "path": norm})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_files_delete(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Bad JSON"}, status=400)

    full, norm = safe_path(data.get("path", ""))
    if norm == "/":
        return web.json_response({"error": "Cannot delete root"}, status=400)

    try:
        if os.path.isdir(full) and not os.path.islink(full):
            shutil.rmtree(full)
        elif os.path.exists(full):
            os.remove(full)
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_files_rename(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Bad JSON"}, status=400)

    src, _ = safe_path(data.get("path", ""))
    dst, dnorm = safe_path(data.get("to", ""))

    if dnorm == "/":
        return web.json_response({"error": "Invalid target"}, status=400)

    try:
        parent = os.path.dirname(dst)
        if parent:
            os.makedirs(parent, exist_ok=True)
        os.replace(src, dst)
        return web.json_response({"ok": True, "path": dnorm})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# ─────────────────────────────────────────────
# WebSocket terminal
# ─────────────────────────────────────────────

async def terminal(request):

    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=1024 * 1024)
    await ws.prepare(request)

    process = None
    master_fd = None
    slave_fd = None
    sender_task = None
    output_queue = None
    loop = asyncio.get_running_loop()

    try:
        # ── Authentication ─────────────────────
        try:
            first = await asyncio.wait_for(ws.receive(), timeout=15)
        except asyncio.TimeoutError:
            await ws.close()
            return ws

        if first.type != WSMsgType.TEXT:
            await ws.close()
            return ws

        traffic.add(len(first.data))

        try:
            auth = json.loads(first.data)
        except Exception:
            await ws.close()
            return ws

        if auth.get("type") != "auth" or auth.get("password") != PASSWORD:
            msg = json.dumps({"type": "auth", "success": False})
            await ws.send_str(msg)
            traffic.add(len(msg))
            await asyncio.sleep(0.5)
            await ws.close()
            return ws

        ok = json.dumps({"type": "auth", "success": True})
        await ws.send_str(ok)
        traffic.add(len(ok))

        # ── PTY ───────────────────────────────
        master_fd, slave_fd = pty.openpty()

        env = os.environ.copy()
        env.update({
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "HOME": DATA_DIR,
            "USER": "root",
            "SHELL": "/bin/bash",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        })

        process = subprocess.Popen(
            ["/bin/bash", "--login"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=DATA_DIR,
            env=env,
            preexec_fn=os.setsid,
            close_fds=True,
        )

        os.close(slave_fd)
        slave_fd = None

        output_queue = asyncio.Queue(maxsize=200)

        def read_pty():
            try:
                data = os.read(master_fd, 8192)
                if data:
                    loop.call_soon_threadsafe(_put, data)
                else:
                    loop.call_soon_threadsafe(_put, None)
            except (OSError, IOError):
                loop.call_soon_threadsafe(_put, None)

        def _put(item):
            try:
                output_queue.put_nowait(item)
            except asyncio.QueueFull:
                try:
                    output_queue.get_nowait()
                    output_queue.put_nowait(item)
                except Exception:
                    pass

        loop.add_reader(master_fd, read_pty)

        async def sender():
            while True:
                data = await output_queue.get()
                if data is None:
                    break
                try:
                    await ws.send_bytes(data)
                    traffic.add(len(data))
                except Exception:
                    break

        sender_task = asyncio.create_task(sender())

        # ── Main loop ─────────────────────────
        async for message in ws:

            if message.type == WSMsgType.TEXT:
                traffic.add(len(message.data))

                try:
                    data = json.loads(message.data)
                except Exception:
                    continue

                mtype = data.get("type")

                if mtype == "input":
                    value = data.get("data", "")
                    if value and master_fd is not None:
                        try:
                            os.write(master_fd, value.encode("utf-8", "ignore"))
                        except OSError:
                            break

                elif mtype == "resize":
                    if master_fd is None:
                        continue
                    try:
                        rows = max(2, min(int(data.get("rows", 24)), 200))
                        cols = max(10, min(int(data.get("cols", 80)), 300))
                        size = struct.pack("HHHH", rows, cols, 0, 0)
                        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, size)
                    except Exception:
                        pass

            elif message.type == WSMsgType.BINARY:
                traffic.add(len(message.data))
                if master_fd is not None:
                    try:
                        os.write(master_fd, bytes(message.data))
                    except OSError:
                        break

            elif message.type == WSMsgType.PING:
                await ws.pong()

            elif message.type == WSMsgType.ERROR:
                break

    except asyncio.CancelledError:
        pass
    except Exception as e:
        print("Terminal error:", repr(e))

    finally:
        if master_fd is not None:
            try:
                loop.remove_reader(master_fd)
            except Exception:
                pass

        if sender_task and output_queue is not None:
            try:
                output_queue.put_nowait(None)
            except Exception:
                pass
            try:
                await asyncio.wait_for(sender_task, timeout=1)
            except Exception:
                sender_task.cancel()

        if process is not None:
            try:
                if process.poll() is None:
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except Exception:
                pass
            try:
                process.wait(timeout=1)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

        if master_fd is not None:
            try:
                os.close(master_fd)
            except Exception:
                pass

        if slave_fd is not None:
            try:
                os.close(slave_fd)
            except Exception:
                pass

    return ws


# ─────────────────────────────────────────────
# Application
# ─────────────────────────────────────────────

app = web.Application(client_max_size=16 * 1024 * 1024)

app.router.add_get("/", index)
app.router.add_get("/ws", terminal)

app.router.add_post("/api/login", api_login)
app.router.add_get("/api/stats", api_stats)
app.router.add_get("/health", health)

app.router.add_get("/api/files/list", api_files_list)
app.router.add_get("/api/files/read", api_files_read)
app.router.add_post("/api/files/write", api_files_write)
app.router.add_post("/api/files/mkdir", api_files_mkdir)
app.router.add_post("/api/files/create", api_files_create)
app.router.add_post("/api/files/delete", api_files_delete)
app.router.add_post("/api/files/rename", api_files_rename)


# ⬇⬇⬇  این دو تا تابع async هستن، پس aiohttp اونا رو await می‌کنه  ⬇⬇⬇
# ولی چون خودشون بلافاصله return می‌کنن (فقط task می‌سازن)، startup بلاک نمی‌شه

async def _on_startup(app):
    asyncio.create_task(traffic_flusher())
    print(f"✅ Twin Terminal ready on 0.0.0.0:{PORT}", flush=True)


async def _on_shutdown(app):
    traffic.save()
    print("👋 shutting down", flush=True)


app.on_startup.append(_on_startup)
app.on_shutdown.append(_on_shutdown)


if __name__ == "__main__":
    print("╭──────────────────────────────────────╮", flush=True)
    print("│        ⚡ TWIN TERMINAL  v2          │", flush=True)
    print("╰──────────────────────────────────────╯", flush=True)
    print(f"Listening on 0.0.0.0:{PORT}", flush=True)
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None)
