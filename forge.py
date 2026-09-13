"""
forge.py — one agent, four builds, one shared workspace.

Runs your existing toolkit together instead of separately:

  browser_agent.py           -> in-process tools (Playwright)
  rag.py                     -> in-process tools (local embeddings)
  coding_agent.py            -> delegated sub-agent (Claude Agent SDK)
  data_analytics_server.py   -> a real MCP server, spawned over stdio

The analytics build stays an MCP server. It is NOT imported. forge.py acts as an
MCP *host*: it launches the server as a subprocess, discovers its tools at
runtime, and merges them into the same tool list the browser and RAG tools live
in. That means the server keeps working unchanged in Claude Desktop, and you can
add any other MCP server by adding one entry to MCP_SERVERS.

The four builds support each other through a shared workspace:

    workspace/data/     analytics reads this (DuckDB queries the files directly)
    workspace/docs/     RAG ingests this
    workspace/project/  the coding sub-agent works here
    workspace/notes/    scratch output

So the agent can scrape a page with the browser, save the table into
workspace/data/, immediately query it with SQL, chart it, and then have the
coding sub-agent write software against the result — in one continuous task.

Setup
-----
    pip install anthropic playwright "mcp>=2.0" claude-agent-sdk \
                duckdb pandas httpx matplotlib sentence-transformers numpy
    playwright install chromium
    npm install -g @anthropic-ai/claude-code     # for the coding sub-agent
    export ANTHROPIC_API_KEY=sk-ant-...

Put forge.py in the same folder as browser_agent.py, rag.py, coding_agent.py and
data_analytics_server.py. Any build whose dependencies are missing is skipped
with a warning — the rest still run.

Usage
-----
    python forge.py "Scrape the top 10 HN stories, save them as a CSV in the data
                     folder, then chart score by story."
    python forge.py --chat                       # multi-turn, one live session
    HEADLESS=1 python forge.py "..."             # no visible browser window
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import sys
import traceback
from contextlib import AsyncExitStack
from pathlib import Path

from anthropic import AsyncAnthropic

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
HERE = Path(__file__).parent.resolve()

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
MAX_STEPS = int(os.environ.get("MAX_STEPS", "40"))
HEADLESS = os.environ.get("HEADLESS", "").lower() in ("1", "true", "yes")

WORKSPACE = Path(os.environ.get("WORKSPACE", HERE / "workspace")).expanduser().resolve()
DATA_DIR = WORKSPACE / "data"
DOCS_DIR = WORKSPACE / "docs"
PROJECT_DIR = WORKSPACE / "project"
NOTES_DIR = WORKSPACE / "notes"
KB_DIR = WORKSPACE / "kb_index"

for _d in (DATA_DIR, DOCS_DIR, PROJECT_DIR, NOTES_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Caps so one fat tool result can't blow the context window.
MAX_TOOL_CHARS = int(os.environ.get("MAX_TOOL_CHARS", "8000"))
MAX_SUBAGENT_CHARS = 6000


def _clip(text: str, limit: int = MAX_TOOL_CHARS) -> str:
    text = text if isinstance(text, str) else str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def _resolve(*candidates: str) -> Path | None:
    """Find the first candidate filename that exists next to forge.py."""
    for name in candidates:
        p = HERE / name
        if p.exists():
            return p
    return None


# MCP servers this app hosts. Add more here — third-party servers work too.
ANALYTICS_SCRIPT = _resolve("data_analytics_server.py", "data_analytics_server_2.py")

MCP_SERVERS: dict[str, dict] = {}
if ANALYTICS_SCRIPT:
    MCP_SERVERS["analytics"] = {
        "command": sys.executable,
        "args": [str(ANALYTICS_SCRIPT)],
        # DATA_DIR points the server at the shared workspace, so anything the
        # browser saves there is queryable without restarting anything.
        "env": {**os.environ, "DATA_DIR": str(DATA_DIR)},
    }


# --------------------------------------------------------------------------- #
# MCP bridge — host side
# --------------------------------------------------------------------------- #
def _attr(obj, *names, default=None):
    """Read the first attribute that exists.

    mcp 1.x used camelCase on its models (inputSchema, isError, mimeType); mcp 2.x
    renamed them to snake_case. Reading both keeps this working on either.
    """
    for n in names:
        val = getattr(obj, n, None)
        if val is not None:
            return val
    return default


class MCPBridge:
    """Spawns MCP servers over stdio, discovers their tools, and calls them.

    Tool names are namespaced as <server>__<tool> so two servers can both expose
    a tool called `search` without colliding.
    """

    def __init__(self, stack: AsyncExitStack):
        self._stack = stack
        self.sessions: dict[str, object] = {}   # server name -> ClientSession
        self.tools: list[dict] = []             # Anthropic-shaped tool schemas
        self._routes: dict[str, tuple[str, str]] = {}  # exposed name -> (server, tool)

    async def connect_all(self, servers: dict[str, dict]):
        if not servers:
            return
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError:
            print("[warn] 'mcp' package not installed — MCP servers skipped "
                  "(pip install mcp)")
            return

        for name, cfg in servers.items():
            # Each server gets its own exit stack. If it fails to start, we close
            # only that stack — otherwise a half-entered context poisons the shared
            # one and the whole app explodes on shutdown instead of degrading.
            server_stack = AsyncExitStack()
            try:
                params = StdioServerParameters(
                    command=cfg["command"],
                    args=cfg.get("args", []),
                    env=cfg.get("env"),
                )
                read, write = await server_stack.enter_async_context(stdio_client(params))
                session = await server_stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self.sessions[name] = session

                listing = await session.list_tools()
                for tool in listing.tools:
                    exposed = f"{name}__{tool.name}"
                    schema = (_attr(tool, "input_schema", "inputSchema")
                              or {"type": "object", "properties": {}})
                    self.tools.append({
                        "name": exposed,
                        "description": (tool.description or "").strip(),
                        "input_schema": schema,
                    })
                    self._routes[exposed] = (name, tool.name)
                self._stack.push_async_callback(self._close_quietly, server_stack, name)
                print(f"[mcp] {name}: {len(listing.tools)} tools "
                      f"({', '.join(t.name for t in listing.tools)})")
            except Exception as e:
                await self._close_quietly(server_stack, name)
                self.sessions.pop(name, None)
                print(f"[warn] MCP server '{name}' failed to start "
                      f"({type(e).__name__}: {e}). Its stderr is above. Continuing "
                      "without it.")

    @staticmethod
    async def _close_quietly(stack: AsyncExitStack, name: str):
        """Shutting down a stdio server can raise teardown noise from the transport's
        task group. That must never mask the real result of the run."""
        try:
            await stack.aclose()
        except Exception as e:
            print(f"[debug] '{name}' shutdown: {type(e).__name__}")

    def handles(self, exposed_name: str) -> bool:
        return exposed_name in self._routes

    async def call(self, exposed_name: str, args: dict):
        """Call an MCP tool and convert its result into Anthropic tool_result content.

        Returns either a string, or a list of content blocks when the server
        returned an image (e.g. make_chart) so the model can actually see it.
        """
        server, tool = self._routes[exposed_name]
        session = self.sessions[server]
        result = await session.call_tool(tool, arguments=args or {})

        blocks, texts = [], []
        for item in _attr(result, "content", default=[]) or []:
            kind = getattr(item, "type", None)
            if kind == "text":
                texts.append(item.text)
            elif kind == "image":
                blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": _attr(item, "mime_type", "mimeType",
                                            default="image/png"),
                        "data": item.data,
                    },
                })
            else:
                texts.append(str(item))

        if _attr(result, "is_error", "isError", default=False):
            return _clip("Tool error: " + ("\n".join(texts) or "unknown error"))
        if blocks:
            if texts:
                blocks.insert(0, {"type": "text", "text": _clip("\n".join(texts), 1500)})
            return blocks
        return _clip("\n".join(texts)) or "(no output)"


# --------------------------------------------------------------------------- #
# Local tools — browser, RAG, coding sub-agent, workspace
# --------------------------------------------------------------------------- #
WORKSPACE_TOOLS = [
    {
        "name": "save_artifact",
        "description": (
            "Save text to the shared workspace. This is how the builds feed each other. "
            "folder='data' makes it immediately queryable by the analytics SQL tools "
            "(use .csv/.json/.parquet). folder='docs' makes it ingestable by the RAG "
            "tools (.md/.txt). folder='notes' is scratch. Returns the full path."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "folder": {"type": "string", "enum": ["data", "docs", "notes"]},
                "filename": {"type": "string", "description": "e.g. hn_stories.csv"},
                "content": {"type": "string"},
            },
            "required": ["folder", "filename", "content"],
        },
    },
    {
        "name": "list_workspace",
        "description": "List what is currently in the shared workspace folders.",
        "input_schema": {"type": "object", "properties": {}},
    },
]

RAG_TOOLS = [
    {
        "name": "rag_ingest",
        "description": (
            "Index documents into the local knowledge base so they can be searched "
            "semantically. Pass a file or folder path; defaults to the workspace docs "
            "folder. Embeddings are computed locally — nothing leaves the machine."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
        },
    },
    {
        "name": "rag_search",
        "description": (
            "Semantic search over indexed documents. Returns the most relevant passages "
            "with their source file and similarity score. Use this before answering "
            "questions about the user's own documents."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "description": "How many passages (default 5)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "rag_list",
        "description": "List the documents currently in the knowledge base.",
        "input_schema": {"type": "object", "properties": {}},
    },
]

CODE_TOOLS = [
    {
        "name": "code_task",
        "description": (
            "Delegate a software task to an autonomous coding sub-agent working in the "
            "workspace project folder. It reads, writes and edits files, runs commands "
            "and tests, and debugs on its own, then reports back. Give it one clear, "
            "self-contained task. It is slow and costs tokens — use it for real code "
            "changes, not for reading a file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "new_session": {
                    "type": "boolean",
                    "description": "Ignore the saved session and start fresh (default false)",
                },
            },
            "required": ["task"],
        },
    },
]

# Dispatch falls through to the browser last, so it needs to know what belongs to
# it. Without this an unrecognised name — a hallucinated tool, or a browser tool
# when the browser build was skipped — would boot Chromium purely to report failure.
BROWSER_TOOL_NAMES = frozenset({
    "navigate", "get_page_content", "click", "type_text", "fill_form", "screenshot",
})

FINISH_TOOL = {
    "name": "finish",
    "description": "Call when the task is complete. Return the final answer or result.",
    "input_schema": {
        "type": "object",
        "properties": {"result": {"type": "string"}},
        "required": ["result"],
    },
}


class LocalTools:
    """Lazily starts each subsystem, so a task that never browses never launches
    Chromium and a task that never searches never loads an embedding model."""

    def __init__(self, stack: AsyncExitStack):
        self._stack = stack
        self._browser_ctrl = None
        self._kb = None
        self.browser_tools: list[dict] = []
        self.rag_tools: list[dict] = []
        self.code_tools: list[dict] = []

    def probe(self):
        """Decide which toolsets to advertise, based on what's importable."""
        try:
            import browser_agent  # noqa: F401
            from playwright.async_api import async_playwright  # noqa: F401
            # Reuse the original tool schemas; finish is handled by the orchestrator.
            self.browser_tools = [t for t in browser_agent.TOOLS if t["name"] != "finish"]
        except Exception as e:
            print(f"[warn] browser tools unavailable: {type(e).__name__}: {e}")

        try:
            import rag  # noqa: F401
            self.rag_tools = RAG_TOOLS
        except Exception as e:
            print(f"[warn] RAG tools unavailable ({type(e).__name__}: {e}) — "
                  "is rag.py next to forge.py?")

        try:
            import claude_agent_sdk  # noqa: F401
            import coding_agent  # noqa: F401
            self.code_tools = CODE_TOOLS
        except Exception as e:
            print(f"[warn] coding sub-agent unavailable: {type(e).__name__}: {e}")

    # -- browser -------------------------------------------------------------
    async def _browser(self):
        if self._browser_ctrl is None:
            from playwright.async_api import async_playwright
            from browser_agent import BrowserController
            pw = await self._stack.enter_async_context(async_playwright())
            browser = await pw.chromium.launch(headless=HEADLESS)
            self._stack.push_async_callback(browser.close)
            page = await browser.new_page()
            self._browser_ctrl = BrowserController(page)
            print("[browser] chromium started"
                  f"{' (headless)' if HEADLESS else ''}")
        return self._browser_ctrl

    # -- rag -----------------------------------------------------------------
    def _knowledge_base(self):
        if self._kb is None:
            from rag import KnowledgeBase
            print("[rag] loading local embedding model (first run downloads it)…")
            self._kb = KnowledgeBase(index_dir=KB_DIR)
        return self._kb

    # -- dispatch ------------------------------------------------------------
    async def call(self, name: str, args: dict):
        # workspace
        if name == "save_artifact":
            folder = {"data": DATA_DIR, "docs": DOCS_DIR, "notes": NOTES_DIR}[args["folder"]]
            target = folder / Path(args["filename"]).name  # no path escapes
            target.write_text(args["content"], encoding="utf-8")
            extra = ""
            if args["folder"] == "data":
                extra = (f" It is now queryable as table '{target.stem}' — "
                         "call analytics__list_sources to confirm.")
            elif args["folder"] == "docs":
                extra = " Call rag_ingest to index it."
            return f"Saved {target} ({len(args['content'])} chars).{extra}"

        if name == "list_workspace":
            lines = []
            for label, d in (("data", DATA_DIR), ("docs", DOCS_DIR),
                             ("project", PROJECT_DIR), ("notes", NOTES_DIR)):
                files = sorted(p.name for p in d.iterdir() if p.is_file())
                lines.append(f"{label}/ ({d}): " + (", ".join(files) if files else "(empty)"))
            return "\n".join(lines)

        # rag
        if name == "rag_ingest":
            kb = self._knowledge_base()
            path = args.get("path") or str(DOCS_DIR)
            return str(kb.ingest_path(path))

        if name == "rag_search":
            kb = self._knowledge_base()
            hits = kb.search(args["query"], k=int(args.get("k", 5)))
            if not hits:
                return "No results — the knowledge base may be empty. Try rag_ingest first."
            out = []
            for i, h in enumerate(hits, 1):
                out.append(f"[{i}] {Path(h['source']).name} "
                           f"(chunk {h['chunk_index']}, score {h['score']:.3f})\n{h['text']}")
            return _clip("\n\n".join(out))

        if name == "rag_list":
            kb = self._knowledge_base()
            srcs = kb.list_sources()
            if not srcs:
                return "No documents indexed yet."
            return "\n".join(f"{n:4d} chunks  {Path(s).name}" for s, n in srcs)

        # coding sub-agent
        if name == "code_task":
            return await self._run_code_task(args["task"], bool(args.get("new_session")))

        # browser
        if name not in BROWSER_TOOL_NAMES:
            return f"Unknown tool: {name}"
        if not self.browser_tools:
            return (f"The browser build did not load, so '{name}' is unavailable. "
                    "See the startup warnings; browser_agent.py and playwright are "
                    "required.")
        ctrl = await self._browser()
        if name == "navigate":
            return await ctrl.navigate(args["url"])
        if name == "get_page_content":
            return _clip(await ctrl.get_page_content())
        if name == "click":
            return await ctrl.click(args["index"])
        if name == "type_text":
            return await ctrl.type_text(args["index"], args["text"],
                                        args.get("submit", False))
        if name == "fill_form":
            return await ctrl.fill_form(args["fields"])
        if name == "screenshot":
            png = await ctrl.screenshot()
            return [{
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.standard_b64encode(png).decode(),
                },
            }]

        return f"Unknown tool: {name}"

    async def _run_code_task(self, task: str, fresh: bool) -> str:
        from claude_agent_sdk import query, AssistantMessage, SystemMessage, ResultMessage
        from coding_agent import build_options, _load_session, _save_session

        resume = None if fresh else _load_session(PROJECT_DIR)
        options = build_options(PROJECT_DIR, resume=resume)

        print(f"\n[code-agent] {task}")
        transcript, session_id = [], None
        async for message in query(prompt=task, options=options):
            if isinstance(message, SystemMessage):
                if getattr(message, "subtype", None) == "init":
                    session_id = (message.data or {}).get("session_id")
            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if hasattr(block, "text") and block.text.strip():
                        print(f"  {block.text.strip()[:200]}")
                        transcript.append(block.text.strip())
                    elif hasattr(block, "name"):
                        inp = getattr(block, "input", {}) or {}
                        detail = (inp.get("command") or inp.get("file_path")
                                  or inp.get("pattern") or "")
                        line = f"→ {block.name}: {str(detail)[:100]}"
                        print(f"  {line}")
                        transcript.append(line)
            elif isinstance(message, ResultMessage):
                cost = getattr(message, "total_cost_usd", None)
                if isinstance(cost, (int, float)):
                    transcript.append(f"[sub-agent cost ${cost:.4f}]")
        if session_id:
            _save_session(PROJECT_DIR, session_id)
        return _clip("\n".join(transcript) or "(sub-agent produced no output)",
                     MAX_SUBAGENT_CHARS)


