# AI Agent Orchestrator (Windows, v1 scaffold)

This is a local, free-tier-first task handoff orchestrator. It launches ACP-capable agents one at a time, asks before each tool action, saves progress/handoff context, detects common provider/quota failures, and stops for user review rather than claiming success when an agent's status is unclear.

## Important limitations (read first)

- **Amazon Q VS Code extension itself is not directly controlled by this orchestrator.** Kiro CLI is used as the first *controllable* agent because it is the successor/related CLI interface. The existing Amazon Q extension stays installed and unchanged.
- **Continue is manual-only in v1.** The VS Code extension is present, but this installation has no verified standalone agent CLI for automatic orchestration.
- **Ollama is text-only as the final fallback in v1.** It creates a recovery plan, not automated file edits.
- No software can guarantee zero monetary spend from provider behavior alone. This project forbids paid fallback/overages in its own routing, uses the explicit OpenRouter free model with Cline, and requires you to check your included Kiro/Copilot allowances. If a provider requests billing/upgrading, cancel and don't approve it.
- ACP tool permissions are never auto-approved. Approve one-time operations only after reading them. The program will not offer persistent/"always allow" approvals.
- Agents can modify the target project after you approve their tool requests. Review the diff before accepting changes.

## Paths

- Orchestrator code, config, state, and logs live in this folder.
- Default coding workspace is `C:\CloudLab\mybot`.
- `state\latest.json` and `logs\run-<id>.json` save task/handoff history. Avoid adding secrets to task prompts because summaries are logged locally.

## Prerequisites

- Python 3.10+
- Kiro CLI logged in with the free allowance
- Cline CLI, logged in/configured to use `nvidia/nemotron-3.5-lightning:free` via OpenRouter
- GitHub Copilot CLI authenticated with included plan allowance only
- Ollama running locally, with `qwen2.5-coder:3b` pulled

## First: network-free self-test

From this folder:

```powershell
python .\orchestrator.py --self-test
```

Expected: `SELF-TEST PASSED.` No AI requests or project changes.

## Read-only preflight

```powershell
python .\orchestrator.py --check
```

This checks command availability and local Ollama. It does not send prompts. Fix missing commands before running tasks.

## Run a task

Use the explicit project path if needed:

```powershell
python .\orchestrator.py --project 'C:\CloudLab\mybot' --task 'Describe the coding task here'
```

The program asks you to type `APPLY` to start. Each agent receives the current task, project status/diff, and previous agents' handoff notes. Tool permission prompts are interactive. Press Enter/deny at permission prompts to reject an operation. If a user denies a tool, stop the task rather than trying to bypass the denial.

The orchestrator tries Kiro CLI ACP v3, then Cline ACP pinned to the free OpenRouter model, then GitHub Copilot CLI ACP. If no ACP agent completes the task, Ollama returns a local text-only recovery plan. Continue is documented as manual-only.

## Recovery

Before a second attempt, inspect:

```powershell
Get-Content .\state\latest.json
```

Use the logs to understand earlier changes; inspect your project's `git diff` before deciding to continue. No changes are automatically reverted, because an agent may have made useful partial progress.
