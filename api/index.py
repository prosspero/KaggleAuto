from http.server import BaseHTTPRequestHandler
import json
import time
import urllib.request
import urllib.error
from pathlib import Path

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        state_file = Path(__file__).parent.parent / "state.json"
        data = {}
        if state_file.exists():
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                pass
        
        # Ping the model endpoint from server-side (bypasses browser CORS)
        health = {"online": False, "latency_ms": None}
        try:
            t0 = time.time()
            req = urllib.request.Request(
                "https://qwen.prosspero.com/api/tags",
                headers={"User-Agent": "KaggleAutoDashboard/1.0"}
            )
            with urllib.request.urlopen(req, timeout=3.0) as resp:
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