# --------------------------------------------------------------------------- #
# System prompt
# --------------------------------------------------------------------------- #
def build_system_prompt(has_browser, has_rag, has_code, has_analytics) -> str:
    """Describe only the builds that actually loaded.

    Every line here is gated on a capability flag. A build whose dependencies were
    missing is skipped at startup, and naming its tools anyway is not a harmless
    inaccuracy — it is an invitation to call something that does not exist, which
    costs a round trip and an error the model then has to recover from.
    """
    live = sum((has_browser, has_analytics, has_rag, has_code))
    if live > 1:
        count = {2: "two", 3: "three", 4: "four"}[live]
        parts = [
            f"You are an orchestrator with {count} capabilities that are meant to be "
            "chained together, not used in isolation. You work out of one shared "
            "workspace:",
        ]
    else:
        parts = ["You are an orchestrator working out of one shared workspace:"]

    parts.append(f"  data folder:    {DATA_DIR}"
                 + ("   (SQL-queryable)" if has_analytics else ""))
    parts.append(f"  docs folder:    {DOCS_DIR}"
                 + ("   (semantically searchable)" if has_rag else ""))
    if has_code:
        parts.append(f"  project folder: {PROJECT_DIR}   "
                     "(the coding sub-agent's working directory)")
    parts += ["", "Available:"]

    if has_browser:
        parts.append("- BROWSER: navigate, get_page_content, click, type_text, fill_form, "
                     "screenshot. Call get_page_content before acting; element indices go "
                     "stale after every click or navigation.")
    if has_analytics:
        parts.append("- ANALYTICS (analytics__*): list_sources, describe_source, run_sql, "
                     "load_api, make_chart. DuckDB SQL over files in the data folder. "
                     "Read-only.")
    if has_rag:
        parts.append("- KNOWLEDGE BASE: rag_ingest, rag_search, rag_list. Local semantic "
                     "search over the user's documents.")
    if has_code:
        parts.append("- CODING SUB-AGENT: code_task. Delegates real software work to an "
                     "autonomous agent in the project folder.")
    parts.append("- WORKSPACE: save_artifact, list_workspace.")

    chaining = []
    if has_analytics:
        source = "Web data you want to analyze: extract it, save_artifact" if has_browser \
            else "Data you want to analyze: save_artifact"
        chaining.append(
            f"- {source} to 'data' as CSV, then analytics__list_sources and "
            "analytics__run_sql. Do not try to do arithmetic in your head over a "
            "scraped table — put it in SQL.")
    if has_rag:
        origin = "Web or local text" if has_browser else "Local text"
        chaining.append(f"- {origin} you want to reason over later: save_artifact to "
                        "'docs', then rag_ingest, then rag_search.")
    grounded = [n for n, on in (("rag_search", has_rag),
                                ("analytics__run_sql", has_analytics)) if on]
    if grounded:
        chaining.append(
            "- Before answering from your own knowledge about the user's data or "
            f"documents, check {' or '.join(grounded)} first. Grounded beats plausible.")
    if has_code:
        chaining.append("- Build or change software: hand code_task one clear "
                        "self-contained task.")
    if chaining:
        parts += ["", "How to chain them:"] + chaining

    parts += [
        "",
        "Be efficient. Prefer one good SQL query to five small ones. If a tool errors, "
        "read the error and adapt rather than repeating the same call. When the task is "
        "done, call finish with the result.",
    ]
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Agent loop
# --------------------------------------------------------------------------- #
class Forge:
    def __init__(self, stack: AsyncExitStack):
        self.client = AsyncAnthropic()
        self.local = LocalTools(stack)
        self.mcp = MCPBridge(stack)
        self.tools: list[dict] = []
        self.system = ""
        self.messages: list[dict] = []

    async def start(self):
        self.local.probe()
        await self.mcp.connect_all(MCP_SERVERS)

        self.tools = (
            self.local.browser_tools
            + self.mcp.tools
            + self.local.rag_tools
            + self.local.code_tools
            + WORKSPACE_TOOLS
            + [FINISH_TOOL]
        )
        self.system = build_system_prompt(
            has_browser=bool(self.local.browser_tools),
            has_rag=bool(self.local.rag_tools),
            has_code=bool(self.local.code_tools),
            has_analytics=bool(self.mcp.tools),
        )
        print(f"[forge] {len(self.tools)} tools ready | model {MODEL} | "
              f"workspace {WORKSPACE}\n")

    async def _dispatch(self, name: str, args: dict):
        try:
            if self.mcp.handles(name):
                return await self.mcp.call(name, args)
            return await self.local.call(name, args or {})
        except Exception as e:
            return f"Error in {name}: {type(e).__name__}: {e}"

    async def run(self, task: str) -> str:
        self.messages.append({"role": "user", "content": task})

        for _ in range(MAX_STEPS):
            resp = await self.client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=self.system,
                tools=self.tools,
                messages=self.messages,
            )
            self.messages.append({"role": "assistant", "content": resp.content})

            for block in resp.content:
                if block.type == "text" and block.text.strip():
                    print(f"\n[claude] {block.text.strip()}")

            if resp.stop_reason != "tool_use":
                return "".join(b.text for b in resp.content
                               if b.type == "text") or "(stopped without finishing)"

            # Every tool_use block needs a matching tool_result, finish included.
            # Returning early on finish used to leave the assistant turn dangling,
            # which is invisible in a one-shot run but rejects the *next* --chat
            # turn with a 400: the conversation carries over, the hole comes with it.
            results, final = [], None
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                if block.name == "finish":
                    final = block.input.get("result", "(finished with no result)")
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "Task marked complete.",
                    })
                    continue

                print(f"  -> {block.name}({_clip(str(block.input), 200)})")
                content = await self._dispatch(block.name, block.input)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content,
                })

            self.messages.append({"role": "user", "content": results})
            if final is not None:
                return final

        return f"Stopped after {MAX_STEPS} steps without finishing."


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #
async def run_once(task: str) -> str:
    async with AsyncExitStack() as stack:
        forge = Forge(stack)
        await forge.start()
        return await forge.run(task)


