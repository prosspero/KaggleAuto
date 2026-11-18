#!/usr/bin/env python3
"""
KaggleAuto GPU Notebook — Ollama Qwen3.6:35b with Cloudflare Tunnel
===================================================================
Designed for Kaggle GPU T4 x2 (32 GB VRAM total).
Installs Ollama, pulls and serves qwen3.6:35b on port 11434, exposes it
via Cloudflare Named Tunnel, writes heartbeats, and gracefully shuts down
at 11.5 hours before Kaggle's 12-hour session limit.
"""

import os
import sys
import json
import time
import signal
import subprocess
import threading
import datetime
import urllib.request
import urllib.error

# ── Tunables ─────────────────────────────────────────────────────────────
OLLAMA_PORT = 11434
HEARTBEAT_PATH = "/kaggle/working/heartbeat.json"
HEARTBEAT_INTERVAL = 60          # seconds
MAX_RUNTIME_HOURS = 11.5         # auto-shutdown at 11.5 hours
CLOUDFLARED_BIN = "/usr/local/bin/cloudflared"
CLOUDFLARED_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"

START_TIME = time.time()
MAX_RUNTIME_SEC = MAX_RUNTIME_HOURS * 3600

# ── 1. Detect GPUs and select Model ──────────────────────────────────────
def get_gpu_count() -> int:
    try:
        out = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True).stdout
        return len(out.strip().splitlines())
    except Exception:
        return 1

n_gpu = get_gpu_count()
MODEL = "qwen3.6:35b" if n_gpu >= 2 else "qwen3.5:9b"
print(f"▸ GPUs detected: {n_gpu} -> Target model: {MODEL}")

# ── 2. Install dependencies (apt + Ollama + cloudflared) ─────────────────
print("▸ Installing system tools (pciutils, lshw, zstd)...")
subprocess.run("apt-get update -qq && apt-get install -y -qq pciutils lshw zstd", shell=True)

print("▸ Installing Ollama...")
subprocess.run("curl -fsSL https://ollama.com/install.sh | sh", shell=True)

print("▸ Downloading cloudflared...")
subprocess.run(
    f"wget -qO {CLOUDFLARED_BIN} {CLOUDFLARED_URL} && chmod +x {CLOUDFLARED_BIN}",
    shell=True,
)

# ── 3. Configure Ollama Environment ──────────────────────────────────────
# Using /tmp/ollama-data for ample disk space on Kaggle
os.environ.update({
    "OLLAMA_HOST": f"0.0.0.0:{OLLAMA_PORT}",
    "OLLAMA_MODELS": "/tmp/ollama-data",
    "OLLAMA_KEEP_ALIVE": "-1",
    "OLLAMA_CONTEXT_LENGTH": "32768",
    "OLLAMA_ORIGINS": "*",
})

# ── 4. Retrieve Cloudflare Tunnel Token ───────────────────────────────────
TUNNEL_TOKEN = os.environ.get("CLOUDFLARE_TUNNEL_TOKEN", "")
if not TUNNEL_TOKEN:
    try:
        from kaggle_secrets import UserSecretsClient
        TUNNEL_TOKEN = UserSecretsClient().get_secret("CLOUDFLARE_TUNNEL_TOKEN")
    except Exception:
        pass

if not TUNNEL_TOKEN or TUNNEL_TOKEN.startswith("__"):
    TUNNEL_TOKEN = "__CLOUDFLARE_TUNNEL_TOKEN__"

if not TUNNEL_TOKEN or TUNNEL_TOKEN.startswith("__"):
    print("⚠ WARNING: CLOUDFLARE_TUNNEL_TOKEN not provided!", file=sys.stderr)
    TUNNEL_TOKEN = ""

# ── 5. Start Ollama Server ───────────────────────────────────────────────
print(f"▸ Starting Ollama server on 0.0.0.0:{OLLAMA_PORT}...")
ollama_proc = subprocess.Popen(["ollama", "serve"])
time.sleep(8)

# Verify Ollama is responsive
def wait_for_ollama(timeout: int = 120) -> bool:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{OLLAMA_PORT}/api/tags"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(3)
    return False

if not wait_for_ollama():
    print("✖ Ollama failed to start. Aborting.", file=sys.stderr)
    sys.exit(1)

print("✔ Ollama daemon is running.")

# Bridge port 8000 -> 11434 in case Cloudflare Tunnel route is set to 8000
def _start_port_proxy():
    import socket, select
    def _forward(s1, s2):
        try:
            while True:
                r, _, _ = select.select([s1, s2], [], [], 60)
                if not r: break
                for s in r:
                    other = s2 if s is s1 else s1
                    data = s.recv(65536)
                    if not data: return
                    other.sendall(data)
        except Exception:
            pass
        finally:
            s1.close()
            s2.close()

    def _listen():
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("0.0.0.0", 8000))
            srv.listen(128)
            while True:
                cl, _ = srv.accept()
                tgt = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    tgt.connect(("127.0.0.1", OLLAMA_PORT))
                    threading.Thread(target=_forward, args=(cl, tgt), daemon=True).start()
                except Exception:
                    cl.close()
        except Exception:
            pass

    threading.Thread(target=_listen, daemon=True).start()

_start_port_proxy()

