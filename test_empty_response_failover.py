#!/usr/bin/env python3
"""Network-free integration test for empty-response automatic agent failover.

Run from the orchestrator folder:
    python .\test_empty_response_failover.py

The test copies orchestrator.py into a temporary sandbox and gives it three
local mock ACP agents: Kiro returns HANDOFF, Cline returns an empty end_turn,
and Copilot returns COMPLETE. It asserts the orchestrator proceeds automatically
through all three agents and completes. No live providers are contacted.
"""
from __future__ import annotations
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SOURCE_DIR = Path(__file__).resolve().parent
ORCHESTRATOR_SOURCE = SOURCE_DIR / "orchestrator.py"
MOCK_SOURCE = SOURCE_DIR / "mock_failover_agent.py"


def main() -> int:
    if not ORCHESTRATOR_SOURCE.is_file():
        print(f"FAIL: orchestrator.py not found at {ORCHESTRATOR_SOURCE}", file=sys.stderr)
        return 2
    if not MOCK_SOURCE.is_file():
        print(f"FAIL: mock_failover_agent.py not found at {MOCK_SOURCE}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="agent-orchestrator-failover-test-") as temp:
        root = Path(temp)
        app_dir = root / "orchestrator"
        project_dir = root / "sandbox-project"
        app_dir.mkdir()
        project_dir.mkdir()

        shutil.copy2(ORCHESTRATOR_SOURCE, app_dir / "orchestrator.py")
        guard_src = ORCHESTRATOR_SOURCE.with_name("safety_guard.py")
        if guard_src.exists():
            shutil.copy2(guard_src, app_dir / "safety_guard.py")
        shutil.copy2(MOCK_SOURCE, app_dir / "mock_failover_agent.py")

        mock_path = str(app_dir / "mock_failover_agent.py")
        agents = [
            {
                "name": "kiro", "label": "Mock Kiro handoff", "type": "acp",
                "enabled": True, "command": sys.executable,
                "args": [mock_path, "handoff"], "timeout_seconds": 10,
            },
            {
                "name": "cline", "label": "Mock Cline empty response", "type": "acp",
                "enabled": True, "command": sys.executable,
                "args": [mock_path, "empty"], "timeout_seconds": 10,
            },
            {
                "name": "copilot", "label": "Mock Copilot completion", "type": "acp",
                "enabled": True, "command": sys.executable,
                "args": [mock_path, "complete"], "timeout_seconds": 10,
            },
        ]
        config = {
            "project_path": str(project_dir),
            "agents": agents,
            "ollama": {
                "model": "not-used-in-this-test",
                "api_url": "http://127.0.0.1:1/api/chat",
                "timeout_seconds": 1,
            },
            "safety": {
                "paid_fallback": False,
                "auto_approve_tools": False,
                "persist_tool_approvals": False,
                "continue_after_user_denial": False,
                "project_mutation_requires_confirmation": True,
            },
        }
        (app_dir / "config.json").write_text(
            json.dumps(config, indent=2), encoding="utf-8"
        )

        cmd = [
            sys.executable, str(app_dir / "orchestrator.py"),
            "--confirm", "--project", str(project_dir),
            "--task",
            "LOCAL TEST ONLY. Do not use tools or make changes. Follow the test agent instruction.",
        ]
        try:
            result = subprocess.run(
                cmd, cwd=str(app_dir), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=45,
            )
        except subprocess.TimeoutExpired:
            print("FAIL: test orchestration timed out.", file=sys.stderr)
            return 1

        combined = (result.stdout or "") + "\n" + (result.stderr or "")
        state_path = app_dir / "state" / "latest.json"
        if not state_path.is_file():
            print(combined)
            print("FAIL: orchestrator did not save state.", file=sys.stderr)
            return 1

        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(combined)
            print(f"FAIL: unreadable saved state: {exc}", file=sys.stderr)
            return 1

        attempts = state.get("attempts", [])
        observed = [(a.get("agent"), a.get("outcome")) for a in attempts]
        expected = [
            ("kiro", "handoff"),
            ("cline", "failed"),
            ("copilot", "complete"),
        ]
        checks = {
            "orchestrator exit code is 0": result.returncode == 0,
            "Kiro handoff marker was received": "MOCK_KIRO_HANDOFF" in combined,
            "empty Cline response classified as failed": "cline ended without assistant text" in combined.lower(),
            "Copilot mock was launched after Cline": combined.find("TRYING Mock Copilot completion") > combined.find("TRYING Mock Cline empty response"),
            "Copilot completion marker was received": "MOCK_COPILOT_COMPLETE" in combined,
            "saved attempt order/outcomes match": observed == expected,
            "saved run status is completed": state.get("status") == "completed",
            "test project remains file-empty": not any(project_dir.iterdir()),
        }
        print("===== LOCAL EMPTY-RESPONSE FAILOVER TEST =====")
        print(combined.strip())
        print("\n===== ASSERTIONS =====")
        for label, passed in checks.items():
            print(f"{'PASS' if passed else 'FAIL'}: {label}")
        print("Observed attempts:", observed)
        print("Saved run status:", state.get("status"))
        print("No cloud API requests were made. Test ran in a disposable temporary folder.")

        if all(checks.values()):
            print("\nEMPTY-RESPONSE FAILOVER TEST PASSED.")
            return 0
        print("\nEMPTY-RESPONSE FAILOVER TEST FAILED.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
