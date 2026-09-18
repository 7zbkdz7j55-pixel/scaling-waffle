"""Regression tests for forge.py.

    python test_forge.py

No API key required: the agent loop runs against a stub client that returns
canned responses. The MCP section spawns the real analytics server over stdio
and is skipped, not failed, when its dependencies are absent — the same way
forge.py itself degrades.

Covers the three defects fixed alongside these tests:
  1. the system prompt naming tools whose build was skipped at startup
  2. finish leaving a tool_use block unanswered, which rejects the next
     --chat turn with a 400
  3. dispatch falling through to Chromium for names the browser doesn't own
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path

HERE = Path(__file__).parent.resolve()
sys.path.insert(0, str(HERE))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-stub-no-network-calls-made")

import forge  # noqa: E402
from forge import Forge, LocalTools, build_system_prompt  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []
SKIPPED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASSED if ok else FAILED).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    return ok


def section(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


# --------------------------------------------------------------------------- #
# Stub Anthropic client
# --------------------------------------------------------------------------- #
class Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Response:
    def __init__(self, content, stop_reason):
        self.content, self.stop_reason = content, stop_reason


class StubMessages:
    """Replays a scripted list of responses and records what it was sent."""

    def __init__(self, script):
        self.script, self.turn, self.received = list(script), 0, []

    async def create(self, **kwargs):
        self.received.append([dict(m) for m in kwargs["messages"]])
        resp = self.script[min(self.turn, len(self.script) - 1)]
        self.turn += 1
        return resp


class StubClient:
    def __init__(self, script):
        self.messages = StubMessages(script)


def assert_tool_results_balanced(messages) -> list[str]:
    """Mirror the Messages API rule: each tool_use needs a tool_result next."""
    problems = []
    for i, msg in enumerate(messages):
        if msg["role"] != "assistant" or not isinstance(msg["content"], list):
            continue
        pending = [getattr(b, "id", None) for b in msg["content"]
                   if getattr(b, "type", None) == "tool_use"]
        if not pending:
            continue
        nxt = messages[i + 1] if i + 1 < len(messages) else None
        answered = set()
        if nxt and nxt["role"] == "user" and isinstance(nxt["content"], list):
            answered = {b.get("tool_use_id") for b in nxt["content"]
                        if isinstance(b, dict) and b.get("type") == "tool_result"}
        missing = [t for t in pending if t not in answered]
        if missing:
            problems.append(f"messages[{i}] tool_use {missing} unanswered")
    return problems


# --------------------------------------------------------------------------- #
# 1. System prompt only advertises builds that loaded
# --------------------------------------------------------------------------- #
def test_system_prompt_gating():
    section("system prompt gating")

    only_sql = build_system_prompt(has_browser=False, has_rag=False,
                                   has_code=False, has_analytics=True)
    for absent in ("rag_ingest", "rag_search", "rag_list", "code_task",
                   "navigate", "get_page_content"):
        check(f"analytics-only prompt omits {absent}", absent not in only_sql)
    check("analytics-only prompt keeps analytics__run_sql",
          "analytics__run_sql" in only_sql)
    check("analytics-only prompt does not claim four capabilities",
          "four capabilities" not in only_sql)
    check("analytics-only prompt drops the coding project folder line",
          "coding sub-agent's working directory" not in only_sql)

    everything = build_system_prompt(True, True, True, True)
    for present in ("navigate", "analytics__run_sql", "rag_search", "code_task"):
        check(f"full prompt keeps {present}", present in everything)
    check("full prompt says four capabilities", "four capabilities" in everything)

    rag_only = build_system_prompt(has_browser=False, has_rag=True,
                                   has_code=False, has_analytics=False)
    check("rag-only prompt omits analytics__run_sql",
          "analytics__run_sql" not in rag_only)
    check("rag-only prompt says 'Local text', not 'Web or local text'",
          "Local text" in rag_only and "Web or local" not in rag_only)

    nothing = build_system_prompt(False, False, False, False)
    check("no-build prompt still mentions the workspace tools",
          "save_artifact" in nothing)
    check("no-build prompt has no chaining section",
          "How to chain them" not in nothing)


# --------------------------------------------------------------------------- #
# 2. finish answers its own tool_use, so --chat survives turn two
# --------------------------------------------------------------------------- #
def test_finish_keeps_chat_valid():
    section("finish / --chat message invariant")

    async def run():
        script = [
            Response([Block(type="text", text="Working."),
                      Block(type="tool_use", id="tu_ws_1", name="list_workspace",
                            input={})], "tool_use"),
            Response([Block(type="tool_use", id="tu_fin_1", name="finish",
                            input={"result": "first answer"})], "tool_use"),
            Response([Block(type="tool_use", id="tu_fin_2", name="finish",
                            input={"result": "second answer"})], "tool_use"),
        ]
        async with AsyncExitStack() as stack:
            agent = Forge(stack)
            agent.client = StubClient(script)
            agent.tools, agent.system = [], "stub"

            first = await agent.run("task one")
            check("turn 1 returns the finish result", first == "first answer", first)
            problems = assert_tool_results_balanced(agent.messages)
            check("no dangling tool_use after turn 1", not problems, "; ".join(problems))

            second = await agent.run("task two")
            check("turn 2 returns its own result", second == "second answer", second)
            problems = assert_tool_results_balanced(agent.messages)
            check("no dangling tool_use after turn 2", not problems, "; ".join(problems))

            # What turn 2 actually put on the wire is what the API would reject.
            sent = agent.client.messages.received[-1]
            check("history carried into turn 2", len(sent) >= 4, f"{len(sent)} messages")
            check("the wire payload for turn 2 is balanced",
                  not assert_tool_results_balanced(sent))

    asyncio.run(run())

    async def run_parallel():
        """finish emitted alongside another tool must answer both blocks."""
        script = [Response([
            Block(type="tool_use", id="tu_par_ws", name="list_workspace", input={}),
            Block(type="tool_use", id="tu_par_fin", name="finish",
                  input={"result": "done"}),
        ], "tool_use")]
        async with AsyncExitStack() as stack:
            agent = Forge(stack)
            agent.client = StubClient(script)
            agent.tools, agent.system = [], "stub"
            out = await agent.run("parallel task")
            check("finish beside another tool still returns", out == "done", out)
            problems = assert_tool_results_balanced(agent.messages)
            check("both parallel tool_use blocks answered", not problems,
                  "; ".join(problems))

    asyncio.run(run_parallel())


# --------------------------------------------------------------------------- #
# 3. Dispatch does not fall through to Chromium
# --------------------------------------------------------------------------- #
def test_dispatch_guard():
    section("dispatch guard")

    async def run():
        async with AsyncExitStack() as stack:
            local = LocalTools(stack)
            local.browser_tools = []  # browser build skipped

            out = await local.call("totally_made_up_tool", {})
            check("unknown tool reports cleanly", "Unknown tool" in str(out), str(out))
            check("unknown tool did not launch Chromium",
                  local._browser_ctrl is None)

            out = await local.call("navigate", {"url": "https://example.com"})
            check("browser tool without the build explains itself",
                  "did not load" in str(out), str(out)[:70])
            check("still no Chromium launched", local._browser_ctrl is None)

            # Workspace tools work with no build loaded at all.
            out = await local.call("save_artifact", {
                "folder": "notes", "filename": "t.txt", "content": "hello"})
            check("save_artifact works standalone", "Saved" in str(out))
            check("save_artifact wrote the file",
                  (forge.NOTES_DIR / "t.txt").read_text() == "hello")

            out = await local.call("save_artifact", {
                "folder": "notes", "filename": "../../escape.txt", "content": "x"})
            check("save_artifact strips path traversal",
                  not (HERE.parent / "escape.txt").exists()
                  and (forge.NOTES_DIR / "escape.txt").exists())

            out = await local.call("list_workspace", {})
            check("list_workspace lists folders", "notes" in str(out) and "data" in str(out))

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# 4. The real MCP server over stdio
# --------------------------------------------------------------------------- #
def test_mcp_bridge():
    section("MCP bridge (real server over stdio)")
    try:
        import mcp  # noqa: F401
        import duckdb  # noqa: F401
        import pandas  # noqa: F401
        import matplotlib  # noqa: F401
    except ImportError as e:
        SKIPPED.append(f"MCP bridge ({e.name} not installed)")
        print(f"  [SKIP] {e.name} not installed — pip install -r requirements.txt")
        return
    if "analytics" not in forge.MCP_SERVERS:
        SKIPPED.append("MCP bridge (data_analytics_server.py not found)")
        print("  [SKIP] data_analytics_server.py not next to forge.py")
        return

    async def run():
        csv = forge.DATA_DIR / "forgetest_scores.csv"
        csv.write_text("name,score\nalice,10\nbob,32\ncarol,58\n", encoding="utf-8")
        try:
            async with AsyncExitStack() as stack:
                bridge = forge.MCPBridge(stack)
                await bridge.connect_all(forge.MCP_SERVERS)

                names = [t["name"] for t in bridge.tools]
                if not check("server started and exposed tools", bool(names)):
                    return
                check("tools are namespaced analytics__*",
                      all(n.startswith("analytics__") for n in names))
                check("input_schema parsed on this mcp version",
                      all(t["input_schema"].get("type") == "object"
                          for t in bridge.tools))

                out = await bridge.call("analytics__list_sources", {})
                check("list_sources sees the seeded CSV", "forgetest_scores" in str(out))

                out = await bridge.call("analytics__run_sql",
                                        {"query": "SELECT SUM(score) AS s FROM forgetest_scores"})
                check("run_sql aggregates", "100" in str(out),
                      str(out).replace("\n", " ")[:60])

                out = await bridge.call("analytics__run_sql",
                                        {"query": "DROP TABLE forgetest_scores"})
                check("read-only guard refuses DROP", "Refused" in str(out))

                out = await bridge.call("analytics__make_chart", {
                    "sql": "SELECT name, score FROM forgetest_scores",
                    "chart_type": "bar", "x": "name", "y": "score"})
                img = next((b for b in out if b.get("type") == "image"), None) \
                    if isinstance(out, list) else None
                if check("make_chart returns an image block", img is not None):
                    raw = base64.b64decode(img["source"]["data"], validate=True)
                    check("image is a real PNG the model can see",
                          raw[:8] == b"\x89PNG\r\n\x1a\n", f"{len(raw)} bytes")
                    check("media_type resolved on this mcp version",
                          img["source"]["media_type"] == "image/png")

                # The README's headline claim: no restart between builds.
                (forge.DATA_DIR / "forgetest_added.csv").write_text("k,v\na,7\n",
                                                                encoding="utf-8")
                out = await bridge.call("analytics__list_sources", {})
                check("a file saved mid-run is queryable with no restart",
                      "forgetest_added" in str(out))

                out = await bridge.call("analytics__describe_source", {"name": "nope"})
                check("unknown table gives a clean error",
                      "Could not describe" in str(out))
        finally:
            for leftover in ("forgetest_scores.csv", "forgetest_added.csv"):
                (forge.DATA_DIR / leftover).unlink(missing_ok=True)

    asyncio.run(run())


def main() -> int:
    print(f"forge.py regression tests\nworkspace: {forge.WORKSPACE}")
    test_system_prompt_gating()
    test_finish_keeps_chat_valid()
    test_dispatch_guard()
    test_mcp_bridge()

    print(f"\n{'=' * 60}")
    print(f"passed {len(PASSED)}   failed {len(FAILED)}   skipped {len(SKIPPED)}")
    for s in SKIPPED:
        print(f"  skipped: {s}")
    for f in FAILED:
        print(f"  FAILED:  {f}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