# ── 6. Start Cloudflare Tunnel Immediately ───────────────────────────────
# Starts immediately so Cloudflare recognizes the connector as Healthy/Active (UP)
# within the first 2 minutes, rather than waiting 30+ minutes for model download.
cf_proc = None
if TUNNEL_TOKEN:
    cf_cmd = [
        CLOUDFLARED_BIN, "tunnel",
        "--protocol", "http2",
        "run",
        "--token", TUNNEL_TOKEN,
    ]
    print("▸ Starting Cloudflare tunnel connector (protocol: http2 over TCP)...")
    cf_log = open("/tmp/cloudflared.log", "a")
    cf_proc = subprocess.Popen(
        cf_cmd,
        stdout=cf_log,
        stderr=subprocess.STDOUT,
    )
    time.sleep(5)
    if cf_proc.poll() is not None:
        print("⚠ cloudflared exited early — check your token.", file=sys.stderr)
        cf_proc = None
    else:
        print(f"✔ Cloudflare tunnel connected and running (PID {cf_proc.pid})")
else:
    print("⚠ Skipping Cloudflare tunnel (no token provided).")

# ── 7. Pull and Warmup Model ─────────────────────────────────────────────
print(f"▸ Pulling model {MODEL} (this may take a few minutes)...")
pull_res = subprocess.run(["ollama", "pull", MODEL], capture_output=True, text=True)
print(pull_res.stdout[-300:] if pull_res.stdout else "")

print("▸ Warming up model into GPU memory...")
try:
    warmup_req = urllib.request.Request(
        f"http://127.0.0.1:{OLLAMA_PORT}/api/chat",
        data=json.dumps({
            "model": MODEL,
            "stream": False,
            "think": False,
            "messages": [{"role": "user", "content": "Reply with exactly: ok"}]
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(warmup_req, timeout=900) as resp:
        body = json.loads(resp.read().decode())
        print(f"✔ Warmup success: {body.get('message', {}).get('content', 'OK')}")
except Exception as e:
    print(f"⚠ Warmup request note: {e}")

# ── 8. Heartbeat Thread ──────────────────────────────────────────────────
_shutdown_event = threading.Event()

def _gpu_util() -> list:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=index,utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            text=True, timeout=10
        )
        gpus = []
        for line in out.strip().splitlines():
            p = [x.strip() for x in line.split(",")]
            gpus.append({
                "index": int(p[0]),
                "gpu_util_pct": int(p[1]),
                "mem_used_mb": int(p[2]),
                "mem_total_mb": int(p[3]),
            })
        return gpus
    except Exception:
        return []

def _model_status() -> str:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{OLLAMA_PORT}/api/tags", timeout=5) as r:
            return "healthy" if r.status == 200 else "degraded"
    except Exception:
        return "unreachable"

def heartbeat_loop():
    while not _shutdown_event.is_set():
        elapsed = time.time() - START_TIME
        remaining = MAX_RUNTIME_SEC - elapsed
        beat = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "uptime_seconds": round(elapsed, 1),
            "remaining_sec": round(max(remaining, 0), 1),
            "model": MODEL,
            "model_status": _model_status(),
            "gpu": _gpu_util(),
            "tunnel_active": cf_proc is not None and cf_proc.poll() is None,
            "ollama_pid": ollama_proc.pid if ollama_proc else None,
        }
        try:
            tmp = HEARTBEAT_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(beat, f, indent=2)
            os.replace(tmp, HEARTBEAT_PATH)
        except OSError:
            pass
        _shutdown_event.wait(HEARTBEAT_INTERVAL)

hb_thread = threading.Thread(target=heartbeat_loop, daemon=True)
hb_thread.start()
print(f"✔ Heartbeat thread started -> {HEARTBEAT_PATH}")

# ── 9. Graceful Shutdown ─────────────────────────────────────────────────
def graceful_shutdown(reason: str = "unknown"):
    print(f"\n▸ Initiating graceful shutdown — reason: {reason}")
    _shutdown_event.set()

    for name, proc in [("cloudflared", cf_proc), ("ollama", ollama_proc)]:
        if proc and proc.poll() is None:
            print(f"  ... stopping {name} (PID {proc.pid})")
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()

    print("✔ Shutdown complete.")
    sys.exit(0)

def _signal_handler(sig, frame):
    graceful_shutdown(reason=f"signal_{sig}")

signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)

# ── 10. Main Execution Loop ──────────────────────────────────────────────
print("\n" + "=" * 60)
print(f"  KaggleAuto Ollama Server Active")
print(f"  Model   : {MODEL}")
print(f"  API     : http://localhost:{OLLAMA_PORT}/v1")
print(f"  Tunnel  : {'active' if cf_proc else 'inactive'}")
print(f"  Limit   : {MAX_RUNTIME_HOURS} hours")
print("=" * 60 + "\n")

try:
    while True:
        elapsed = time.time() - START_TIME
        if elapsed >= MAX_RUNTIME_SEC:
            graceful_shutdown(reason="max_runtime_reached")

        # Watch for Ollama process death
        if ollama_proc.poll() is not None:
            print("⚠ Ollama server died unexpectedly.", file=sys.stderr)
            graceful_shutdown(reason="ollama_crashed")

        # Restart cloudflared if it drops
        if cf_proc and cf_proc.poll() is not None and TUNNEL_TOKEN:
            print("⚠ cloudflared stopped — restarting...")
            cf_proc = subprocess.Popen(
                [CLOUDFLARED_BIN, "tunnel", "--no-autoupdate", "run", "--token", TUNNEL_TOKEN],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )

        time.sleep(30)
except KeyboardInterrupt:
    graceful_shutdown(reason="keyboard_interrupt")
