from http.server import BaseHTTPRequestHandler
import json
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
        
        body = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
