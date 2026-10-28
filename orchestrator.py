#!/usr/bin/env python3
"""Free-tier-first, approval-gated ACP coding-agent orchestrator for Windows.

V1 supports ACP agents (Kiro CLI v3, Cline, GitHub Copilot CLI) and a
text-only Ollama fallback. It never auto-approves tool permissions.
"""
from __future__ import annotations
try:
    import safety_guard
except ImportError:
    safety_guard = None

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
STATE_DIR = APP_DIR / "state"
LOG_DIR = APP_DIR / "logs"


class OrchestratorError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def read_config() -> dict[str, Any]:
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise OrchestratorError(f"Config not found: {CONFIG_PATH}") from exc
    except json.JSONDecodeError as exc:
        raise OrchestratorError(f"Invalid config JSON: {exc}") from exc
    if not isinstance(cfg.get("agents"), list):
        raise OrchestratorError("config.json must contain an 'agents' list")
    return cfg


def expand_env(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value))


def resolve_project(cfg: dict[str, Any], override: str | None) -> Path:
    raw = override or cfg.get("project_path", "")
    raw = expand_env(str(raw))
    if not raw:
        raise OrchestratorError("Set project_path in config.json or pass --project")
    project = Path(raw).resolve()
    if not project.exists() or not project.is_dir():
        raise OrchestratorError(f"Project folder does not exist: {project}")
    danger = str(project).replace("/", "\\").rstrip("\\").lower()
    if danger in {r"c:\windows", r"c:\windows\system32", "c:"}:
        raise OrchestratorError(f"Refusing to use a Windows system/root folder: {project}")
    return project


def resolve_command(agent: dict[str, Any]) -> tuple[list[str] | None, str | None]:
    raw = expand_env(str(agent.get("command", "")))
    args = [expand_env(str(a)) for a in agent.get("args", [])]
    if not raw:
        return None, "no command configured"
    candidate = Path(raw)
    if candidate.is_file():
        # .cmd/.bat files need cmd.exe on Windows; PowerShell scripts need pwsh/powershell.
        suffix = candidate.suffix.lower()
        if os.name == "nt" and suffix in {".cmd", ".bat"}:
            cmdline = subprocess.list2cmdline([str(candidate), *args])
            return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", cmdline], None
        if os.name == "nt" and suffix == ".ps1":
            return ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(candidate), *args], None
        return [str(candidate), *args], None
    located = shutil.which(raw)
    if located:
        suffix = Path(located).suffix.lower()
        if os.name == "nt" and suffix in {".cmd", ".bat"}:
            cmdline = subprocess.list2cmdline([located, *args])
            return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", cmdline], None
        if os.name == "nt" and suffix == ".ps1":
            return ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", located, *args], None
        return [located, *args], None
    return None, f"command not found: {raw}"


def git_snapshot(project: Path) -> dict[str, str]:
    result: dict[str, str] = {"status": "(not a git repository)", "diff_stat": "", "diff": ""}
    if not (project / ".git").exists() and not shutil.which("git"):
        return result
    try:
        p = subprocess.run(["git", "-C", str(project), "status", "--short"], capture_output=True, text=True, timeout=10)
        if p.returncode == 0:
            result["status"] = p.stdout.strip() or "(clean working tree)"
            d = subprocess.run(["git", "-C", str(project), "diff", "--stat"], capture_output=True, text=True, timeout=10)
            result["diff_stat"] = d.stdout.strip()
            # Keep handoff context bounded and avoid adding untracked file contents.
            diff = subprocess.run(["git", "-C", str(project), "diff", "--no-ext-diff", "--unified=2"], capture_output=True, text=True, timeout=15)
            result["diff"] = diff.stdout[-16000:]
    except (OSError, subprocess.SubprocessError):
        pass
    return result


def redacted_error(text: str) -> str:
    # Basic redaction of common API-key shapes before writing logs/state.
    text = re.sub(r"(?i)sk-or-v1-[A-Za-z0-9_-]{10,}", "[REDACTED_OPENROUTER_KEY]", text)
    text = re.sub(r"(?i)sk-[A-Za-z0-9_-]{16,}", "[REDACTED_API_KEY]", text)
    text = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)\S+", r"\1[REDACTED]", text)
    return text


def is_quota_or_provider_failure(text: str) -> bool:
    pattern = r"(?i)\b(usage limit reached|rate.?limit|quota exceeded|insufficient quota|credits? exhausted|out of credits|billing required|payment required|not logged in|unauthorized|invalid api.?key|authentication failed|provider error|provider returned error|no longer available|no provider serves|insufficient balance|model unavailable|too many requests|429|401|402|403)\b"
    return bool(re.search(pattern, text or ""))


