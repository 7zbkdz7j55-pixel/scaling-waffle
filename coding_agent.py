"""
coding_agent.py — an autonomous coding sub-agent, built on the Claude Agent SDK.

Runs standalone, and is also the delegate behind forge.py's `code_task` tool. It
gets a task in plain English, then reads, writes and edits files, runs commands
and tests, and debugs on its own inside one project directory.

The Claude Agent SDK is Claude Code packaged as a library: it supplies the agent
loop and the built-in tools (Read/Write/Edit/Bash/Glob/Grep/...). This module's
job is small and deliberate — decide *where* it runs, *which* tools it may use,
and *what it remembers between runs*.

Session persistence
-------------------
The SDK reports a session id on its init message. We store it in
`<project>/.claude_agent_session` and pass it back as `resume=` next time, so a
follow-up task continues the same conversation instead of rediscovering the
codebase. Resume restores the *conversation*, not your files — run the project
folder as a git repo and commit between tasks.

Setup
-----
    pip install claude-agent-sdk
    npm install -g @anthropic-ai/claude-code
    export ANTHROPIC_API_KEY=sk-ant-...

Usage
-----
    python coding_agent.py "add a --verbose flag to cli.py and a test for it"
    python coding_agent.py --new "start over: scaffold a FastAPI app"
    python coding_agent.py --dir ./somewhere "fix the failing test"
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    query,
)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
HERE = Path(__file__).parent.resolve()

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
MAX_TURNS = int(os.environ.get("CODE_MAX_TURNS", "60"))

# Where the resume token lives, relative to the project directory.
SESSION_FILE = ".claude_agent_session"

# The sub-agent runs unattended: there is no human to answer a permission
# prompt, so anything not pre-approved here is simply refused mid-task. Keeping
# this an explicit allowlist rather than bypassPermissions means the tool
# surface is auditable, and the blast radius stays inside `cwd`.
DEFAULT_ALLOWED_TOOLS = [
    "Read", "Write", "Edit", "Glob", "Grep", "Bash", "TodoWrite", "NotebookEdit",
]
ALLOWED_TOOLS = [
    t for t in os.environ.get("CODE_ALLOWED_TOOLS", ",".join(DEFAULT_ALLOWED_TOOLS)).split(",")
    if t.strip()
]

# acceptEdits lets it write without prompting; the allowlist above is what
# actually bounds it. Override with CODE_PERMISSION_MODE if you know why.
PERMISSION_MODE = os.environ.get("CODE_PERMISSION_MODE", "acceptEdits")

SYSTEM_PROMPT = (
    "You are a focused coding agent working inside one project directory. "
    "Read before you write: look at the existing code and match its conventions, "
    "naming and structure rather than importing your own style. Make the smallest "
    "change that fully does the job. When you change behaviour, run the project's "
    "own tests or a quick check to prove it works, and say what you ran. If the "
    "task is ambiguous, choose the most conventional interpretation and state the "
    "assumption in your final message instead of stopping to ask — nobody is "
    "watching this run. Finish with a short summary of what changed and why."
)


# --------------------------------------------------------------------------- #
# Session persistence
# --------------------------------------------------------------------------- #
def _session_path(project_dir: Path | str) -> Path:
    return Path(project_dir) / SESSION_FILE


def _load_session(project_dir: Path | str) -> str | None:
    """Return the saved session id for this project, or None to start fresh."""
    path = _session_path(project_dir)
    try:
        session_id = path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as e:
        print(f"[warn] could not read {path}: {e}")
        return None
    return session_id or None


def _save_session(project_dir: Path | str, session_id: str) -> None:
    """Persist the session id so the next run can resume this conversation."""
    if not session_id:
        return
    path = _session_path(project_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(session_id, encoding="utf-8")
    except OSError as e:
        # A lost resume token costs context on the next run, nothing more.
        print(f"[warn] could not save session to {path}: {e}")


def clear_session(project_dir: Path | str) -> bool:
    """Forget the saved conversation. Returns True if a session was removed."""
    path = _session_path(project_dir)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        print(f"[warn] could not clear {path}: {e}")
        return False


# Public aliases. forge.py imports the underscored names; these read better
# anywhere else.
load_session = _load_session
save_session = _save_session


# --------------------------------------------------------------------------- #
# Options
# --------------------------------------------------------------------------- #
def build_options(
    project_dir: Path | str,
    resume: str | None = None,
    model: str | None = None,
    max_turns: int | None = None,
) -> ClaudeAgentOptions:
    """Build the sub-agent's options, rooted at project_dir.

    `resume` is a session id from a previous run (see _load_session). Passing an
    id for a session the CLI no longer has on disk starts a new conversation
    rather than failing, so a stale token is not worth guarding against.
    """
    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)

    return ClaudeAgentOptions(
        cwd=str(project_dir),
        resume=resume,
        model=model or MODEL,
        system_prompt=SYSTEM_PROMPT,
        allowed_tools=list(ALLOWED_TOOLS),
        permission_mode=PERMISSION_MODE,
        max_turns=max_turns if max_turns is not None else MAX_TURNS,
        # Respect a CLAUDE.md sitting in the project itself, but not the
        # machine's user-level settings — the sub-agent should behave the same
        # wherever this repo is checked out.
        setting_sources=["project"],
    )


# --------------------------------------------------------------------------- #
# Running a task
# --------------------------------------------------------------------------- #
def _describe_tool_use(block) -> str:
    """One line for a tool call, favouring whichever field says the most."""
    inp = getattr(block, "input", {}) or {}
    detail = (inp.get("command") or inp.get("file_path") or inp.get("pattern")
              or inp.get("path") or inp.get("description") or "")
    return f"→ {block.name}: {str(detail)[:100]}"


async def run_task(
    task: str,
    project_dir: Path | str,
    fresh: bool = False,
    quiet: bool = False,
) -> str:
    """Run one task to completion and return a transcript of what it did."""
    project_dir = Path(project_dir)
    resume = None if fresh else _load_session(project_dir)
    options = build_options(project_dir, resume=resume)

    if not quiet:
        where = "fresh session" if resume is None else f"resuming {resume[:8]}…"
        print(f"[code-agent] {where} in {project_dir}")

    transcript: list[str] = []
    session_id: str | None = None

    async for message in query(prompt=task, options=options):
        if isinstance(message, SystemMessage):
            if message.subtype == "init":
                session_id = (message.data or {}).get("session_id")
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                text = getattr(block, "text", None)
                if text and text.strip():
                    line = text.strip()
                    if not quiet:
                        print(f"  {line[:200]}")
                    transcript.append(line)
                elif hasattr(block, "name"):
                    line = _describe_tool_use(block)
                    if not quiet:
                        print(f"  {line}")
                    transcript.append(line)
        elif isinstance(message, ResultMessage):
            # ResultMessage carries the authoritative session id; prefer it and
            # fall back to the one announced at init.
            session_id = message.session_id or session_id
            if message.is_error:
                transcript.append(f"[sub-agent reported an error: "
                                  f"{message.subtype}]")
            cost = message.total_cost_usd
            if isinstance(cost, (int, float)):
                transcript.append(f"[sub-agent cost ${cost:.4f}]")

    if session_id:
        _save_session(project_dir, session_id)

    return "\n".join(transcript) or "(sub-agent produced no output)"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Delegate a coding task to an autonomous sub-agent."
    )
    parser.add_argument("task", nargs="*", help="What you want done")
    parser.add_argument("--dir", default=str(HERE / "workspace" / "project"),
                        help="Project directory the agent works in")
    parser.add_argument("--new", action="store_true",
                        help="Ignore the saved session and start a fresh conversation")
    parser.add_argument("--forget", action="store_true",
                        help="Delete the saved session and exit")
    args = parser.parse_args()

    project_dir = Path(args.dir).expanduser().resolve()

    if args.forget:
        removed = clear_session(project_dir)
        print("Session cleared." if removed else "No saved session to clear.")
        return

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: set ANTHROPIC_API_KEY first.")
        sys.exit(1)

    task = " ".join(args.task).strip()
    if not task:
        parser.error("give it a task, e.g. coding_agent.py \"add a test for foo\"")

    result = asyncio.run(run_task(task, project_dir, fresh=args.new))
    print("\n" + "=" * 60 + "\nDONE\n" + "=" * 60 + f"\n{result}")


if __name__ == "__main__":
    main()
