"""Local ACP test agent with handoff, empty-response, and completion modes.

This fixture performs no network requests and never reads or edits the target
project. It implements only the ACP messages needed by the orchestrator test.
"""
from __future__ import annotations
import json
import sys

mode = sys.argv[1] if len(sys.argv) > 1 else "complete"

RESPONSES = {
    "handoff": "MOCK_KIRO_HANDOFF\nORCHESTRATOR_STATUS: HANDOFF",
    "empty": "",
    "complete": "MOCK_COPILOT_COMPLETE\nORCHESTRATOR_STATUS: COMPLETE",
}
if mode not in RESPONSES:
    raise SystemExit(f"Unsupported mock mode: {mode}")

for raw in sys.stdin:
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        continue

    method = message.get("method")
    request_id = message.get("id")

    if method == "initialize":
        reply = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": 1,
                "agentInfo": {"name": f"mock-{mode}", "version": "1.0"},
                "agentCapabilities": {},
            },
        }
        print(json.dumps(reply), flush=True)
    elif method == "initialized":
        pass
    elif method == "session/new":
        print(json.dumps({
            "jsonrpc": "2.0", "id": request_id,
            "result": {"sessionId": f"mock-session-{mode}"},
        }), flush=True)
    elif method == "session/prompt":
        response_text = RESPONSES[mode]
        if response_text:
            update = {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": response_text},
            }
            print(json.dumps({
                "jsonrpc": "2.0", "method": "session/update",
                "params": {
                    "sessionId": f"mock-session-{mode}",
                    "update": update,
                },
            }), flush=True)
        # Empty mode intentionally omits session/update text, while still
        # returning a normal end_turn result to reproduce Cline's observed case.
        print(json.dumps({
            "jsonrpc": "2.0", "id": request_id,
            "result": {"stopReason": "end_turn"},
        }), flush=True)
    elif request_id is not None:
        print(json.dumps({
            "jsonrpc": "2.0", "id": request_id,
            "error": {"code": -32601, "message": "Method not found"},
        }), flush=True)