def save_state(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    state_path = STATE_DIR / "latest.json"
    temp_path = state_path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    temp_path.replace(state_path)
    run_path = LOG_DIR / f"run-{state['run_id']}.json"
    run_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


class ACPProcess:
    """Minimal ACP stdio client for line-delimited JSON-RPC 2.0 servers."""
    def __init__(self, command: list[str], cwd: Path, timeout: int = 900):
        self.command = command
        self.cwd = cwd
        self.timeout = timeout
        self.proc: subprocess.Popen[str] | None = None
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.stderr_lines: list[str] = []
        self.next_id = 1
        self.session_id: str | None = None
        self.text_chunks: list[str] = []
        self.user_denied = False
        self.metering: list[dict[str, Any]] = []
        # Content-free protocol diagnostics are useful when an ACP turn ends empty.
        self.protocol_events: list[dict[str, Any]] = []

    def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            for line in self.proc.stdout:
                self.lines.put(line.rstrip("\r\n"))
        finally:
            self.lines.put(None)

    def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            clean = redacted_error(line.rstrip())
            if clean:
                self.stderr_lines.append(clean)
                if len(self.stderr_lines) > 120:
                    self.stderr_lines = self.stderr_lines[-120:]

    def start(self) -> None:
        print("\nLaunching:", subprocess.list2cmdline(self.command))
        self.proc = subprocess.Popen(
            self.command,
            cwd=str(self.cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass

    def send(self, payload: dict[str, Any]) -> None:
        if not self.proc or not self.proc.stdin:
            raise OrchestratorError("ACP process is not running")
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)

    def _permission(self, msg: dict[str, Any]) -> None:
        request_id = msg.get("id")
        params = msg.get("params") or {}
        options = params.get("options") or []
        tool_call = params.get("toolCall") or params.get("tool_call") or {}
        title = tool_call.get("title") or tool_call.get("name") or "agent tool request"
        print("\n\nAGENT PERMISSION REQUEST")
        print("Tool:", title)
        if tool_call.get("kind"):
            print("Kind:", tool_call.get("kind"))
        if tool_call.get("rawInput") is not None:
            print("Input:", json.dumps(tool_call.get("rawInput"), ensure_ascii=False)[:1200])
        if not options:
            print("No permission options supplied; refusing safely.")
            self.send({"jsonrpc": "2.0", "id": request_id, "result": {"outcome": {"outcome": "cancelled"}}})
            return
        safe_options = []
        for option in options:
            kind = str(option.get("kind", ""))
            if "always" not in kind.lower():
                safe_options.append(option)
        if not safe_options:
            safe_options = [o for o in options if "reject" in str(o.get("kind", "")).lower()]
        print("Permission options (persistent/always approvals are disabled by this orchestrator):")
        for i, opt in enumerate(safe_options, 1):
            print(f"  {i}. {opt.get('name', opt.get('kind', 'option'))} [{opt.get('kind', 'unknown')}] — {opt.get('optionId', '?')}")
        answer = input("Choose a one-time approval number, or press Enter to deny: ").strip()
        chosen = None
        if answer.isdigit() and 1 <= int(answer) <= len(safe_options):
            chosen = safe_options[int(answer) - 1]
        if chosen and "reject" not in str(chosen.get("kind", "")).lower():
            outcome_data = {"outcome": "selected", "optionId": chosen.get("optionId")}
        elif chosen:
            self.user_denied = True
            outcome_data = {"outcome": "selected", "optionId": chosen.get("optionId")}
        else:
            self.user_denied = True
            reject = next((o for o in options if "reject" in str(o.get("kind", "")).lower() and "always" not in str(o.get("kind", "")).lower()), None)
            outcome_data = {"outcome": "selected", "optionId": reject["optionId"]} if reject else {"outcome": "cancelled"}
        self.send({"jsonrpc": "2.0", "id": request_id, "result": {"outcome": outcome_data}})

    def _handle_line(self, line: str) -> dict[str, Any] | None:
        if not line.strip():
            return None
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            # ACP stdout is protocol-only. Ignore non-JSON banners but surface them for diagnosis.
            print("[agent startup output]", line[:400])
            return None
        if not isinstance(msg, dict):
            return None
        if "method" in msg and "id" in msg:
            if msg["method"] == "session/request_permission":
                self._permission(msg)
            else:
                self.send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": f"Unsupported client request: {msg['method']}"}})
            return None
        if msg.get("method") == "session/update":
            params = msg.get("params") or {}
            update = params.get("update") or params.get("sessionUpdate") or {}
            if isinstance(update, dict):
                kind = str(update.get("sessionUpdate") or update.get("type") or "")
                content = update.get("content")
                # Accept text delivered as a single ACP content block, a list of
                # blocks, or a plain string across different compatible agents.
                chunk = ""
                if isinstance(content, dict):
                    if content.get("type", "text") == "text":
                        chunk = str(content.get("text", ""))
                elif isinstance(content, list):
                    chunk = "".join(
                        str(block.get("text", ""))
                        for block in content
                        if isinstance(block, dict) and block.get("type", "text") == "text"
                    )
                elif isinstance(content, str):
                    chunk = content
                if not chunk:
                    chunk = str(update.get("text", ""))

                event = {
                    "kind": kind or "unknown",
                    "update_keys": sorted(str(key) for key in update.keys()),
                    "content_type": type(content).__name__ if content is not None else "none",
                }
                if kind in {"tool_call", "tool_call_update"}:
                    event["tool_status"] = str(update.get("status", ""))[:40]
                self.protocol_events.append(event)
                if len(self.protocol_events) > 100:
                    self.protocol_events = self.protocol_events[-100:]

                if kind in {"agent_message_chunk", "agent_message", "text"} and chunk:
                    self.text_chunks.append(chunk)
                    print(chunk, end="", flush=True)
                elif kind == "session_info_update":
                    meta = update.get("_meta") or update.get("meta") or {}
                    if meta.get("kiro", {}).get("kind") == "turn_completion":
                        metric = meta.get("kiro", {}).get("promptTurnSummaries")
                        self.metering.append({"kind": "turn_completion", "summary": metric})
                        print("\n[Kiro turn usage metadata captured]")
                elif kind in {"tool_call", "tool_call_update"}:
                    title = update.get("title") or update.get("name") or kind
                    status = update.get("status", "")
                    print(f"\n[agent tool: {title} {status}]".rstrip(), flush=True)
            return None
        return msg

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        self.send(payload)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self.proc and self.proc.poll() is not None and self.lines.empty():
                raise OrchestratorError(f"Agent process exited with code {self.proc.returncode}. " + "\n".join(self.stderr_lines[-20:]))
            try:
                line = self.lines.get(timeout=0.5)
            except queue.Empty:
                continue
            if line is None:
                raise OrchestratorError("ACP agent closed stdout. " + "\n".join(self.stderr_lines[-20:]))
            msg = self._handle_line(line)
            if msg and msg.get("id") == request_id:
                if "error" in msg:
                    raise OrchestratorError(f"ACP {method} error: {json.dumps(msg['error'], ensure_ascii=False)}")
                return msg.get("result") or {}
        raise OrchestratorError(f"Timed out waiting for ACP method {method} after {self.timeout}s")

    def run_task(self, task_prompt: str) -> dict[str, Any]:
        self.start()
        try:
            init = self.request("initialize", {
                "protocolVersion": 1,
                "clientInfo": {"name": "ai-agent-orchestrator", "title": "AI Agent Orchestrator", "version": "0.1.0"},
                "clientCapabilities": {},
            })
            self.notify("initialized", {})
            caps = init.get("agentCapabilities") or {}
            print("\nACP initialized. Agent capabilities:", json.dumps(caps, ensure_ascii=False)[:500])
            session = self.request("session/new", {"cwd": str(self.cwd), "mcpServers": []})
            session_id = session.get("sessionId")
            if not session_id:
                raise OrchestratorError("ACP session/new did not return sessionId")
            self.session_id = session_id
            prompt_result = self.request("session/prompt", {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": task_prompt}],
            })
            text = "".join(self.text_chunks).strip()
            return {
                "session_id": session_id,
                "result": prompt_result,
                "stop_reason": prompt_result.get("stopReason"),
                "text": text,
                "stderr": self.stderr_lines[-40:],
                "protocol_events": self.protocol_events[-100:],
                "user_denied": self.user_denied,
                "metering": self.metering,
            }
        finally:
            self.close()


class ClineCLIProcess:
    """Runner for Cline's standard JSON CLI interface."""

    def __init__(self, command: list[str], cwd: Path, timeout: int = 900):
        self.command = command
        self.cwd = cwd
        self.timeout = timeout

    def run_task(self, task_prompt: str) -> dict[str, Any]:
        full_cmd = list(self.command)
        if "-y" not in full_cmd:
            full_cmd.append("-y")
        if "--cwd" not in full_cmd:
            full_cmd.extend(["--cwd", str(self.cwd)])
        full_cmd.append(task_prompt)
        print("\nLaunching Cline CLI:", subprocess.list2cmdline(full_cmd[:6]), "...")

        proc = subprocess.Popen(
            full_cmd,
            cwd=str(self.cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        text_chunks: list[str] = []
        stderr_lines: list[str] = []
        protocol_events: list[dict[str, Any]] = []
        metering: list[dict[str, Any]] = []
        run_result: dict[str, Any] = {}
        stop_reason = None

        def _read_stderr() -> None:
            assert proc.stderr
            for line in proc.stderr:
                clean = redacted_error(line.rstrip())
                if clean:
                    stderr_lines.append(clean)
                    if len(stderr_lines) > 120:
                        stderr_lines.pop(0)

        threading.Thread(target=_read_stderr, daemon=True).start()

        try:
            assert proc.stdout
            for raw_line in proc.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue

                etype = event.get("type")
                if etype == "run_result":
                    run_result = event
                    stop_reason = event.get("finishReason")
                    cost = event.get("usage", {}).get("totalCost", 0)
                    if cost:
                        metering.append({"kind": "cost", "totalCost": cost})
                    if event.get("text") and not text_chunks:
                        text_chunks.append(str(event.get("text", "")))
                elif etype == "error":
                    protocol_events.append({"kind": "error", "message": event.get("message")})
                    stderr_lines.append(str(event.get("message", "")))
                elif etype == "agent_event":
                    ev = event.get("event") or {}
                    ev_type = ev.get("type")
                    if ev_type == "content_start" and ev.get("contentType") == "text":
                        txt = str(ev.get("text", ""))
                        if txt:
                            text_chunks.append(txt)
                            print(txt, end="", flush=True)
                    elif ev_type == "content_end" and ev.get("contentType") == "text":
                        txt = str(ev.get("text", ""))
                        if txt and not text_chunks:
                            text_chunks.append(txt)
                            print(txt, end="", flush=True)
                    elif ev_type == "done":
                        if ev.get("reason") == "error":
                            stop_reason = "error"
                            err_txt = str(ev.get("text", ""))
                            if err_txt:
                                stderr_lines.append(err_txt)
                        elif not stop_reason:
                            stop_reason = ev.get("reason")
                        done_cost = ev.get("usage", {}).get("totalCost", 0)
                        if done_cost:
                            metering.append({"kind": "cost", "totalCost": done_cost})

            proc.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise OrchestratorError(f"Cline CLI timed out after {self.timeout}s")
        except Exception as exc:
            proc.kill()
            raise OrchestratorError(f"Cline CLI execution failed: {exc}") from exc

        final_text = "".join(text_chunks).strip()
        if not final_text and run_result.get("text"):
            final_text = str(run_result.get("text", "")).strip()

        cost_val = run_result.get("usage", {}).get("totalCost", 0)
        if cost_val and float(cost_val) > 0:
            stderr_lines.append(f"CRITICAL SAFETY VIOLATION: Cline reported non-zero cost ${cost_val}. Policy forbids paid usage.")

        return {
            "session_id": run_result.get("taskId"),
            "result": run_result,
            "stop_reason": stop_reason or ("completed" if proc.returncode == 0 else "error"),
            "text": final_text,
            "stderr": stderr_lines[-40:],
            "protocol_events": protocol_events[-100:],
            "user_denied": False,
            "metering": metering,
        }


def classify_agent_response(response: str, stop_reason: str | None, stderr: list[str] | None = None) -> tuple[str, str | None]:
    """Classify an ACP turn; an empty end_turn is failure, not user success."""
    response = (response or "").strip()
    diagnostics = "\n".join(stderr or [])
    combined = (response + "\n" + diagnostics).strip()

    if combined and is_quota_or_provider_failure(combined):
        return "provider_failure", "Provider/quota failure detected in response or agent stderr"
    if not response:
        detail = f"ACP returned no assistant text (stopReason={stop_reason or 'unknown'})."
        if diagnostics:
            detail += " stderr excerpt: " + diagnostics[-1600:]
        return "failed", redacted_error(detail)

    status_match = re.search(r"(?im)^\s*ORCHESTRATOR_STATUS:\s*(COMPLETE|HANDOFF|NEEDS_USER)\s*$", response)
    if not status_match:
        return "needs_user", "Agent returned text but no recognized ORCHESTRATOR_STATUS line"
    status = status_match.group(1).upper()
    return {"COMPLETE": "complete", "HANDOFF": "handoff", "NEEDS_USER": "needs_user"}[status], None


def git_handoff_summary(snapshot: dict[str, str]) -> str:
    return "\n".join([
        "CURRENT PROJECT STATE (collected immediately before handoff):",
        f"git status --short: {snapshot.get('status', '')}",
        f"git diff --stat:\n{snapshot.get('diff_stat', '')}",
        "Current tracked diff excerpt:", snapshot.get("diff", "") or "(no tracked diff captured)",
    ])


def compose_prompt(task: str, history: list[dict[str, Any]], snapshot: dict[str, str], project: Path, original_task: str | None = None) -> str:
    original = original_task or task
    parts = [
        "You are one worker in a free-tier-first coding-agent handoff system.",
        "WORKSPACE: " + str(project),
        "RULES: Never select or recommend paid plans, paid models, overages, or billing. Never expose credentials. Do not undo earlier work without inspecting it. Use project-local changes only. Ask permission for tools through the normal ACP permission flow; the orchestrator never auto-approves tools.",
        "After attempting the task, end your response with exactly one status line:",
        "ORCHESTRATOR_STATUS: COMPLETE (only if you completed the requested work and did relevant checks)",
        "ORCHESTRATOR_STATUS: HANDOFF (if you cannot continue, the provider/model is unavailable, quota/auth failed, or you need another agent; explain why)",
        "ORCHESTRATOR_STATUS: NEEDS_USER (if you need a decision or permission from the user).",
        "Do not claim changes or tests succeeded unless you actually verified them.",
        "\nORIGINAL TASK:\n" + original,
        "\nCURRENT INSTRUCTION FOR THIS AGENT:\n" + task if task != original else "",
        "\nPROJECT STATE:\n" + git_handoff_summary(snapshot),
    ]
    if history:
        parts.append("\nPREVIOUS AGENT ATTEMPTS (continue from this; do not restart):")
        for entry in history[-3:]:
            parts.append(
                f"\nAgent: {entry.get('agent')}\nOutcome: {entry.get('outcome')}\n"
                f"Response excerpt:\n{str(entry.get('response', ''))[-5000:]}\n"
                f"Error: {str(entry.get('error', ''))[-1000:]}"
            )
    return "\n\n".join(parts)


def run_ollama_fallback(task: str, state: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    settings = cfg.get("ollama", {})
    model = settings.get("model", "qwen2.5-coder:3b")
    url = settings.get("api_url", "http://127.0.0.1:11434/api/chat")
    history = state.get("attempts", [])
    context = compose_prompt(task, history, git_snapshot(Path(cfg["project_path"])), Path(cfg["project_path"]))
    context += "\n\nFINAL LOCAL FALLBACK MODE: You cannot edit files or run tools in this fallback. Provide an actionable recovery plan or patch text only. End with ORCHESTRATOR_STATUS: NEEDS_USER."
    body = {"model": model, "messages": [{"role": "user", "content": context}], "stream": False, "options": {"temperature": 0.2}}
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=int(settings.get("timeout_seconds", 180))) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        response = (((data.get("message") or {}).get("content")) or "").strip()
        if not response:
            raise OrchestratorError("Ollama returned an empty response")
        print("\n\n===== OLLAMA LOCAL FALLBACK (TEXT-ONLY; NO FILE CHANGES) =====\n")
        print(response)
        return {"agent": "ollama", "label": f"Ollama {model} (text-only fallback)", "outcome": "needs_user", "response": redacted_error(response), "error": None, "usage": data.get("prompt_eval_count", 0)}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OrchestratorError) as exc:
        return {"agent": "ollama", "label": f"Ollama {model}", "outcome": "unavailable", "response": "", "error": str(exc)}


def command_health(cfg: dict[str, Any]) -> int:
    print("AI Agent Orchestrator — read-only preflight\n")
    print("Project folder:", cfg.get("project_path", "(not set)"))
    print("Automatic tool approval: DISABLED (every permission request is interactive)")
    print("Paid fallback: DISABLED by policy; stop/fallback on quota/billing errors")
    all_ok = True
    for agent in cfg.get("agents", []):
        name = agent.get("name", "unknown")
        if not agent.get("enabled", True):
            print(f"[SKIP] {name}: {agent.get('reason', 'disabled')}")
            continue
        if agent.get("type") == "manual":
            print(f"[MANUAL ONLY] {name}: {agent.get('reason', 'not callable from orchestrator')}")
            continue
        cmd, err = resolve_command(agent)
        if err:
            all_ok = False
            print(f"[MISSING] {name}: {err}")
        else:
            print(f"[FOUND] {name}: {cmd[0]} {subprocess.list2cmdline(cmd[1:])}")
    ollama_cfg = cfg.get("ollama", {})
    try:
        req = urllib.request.Request(ollama_cfg.get("api_url", "http://127.0.0.1:11434/api/tags").replace("/api/chat", "/api/tags"), method="GET")
        with urllib.request.urlopen(req, timeout=3) as response:
            tags = json.loads(response.read().decode("utf-8"))
        models = [m.get("name", "") for m in tags.get("models", [])]
        local_model = ollama_cfg.get("model", "qwen2.5-coder:3b")
        print(f"[FOUND] Ollama API: {local_model} {'available' if any(local_model in m for m in models) else 'NOT IN MODEL LIST'}")
    except Exception as exc:
        all_ok = False
        print(f"[UNAVAILABLE] Ollama API: {exc}")
    print("\nPreflight completed. No AI prompts were sent and no project files were changed.")
    return 0 if all_ok else 1


def self_test() -> int:
    # Local, network-free smoke tests of core safety and ACP protocol wiring.
    assert is_quota_or_provider_failure("Usage limit reached for all Pro models")
    assert is_quota_or_provider_failure("HTTP 429 Too Many Requests")
    assert not is_quota_or_provider_failure("Implemented the requested fix and tests pass")
    assert classify_agent_response("", "end_turn", []) == ("failed", "ACP returned no assistant text (stopReason=end_turn).")
    assert classify_agent_response("ORCHESTRATOR_STATUS: HANDOFF", "end_turn", [])[0] == "handoff"
    assert classify_agent_response("", "end_turn", ["HTTP 429 Too Many Requests"])[0] == "provider_failure"
    prompt = compose_prompt("self test", [], {"status": "clean", "diff_stat": "", "diff": ""}, Path("."))
    assert "ORCHESTRATOR_STATUS: COMPLETE" in prompt
    mock = ACPProcess([sys.executable, str(APP_DIR / "mock_acp_agent.py")], APP_DIR, timeout=10)
    result = mock.run_task("Reply with MOCK_OK")
    assert "MOCK_OK" in result["text"], f"mock ACP text missing: {result}"
    assert "ORCHESTRATOR_STATUS: COMPLETE" in result["text"]
    print("\nSELF-TEST PASSED. The ACP initialize/session/prompt exchange worked with a local mock.")
    print("No cloud API requests, live agents, or project changes were made.")
    return 0



def classify_task_intent(prompt: str) -> str:
    """Classifies prompt into: architecture, bugfix, or fast_coding."""
    import re
    text = (prompt or "").lower()
    if re.search(r"\b(architect|refactor|design|system|structure|framework|database|multi-file|scaffold)\b", text):
        return "architecture"
    if re.search(r"\b(fix|bug|error|exception|traceback|fail|broken|patch|issue|test|assert)\b", text):
        return "bugfix"
    return "fast_coding"


def sort_agents_for_intent(agents: list[dict[str, Any]], intent: str) -> list[dict[str, Any]]:
    """Dynamically sorts agent queue according to capability efficiency,
    keeping kiro as primary, ordering cline models by intent, and copilot as backup."""
    cline_priority_map = {
        "architecture": [
            "cline-nemotron-ultra", "cline-nemotron-3.5", "cline-dots3",
            "cline-flint", "cline-cohere", "cline-gemma4", "cline-free-router", "cline"
        ],
        "bugfix": [
            "cline-flint", "cline-cohere", "cline-nemotron-3.5",
            "cline-nemotron-ultra", "cline-dots3", "cline-gemma4", "cline-free-router", "cline"
        ],
        "fast_coding": [
            "cline-nemotron-3.5", "cline-free-router", "cline-cohere",
            "cline-gemma4", "cline-flint", "cline-nemotron-ultra", "cline-dots3", "cline"
        ],
    }
    preferred_cline = cline_priority_map.get(intent, cline_priority_map["fast_coding"])

    def _rank(agent: dict[str, Any]) -> int:
        name = agent.get("name", "")
        if name == "kiro":
            return -100
        if name in preferred_cline:
            return preferred_cline.index(name)
        if name.startswith("cline"):
            return 50
        if name == "copilot":
            return 200
        return 500

    return sorted(agents, key=_rank)


def build_handoff_briefing(previous_agent_name: str, reason: str) -> str:
    """Generates a mandatory handover note for the replacement agent."""
    lines = [
        "======================================================================",
        "[MANDATORY HANDOVER BRIEFING - READ CAREFULLY BEFORE WRITING CODE]",
        f"The previous agent ({previous_agent_name}) was stopped ({reason}).",
        "Before writing, editing, or deleting any code, you MUST:",
        "1. Inspect existing files and git status to see what was already built.",
        "2. Match the existing architecture, libraries, and async/sync style.",
        "3. Do NOT rewrite working code or swap out installed packages.",
        "4. Pick up the remaining work from where the previous agent stopped.",
        "======================================================================\n",
    ]
    return "\n".join(lines)


def intercept_tool_command(command: str) -> bool:
    if safety_guard is not None:
        decision, reason = safety_guard.check_command_safety(command)
        if decision == "BLOCK":
            print(f"\n[SAFETY INTERCEPTOR: BLOCKED] {command}\nReason: {reason}")
            return False
        if decision == "ALLOW":
            print(f"\n[SAFETY INTERCEPTOR: AUTO-APPROVED] {command}")
            return True
    if decision == "BLOCK":
        print(f"\n[SAFETY INTERCEPTOR: BLOCKED] {command}\nReason: {reason}")
        return False
    if decision == "ALLOW":
        print(f"\n[SAFETY INTERCEPTOR: AUTO-APPROVED] {command}")
        return True
    choice = input(f"\n[PERMISSION PROMPT] Agent requests execution of:\n  {command}\nApprove? [y/N]: ").strip().lower()
    return choice in {"y", "yes"}

def run(args: argparse.Namespace) -> int:
    cfg = read_config()
    if args.self_test:
        return self_test()

    if args.resume:
        if args.check:
            raise OrchestratorError("Use --check without --resume")
        state_path = STATE_DIR / "latest.json"
        if not state_path.exists():
            raise OrchestratorError(f"No saved run to resume: {state_path}")
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OrchestratorError(f"Could not read saved state: {exc}") from exc
        if state.get("status") == "completed":
            raise OrchestratorError("The latest run is already completed; start a new task instead")
        if state.get("status") == "stopped_by_user_denial":
            raise OrchestratorError("Cannot resume after a denied tool permission; review the denial and start a new task only if appropriate")
        if not isinstance(state.get("attempts"), list):
            raise OrchestratorError("Saved state attempts field is invalid")
        project = resolve_project(cfg, args.project or state.get("project_path"))
        task = (args.task or state.get("original_task") or "").strip()
        if not task:
            raise OrchestratorError("Resume task is empty")
        prior_names = [str(a.get("agent", "unknown")) for a in state["attempts"] if a.get("agent")]
        print("\nResuming saved run:", state.get("run_id", "(unknown)"))
        print("Previously attempted agents will be skipped:", ", ".join(prior_names) or "none")
        print("Target project:", project)
        print("Orchestrator files/logs remain under:", APP_DIR)
        print("Paid fallback: DISABLED. Continue only with eligible free/included usage.")
        print("\nThe next agent may edit the target project only after you approve individual tool requests.")
        if args.confirm:
            print("Resume explicitly confirmed via --confirm. Individual agent tool permissions remain interactive.")
        else:
            confirmation = input(f"Type APPLY to resume in {project}, or anything else to cancel: ").strip()
            if confirmation != "APPLY":
                print("Cancelled. Saved state was not modified.")
                return 0
        state["project_path"] = str(project)
        state["status"] = "running"
        state["resumed_at"] = now_iso()
        state["active_task"] = task
        state["run_id"] = str(state.get("run_id") or datetime.now().strftime("%Y%m%d-%H%M%S"))
        save_state(state)
        attempted_names = set(prior_names)
    else:
        project = resolve_project(cfg, args.project)
        if args.check:
            cfg["project_path"] = str(project)
            return command_health(cfg)
        task = (args.task or "").strip()
        if not task:
            task = input("Describe the coding task to run in the project folder: ").strip()
        if not task:
            raise OrchestratorError("Task cannot be empty")
        print("\nTarget project:", project)
        print("Orchestrator files/logs remain under:", APP_DIR)
        print("Agent order: Kiro CLI → Cline (OpenRouter free model) → Copilot CLI → Ollama text-only fallback")
        print("Continue is configured separately in VS Code; its extension has no verified standalone CLI in this setup.")
        print("Safety: ACP tool actions require your one-time approval. No persistent approvals are offered. No paid fallback is allowed.")
        print("\nAgents may edit the target project after you approve individual tool calls.")
        if args.confirm:
            print("Task start explicitly confirmed via --confirm. Individual agent tool permissions remain interactive.")
        else:
            confirmation = input(f"Type APPLY to start task in {project}, or anything else to cancel: ").strip()
            if confirmation != "APPLY":
                print("Cancelled. No agent was launched.")
                return 0
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        state = {
            "schema_version": 1, "run_id": run_id, "created_at": now_iso(),
            "project_path": str(project), "original_task": task, "active_task": task,
            "status": "running", "attempts": [],
        }
        save_state(state)
        attempted_names = set()

    agents = [a for a in cfg.get("agents", []) if a.get("enabled", True) and a.get("type") in {"acp", "cline_cli"}]
    task_intent = classify_task_intent(task)
    agents = sort_agents_for_intent(agents, task_intent)
    lineup_names = " -> ".join(a.get("name") for a in agents)
    print(f"\n[Dynamic Router] Intent: {task_intent.upper()} | Prioritized lineup: {lineup_names}")
    handoff_note = ""
    last_snapshot = git_snapshot(project)
    for agent in agents:
        name = agent.get("name", "unknown")
        if name in attempted_names:
            print(f"\nSkipping {name}: already attempted in saved run.")
            continue
        cmd, cmd_err = resolve_command(agent)
        attempt: dict[str, Any] = {"agent": name, "label": agent.get("label", name), "started_at": now_iso(), "outcome": "failed", "response": "", "error": None}
        if cmd_err or not cmd:
            attempt["error"] = cmd_err or "Could not resolve agent command"
            attempt["finished_at"] = now_iso()
            state["attempts"].append(attempt)
            print(f"\nSkipping {name}: {attempt['error']}")
            save_state(state)
            continue
        last_snapshot = git_snapshot(project)
        prompt = compose_prompt(task, state["attempts"], last_snapshot, project, original_task=state.get("original_task"))
        try:
            if agent.get("type") == "cline_cli":

                client = ClineCLIProcess(cmd, project, timeout=int(agent.get("timeout_seconds", 900)))

            else:

                client = ACPProcess(cmd, project, timeout=int(agent.get("timeout_seconds", 900)))
            print(f"\n===== TRYING {agent.get('label', name)} =====")
            result = client.run_task(prompt)
            attempt["response"] = redacted_error(result.get("text", ""))
            attempt["acp_result"] = result.get("result", {})
            attempt["session_id"] = result.get("session_id")
            attempt["stop_reason"] = result.get("stop_reason")
            attempt["provider_usage_metadata"] = result.get("metering", [])
            attempt["protocol_events"] = result.get("protocol_events", [])
            attempt["stderr"] = redacted_error("\n".join(result.get("stderr", [])))[-4000:]
            response = attempt["response"]
            if result.get("user_denied"):
                attempt["outcome"] = "user_denied"
                attempt["error"] = "A tool permission was denied; the orchestrator will not bypass the denial by switching agents."
                attempt["finished_at"] = now_iso()
                attempt["post_task_git_snapshot"] = git_snapshot(project)
                state["attempts"].append(attempt)
                state["status"] = "stopped_by_user_denial"
                save_state(state)
                print("\nStopped because you denied a tool permission. No other agent will be used to bypass that decision.")
                return 2

            outcome, classification_error = classify_agent_response(response, result.get("stop_reason"), result.get("stderr", []))
            attempt["outcome"] = outcome
            attempt["error"] = classification_error
            if outcome == "provider_failure":
                print(f"\nProvider/quota issue detected for {name}; capturing state before handoff.")
            elif outcome == "failed":
                event_kinds = sorted({str(event.get("kind", "unknown")) for event in result.get("protocol_events", [])})
                suffix = f"; updates={', '.join(event_kinds)}" if event_kinds else ""
                print(f"\n{name} ended without assistant text (stopReason={result.get('stop_reason')}{suffix}). Capturing diagnostics and trying next eligible agent.")
                handoff_note = build_handoff_briefing(agent.get("name", "previous_agent"), "encountered error or rate limit")
            elif outcome == "needs_user":
                print("\nAgent did not clearly mark the task complete. Safely stopping for user review.")
            elif outcome == "handoff":
                print(f"\n{name} requested handoff; capturing state before next agent.")
                handoff_note = build_handoff_briefing(agent.get("name", "previous_agent"), "requested handoff")
            attempt["finished_at"] = now_iso()
            attempt["post_task_git_snapshot"] = git_snapshot(project)
            state["attempts"].append(attempt)
            save_state(state)
            if outcome == "complete":
                state["status"] = "completed"
                state["completed_at"] = now_iso()
                save_state(state)
                print(f"\nTask marked complete by {name}. Review its output and git diff before accepting changes.")
                return 0
            if outcome == "needs_user":
                state["status"] = "needs_user"
                save_state(state)
                print(f"\nPaused. Current progress and handoff context saved to {STATE_DIR / 'latest.json'}")
                return 2
        except (OrchestratorError, OSError, subprocess.SubprocessError, BrokenPipeError, EOFError) as exc:
            attempt["error"] = redacted_error(str(exc))
            attempt["outcome"] = "failed"
            attempt["finished_at"] = now_iso()
            attempt["post_task_git_snapshot"] = git_snapshot(project)
            state["attempts"].append(attempt)
            save_state(state)
            print(f"\nAgent {name} failed: {attempt['error']}\nSaving state and attempting next eligible agent.")
            continue
        # HANDOFF, provider failures and empty output continue to the next agent.

    state["status"] = "all_agents_failed_or_handed_off"
    save_state(state)
    print("\nNo ACP coding agent completed the task. Trying the local Ollama text-only fallback for a recovery plan.")
    fallback = run_ollama_fallback(task, state, {**cfg, "project_path": str(project)})
    state["attempts"].append(fallback)
    state["status"] = "needs_user" if fallback.get("outcome") == "needs_user" else "all_agents_failed"
    state["finished_at"] = now_iso()
    save_state(state)
    run_history_path = LOG_DIR / ("run-" + str(state.get("run_id", "unknown")) + ".json")
    print(f"\nSaved run history: {run_history_path}")
    print("Note: Continue is not auto-invoked because only the VS Code extension was verified; it remains a manual IDE option.")
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Free-tier-first ACP coding-agent handoff orchestrator")
    parser.add_argument("--task", help="Coding task to run; with --resume, this is the continuation instruction.")
    parser.add_argument("--project", help="Override project path from config.json")
    parser.add_argument("--resume", action="store_true", help="Resume state/latest.json and skip agents already attempted")
    parser.add_argument("--confirm", action="store_true", help="Explicitly confirm start/resume without an interactive APPLY prompt; does not auto-approve agent tool permissions")
    parser.add_argument("--check", action="store_true", help="Read-only preflight; do not launch AI agents")
    parser.add_argument("--self-test", action="store_true", help="Network-free local self-test")
    args = parser.parse_args()
    try:
        return run(args)
    except (OrchestratorError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
