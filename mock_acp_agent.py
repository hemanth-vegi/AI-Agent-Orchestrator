"""Tiny local ACP protocol fixture used by --self-test; no network access."""
import json
import sys

for line in sys.stdin:
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue
    method = msg.get("method")
    ident = msg.get("id")
    if method == "initialize":
        result = {"protocolVersion": 1, "agentInfo": {"name": "mock-acp", "version": "1"}, "agentCapabilities": {}}
        print(json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}), flush=True)
    elif method == "initialized":
        pass
    elif method == "session/new":
        print(json.dumps({"jsonrpc": "2.0", "id": ident, "result": {"sessionId": "mock-session"}}), flush=True)
    elif method == "session/prompt":
        update = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "MOCK_OK\nORCHESTRATOR_STATUS: COMPLETE"}}
        print(json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "mock-session", "update": update}}), flush=True)
        print(json.dumps({"jsonrpc": "2.0", "id": ident, "result": {"stopReason": "end_turn"}}), flush=True)
    else:
        if ident is not None:
            print(json.dumps({"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "Method not found"}}), flush=True)
