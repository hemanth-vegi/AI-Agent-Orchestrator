import http.server
import json
import socketserver
import subprocess
import sys
import time
from pathlib import Path

PORT = 8765
ORCH_DIR = Path(__file__).resolve().parent
ORCH_SCRIPT = ORCH_DIR / "orchestrator.py"
STATE_FILE = ORCH_DIR / "state" / "latest.json"

class OrchestratorProxyHandler(http.server.BaseHTTPRequestHandler):
    def _send_json(self, data, status=200):
        raw = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            self._send_json({
                "object": "list",
                "data": [{"id": "auto-failover-orchestrator", "object": "model", "owned_by": "local-orchestrator"}]
            })
        else:
            self._send_json({"status": "ok", "service": "AI-Agent-Orchestrator Proxy"})

    def do_POST(self):
        if not self.path.startswith("/v1/chat/completions"):
            self._send_json({"error": "Not found"}, 404)
            return

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        messages = body.get("messages", [])
        stream = body.get("stream", False)

        user_prompt = "Hello"
        for m in reversed(messages):
            if m.get("role") == "user":
                content = m.get("content", "")
                if isinstance(content, list):
                    user_prompt = " ".join(
                        part.get("text", "") for part in content if isinstance(part, dict)
                    )
                else:
                    user_prompt = str(content)
                break

        # Run orchestrator.py with automatic one-time tool approvals piped to stdin
        proc = subprocess.Popen(
            [sys.executable, str(ORCH_SCRIPT), "--task", user_prompt, "--confirm"],
            cwd=str(ORCH_DIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace"
        )
        stdout_text, _ = proc.communicate(input=("1\n" * 60))

        # Build a clean summary showing any model failovers + final response
        reply_parts = []
        if STATE_FILE.exists():
            try:
                st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                attempts = st.get("attempts", [])
                if len(attempts) > 1:
                    chain = " -> ".join(f"`{a.get('agent')}` ({a.get('outcome')})" for a in attempts)
                    reply_parts.append(f"**[Failover Cascade Triggered]:** {chain}\n")
                elif attempts:
                    a = attempts[-1]
                    reply_parts.append(f"**[Active Agent]:** `{a.get('agent')}` | **Outcome:** `{a.get('outcome')}`\n")
                if attempts and attempts[-1].get("response"):
                    reply_parts.append(attempts[-1]["response"])
            except Exception:
                pass

        if not reply_parts:
            reply_parts.append(stdout_text[-3000:] if stdout_text else "Task executed.")

        final_text = "\n\n".join(reply_parts)

        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            chunk = {
                "id": f"chatcmpl-{int(time.time())}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "auto-failover-orchestrator",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": final_text}, "finish_reason": "stop"}]
            }
            self.wfile.write(f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode("utf-8"))
        else:
            self._send_json({
                "id": f"chatcmpl-{int(time.time())}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "auto-failover-orchestrator",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": final_text},
                    "finish_reason": "stop"
                }]
            })

if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", PORT), OrchestratorProxyHandler) as httpd:
        print(f"Orchestrator Right-Sidebar Chat Proxy listening on http://127.0.0.1:{PORT}/v1")
        httpd.serve_forever()