async def run_chat():
    async with AsyncExitStack() as stack:
        forge = Forge(stack)
        await forge.start()
        print("Interactive session. All four builds share one workspace and one "
              "conversation. 'exit' to quit.\n")
        while True:
            try:
                task = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not task:
                continue
            if task.lower() in ("exit", "quit"):
                break
            try:
                result = await forge.run(task)
                print(f"\n{'=' * 60}\n{result}\n{'=' * 60}\n")
            except Exception:
                traceback.print_exc()


def main():
    parser = argparse.ArgumentParser(
        description="Run the browser agent, RAG knowledge base, coding sub-agent and "
                    "MCP analytics server as one app."
    )
    parser.add_argument("task", nargs="*", help="The task (omit with --chat)")
    parser.add_argument("--chat", action="store_true", help="Interactive multi-turn session")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: set ANTHROPIC_API_KEY first.")
        sys.exit(1)

    sys.path.insert(0, str(HERE))  # so sibling builds import cleanly

    if args.chat:
        asyncio.run(run_chat())
        return

    task = " ".join(args.task).strip()
    if not task:
        print("Provide a task, or use --chat.")
        sys.exit(1)
    result = asyncio.run(run_once(task))
    print("\n" + "=" * 60 + "\nRESULT\n" + "=" * 60 + f"\n{result}")


if __name__ == "__main__":
    main()
