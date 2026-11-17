from http.server import BaseHTTPRequestHandler
import json
import time
import urllib.request
import urllib.error
from pathlib import Path

GITHUB_STATE_URL = "https://raw.githubusercontent.com/prosspero/KaggleAuto/main/state.json"
HEALTH_CHECK_URL = "https://qwen.prosspero.com/api/tags"

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        data = {}
        # 1. Fetch latest state from GitHub raw (always up to date)
        try:
            req = urllib.request.Request(
                GITHUB_STATE_URL + f"?_t={int(time.time())}",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            )
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            pass

        # 2. Fallback to local state.json if remote failed
        if not data:
            state_file = Path(__file__).parent.parent / "state.json"
            if state_file.exists():
                try:
                    with open(state_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    pass

        # 3. Server-side health check against Cloudflare tunnel (bypasses browser CORS)
        health = {"online": False, "latency_ms": None}
        try:
            t0 = time.time()
            req = urllib.request.Request(
                HEALTH_CHECK_URL,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
            )
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                if resp.status == 200:
                    health["online"] = True
                    health["latency_ms"] = int((time.time() - t0) * 1000)
        except Exception as e:
            health["error"] = str(e)

        data["live_health"] = health

        body = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
