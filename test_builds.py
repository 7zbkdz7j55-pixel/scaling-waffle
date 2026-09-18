"""Tests for the three in-process builds: browser_agent, rag, coding_agent.

    python test_builds.py

No API key and no network required. Each section skips itself — rather than
failing — when its dependency is absent, mirroring how forge.py degrades.

The browser section drives real Chromium against a temporary local site served
on a loopback port. If Playwright's bundled browser does not match the installed
`playwright` package, point the test at a binary you do have:

    FORGE_TEST_CHROMIUM=/path/to/chrome python test_builds.py
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import os
import shutil
import socketserver
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).parent.resolve()
sys.path.insert(0, str(HERE))

PASSED: list[str] = []
FAILED: list[str] = []
SKIPPED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASSED if ok else FAILED).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    return ok


def section(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def skip(what: str, why: str) -> None:
    SKIPPED.append(f"{what} ({why})")
    print(f"  [SKIP] {why}")


# --------------------------------------------------------------------------- #
# rag.py
# --------------------------------------------------------------------------- #
PRICING_DOC = """# Competitor pricing review

Acme Corp charges $49 per seat per month on their Pro tier.
Globex offers a flat $500 monthly fee with unlimited seats.
Initech undercuts everyone at $19 per seat but caps API calls at 10k.
"""

RETRO_DOC = """# Q3 retrospective

Deployment velocity improved after we moved to trunk-based development.
The on-call rotation remains the biggest source of burnout for the team.
We should invest in better alerting before hiring more engineers.
"""


def test_rag():
    section("rag.py")
    try:
        import numpy  # noqa: F401
    except ImportError:
        skip("rag", "numpy not installed")
        return
    import rag

    # -- chunking, independent of any embedder --------------------------- #
    check("short text is one chunk", rag.chunk_text("hello world") == ["hello world"])
    check("empty text yields no chunks", rag.chunk_text("   ") == [])

    long_text = "\n\n".join(f"Paragraph {i}. " + "filler words here. " * 12
                            for i in range(20))
    pieces = rag.chunk_text(long_text, size=500, overlap=100)
    check("long text splits into several chunks", len(pieces) > 3, f"{len(pieces)}")
    check("no chunk wildly exceeds the size", max(len(p) for p in pieces) <= 700,
          f"max {max(len(p) for p in pieces)}")
    check("chunks are non-empty", all(p.strip() for p in pieces))
    joined = " ".join(pieces)
    check("no content dropped between chunks", "Paragraph 19" in joined)

    # overlap should make consecutive chunks share a tail/head
    overlapped = rag.chunk_text("x" * 300 + " " + "y" * 300, size=200, overlap=50)
    check("overlap produces multiple chunks", len(overlapped) > 1)

    workdir = Path(tempfile.mkdtemp(prefix="forge-rag-"))
    try:
        docs = workdir / "docs"
        docs.mkdir()
        (docs / "pricing.md").write_text(PRICING_DOC, encoding="utf-8")
        (docs / "retro.md").write_text(RETRO_DOC, encoding="utf-8")
        (docs / "ignore.bin").write_bytes(b"\x00\x01binary")

        kb = rag.KnowledgeBase(index_dir=workdir / "index")
        summary = kb.ingest_path(docs)
        check("ingest reports what it did", "Indexed" in summary, summary[:70])
        check("both documents indexed", len(kb.list_sources()) == 2,
              str([Path(s).name for s, _ in kb.list_sources()]))
        check("binary file ignored",
              not any("ignore.bin" in s for s, _ in kb.list_sources()))

        # -- search ranking ---------------------------------------------- #
        hits = kb.search("how much does Initech charge per seat", k=2)
        check("search returns hits", len(hits) > 0, f"{len(hits)}")
        if hits:
            top = hits[0]
            check("pricing question ranks the pricing doc first",
                  "pricing.md" in top["source"], Path(top["source"]).name)
            check("hit has the shape forge.py formats",
                  set(top) >= {"source", "chunk_index", "score", "text"},
                  str(sorted(top)))
            check("score is a float", isinstance(top["score"], float))
            check("scores are sorted descending",
                  all(hits[i]["score"] >= hits[i + 1]["score"]
                      for i in range(len(hits) - 1)))

        hits = kb.search("on-call burnout and alerting", k=1)
        check("retro question ranks the retro doc first",
              hits and "retro.md" in hits[0]["source"],
              Path(hits[0]["source"]).name if hits else "no hits")

        check("empty query returns nothing", kb.search("", k=3) == [])
        check("k larger than the corpus is clamped", len(kb.search("widget", k=99)) <= len(kb))

        # -- incremental ingest ------------------------------------------ #
        again = kb.ingest_path(docs)
        check("re-ingesting unchanged files skips them",
              "Nothing new" in again or "unchanged" in again, again[:70])

        (docs / "retro.md").write_text(RETRO_DOC + "\nWe also shipped the new dashboard.\n",
                                       encoding="utf-8")
        changed = kb.ingest_path(docs)
        check("a changed file is re-indexed", "Indexed" in changed, changed[:70])
        hits = kb.search("new dashboard shipped", k=1)
        check("new content is searchable",
              hits and "dashboard" in hits[0]["text"], "found" if hits else "no hits")
        check("re-indexing did not duplicate the document",
              len(kb.list_sources()) == 2, str(len(kb.list_sources())))

        # -- persistence --------------------------------------------------- #
        chunk_count = len(kb)
        reopened = rag.KnowledgeBase(index_dir=workdir / "index")
        check("index survives a reopen", len(reopened) == chunk_count,
              f"{len(reopened)} vs {chunk_count}")
        check("reopened index still searches",
              bool(reopened.search("Initech pricing", k=1)))
        check("reopened index did not re-embed from scratch",
              reopened.vectors is not None)

        # -- changing embedder re-embeds rather than corrupting ------------ #
        swapped = rag.KnowledgeBase(index_dir=workdir / "index",
                                    embedder=rag.HashingEmbedder(dim=256))
        hits = swapped.search("Initech per seat", k=1)
        check("a different embedder re-embeds and still works",
              bool(hits) and swapped.vectors.shape[1] == 256,
              f"dim {swapped.vectors.shape[1] if swapped.vectors is not None else '?'}")

        # -- misc ---------------------------------------------------------- #
        missing = kb.ingest_path(workdir / "nope")
        check("ingesting a missing path explains itself",
              "does not exist" in missing, missing[:50])
        empty_dir = workdir / "empty"
        empty_dir.mkdir()
        check("ingesting an empty folder explains itself",
              "No indexable" in kb.ingest_path(empty_dir))
        check("clear empties the index", "Cleared" in kb.clear() and len(kb) == 0)
        check("search on an empty index returns nothing", kb.search("anything") == [])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# rag.py — semantic embedder wiring
# --------------------------------------------------------------------------- #
def test_semantic_embedder():
    """Check the sentence-transformers wrapper against a stand-in model.

    Downloading the real model needs network, which is exactly what the fallback
    exists to survive — so this pins the wiring (which methods are called, with
    which arguments, and what comes back) without depending on a download.
    """
    section("rag.py — semantic embedder wiring")
    try:
        import numpy as np
        import sentence_transformers
    except ImportError as e:
        skip("semantic embedder", f"{e.name} not installed")
        return
    import rag

    seen: dict = {}

    class StandInModel:
        def __init__(self, name):
            seen["model_name"] = name

        def get_sentence_embedding_dimension(self):
            return 8

        def encode(self, texts, **kwargs):
            seen["texts"] = list(texts)
            seen["kwargs"] = kwargs
            # float64 on purpose: the wrapper is expected to narrow it.
            return np.linspace(0.1, 1.0, num=len(texts) * 8).reshape(len(texts), 8)

    original = sentence_transformers.SentenceTransformer
    sentence_transformers.SentenceTransformer = StandInModel
    try:
        embedder = rag.SentenceTransformerEmbedder("stand-in/model")
        check("dimension read from the model", embedder.dim == 8, str(embedder.dim))
        check("marked semantic", embedder.semantic is True)
        check("name identifies the model", embedder.name == "st:stand-in/model",
              embedder.name)
        check("model name passed through", seen["model_name"] == "stand-in/model")

        vectors = embedder.embed(["alpha", "beta"])
        check("shape is (n_texts, dim)", vectors.shape == (2, 8), str(vectors.shape))
        check("narrowed to float32", vectors.dtype == np.float32, str(vectors.dtype))
        check("normalisation requested from the model",
              seen["kwargs"].get("normalize_embeddings") is True)
        check("numpy requested from the model",
              seen["kwargs"].get("convert_to_numpy") is True)
        check("progress bar suppressed",
              seen["kwargs"].get("show_progress_bar") is False)
        check("texts forwarded unchanged", seen["texts"] == ["alpha", "beta"])

        empty = embedder.embed([])
        check("empty input returns an empty matrix of the right width",
              empty.shape == (0, 8), str(empty.shape))

        # It must also drop into a KnowledgeBase and drive a real search.
        workdir = Path(tempfile.mkdtemp(prefix="forge-sem-"))
        try:
            docs = workdir / "docs"
            docs.mkdir()
            (docs / "a.md").write_text(PRICING_DOC, encoding="utf-8")
            kb = rag.KnowledgeBase(index_dir=workdir / "idx", embedder=embedder)
            kb.ingest_path(docs)
            check("KnowledgeBase accepts the semantic embedder", len(kb) >= 1)
            check("index records it as semantic", kb.meta.get("semantic") is True,
                  str(kb.meta))
            check("search runs end to end", len(kb.search("pricing", k=1)) == 1)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
    finally:
        sentence_transformers.SentenceTransformer = original


# --------------------------------------------------------------------------- #
# coding_agent.py
# --------------------------------------------------------------------------- #
def test_coding_agent():
    section("coding_agent.py")
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        skip("coding_agent", "claude-agent-sdk not installed")
        return
    import coding_agent

    workdir = Path(tempfile.mkdtemp(prefix="forge-code-"))
    try:
        project = workdir / "project"

        # -- session round-trip -------------------------------------------- #
        check("no session before one is saved",
              coding_agent._load_session(project) is None)
        coding_agent._save_session(project, "sess-abc-123")
        check("session file created",
              (project / coding_agent.SESSION_FILE).exists())
        check("session round-trips",
              coding_agent._load_session(project) == "sess-abc-123")
        check("public alias agrees",
              coding_agent.load_session(project) == "sess-abc-123")
        check("saving an empty id is a no-op",
              (coding_agent._save_session(project, "") or True)
              and coding_agent._load_session(project) == "sess-abc-123")
        check("clear_session removes it", coding_agent.clear_session(project) is True)
        check("session is gone", coding_agent._load_session(project) is None)
        check("clearing twice is harmless",
              coding_agent.clear_session(project) is False)

        # A blank file must read as "no session", not as an empty resume token.
        (project / coding_agent.SESSION_FILE).write_text("  \n", encoding="utf-8")
        check("a blank session file reads as no session",
              coding_agent._load_session(project) is None)

        # -- options -------------------------------------------------------- #
        options = coding_agent.build_options(project)
        check("cwd is the project directory", str(options.cwd) == str(project),
              str(options.cwd))
        check("build_options creates the project directory", project.is_dir())
        check("no resume by default", options.resume is None)
        check("a model is set", bool(options.model), str(options.model))
        check("a system prompt is set", bool(options.system_prompt))
        check("edit tools are pre-approved",
              {"Read", "Write", "Edit"} <= set(options.allowed_tools),
              str(options.allowed_tools))
        check("Bash is pre-approved so it can run tests",
              "Bash" in options.allowed_tools)
        check("permission mode is not a blanket bypass",
              options.permission_mode != "bypassPermissions",
              str(options.permission_mode))
        check("max_turns is bounded", isinstance(options.max_turns, int)
              and options.max_turns > 0, str(options.max_turns))

        resumed = coding_agent.build_options(project, resume="sess-xyz")
        check("resume is threaded through", resumed.resume == "sess-xyz")

        custom = coding_agent.build_options(project, model="claude-opus-5", max_turns=3)
        check("model override honoured", custom.model == "claude-opus-5")
        check("max_turns override honoured", custom.max_turns == 3)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# browser_agent.py
# --------------------------------------------------------------------------- #
TEST_INDEX = """<!doctype html><html><head><title>Forge Test Page</title></head><body>
<h1>Widget Store</h1>
<p>Welcome to the store. We sell widgets.</p>
<a href="/about.html">About us</a>
<button onclick="document.getElementById('secret').style.display='block'">Reveal price</button>
<div id="secret" style="display:none">The price is 42 dollars.</div>
<form action="/about.html">
  <input type="text" name="q" placeholder="Search widgets">
  <input type="email" name="email" placeholder="Your email">
  <button type="submit">Go</button>
</form>
<input type="hidden" name="csrf" value="nope">
<button disabled>Disabled button</button>
</body></html>"""

TEST_ABOUT = """<!doctype html><html><head><title>About</title></head><body>
<h1>About Widget Store</h1><p>Founded in 1999 by two people in a garage.</p>
<a href="/index.html">Back home</a></body></html>"""


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # keep the test output readable
        pass


def _serve(directory: Path):
    handler = functools.partial(_QuietHandler, directory=str(directory))
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def test_browser():
    section("browser_agent.py")
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        skip("browser", "playwright not installed")
        return
    import browser_agent

    # Schemas need no browser at all.
    names = [t["name"] for t in browser_agent.TOOLS]
    check("TOOLS exports the expected set",
          set(names) == {"navigate", "get_page_content", "click", "type_text",
                         "fill_form", "screenshot", "finish"}, str(names))
    check("every tool has an object input_schema",
          all(t["input_schema"].get("type") == "object" for t in browser_agent.TOOLS))
    check("every tool has a description",
          all(t["description"].strip() for t in browser_agent.TOOLS))
    check("forge's finish filter leaves the six browser tools",
          len([t for t in browser_agent.TOOLS if t["name"] != "finish"]) == 6)
    check("the schema names match forge's dispatch table",
          {t["name"] for t in browser_agent.TOOLS if t["name"] != "finish"}
          == set(__import__("forge").BROWSER_TOOL_NAMES))

    asyncio.run(_browser_live())


async def _browser_live():
    from playwright.async_api import async_playwright
    import browser_agent

    site = Path(tempfile.mkdtemp(prefix="forge-site-"))
    (site / "index.html").write_text(TEST_INDEX, encoding="utf-8")
    (site / "about.html").write_text(TEST_ABOUT, encoding="utf-8")
    httpd, port = _serve(site)
    base = f"http://127.0.0.1:{port}"

    launch_kwargs = {"headless": True}
    override = os.environ.get("FORGE_TEST_CHROMIUM")
    if override:
        launch_kwargs["executable_path"] = override

    try:
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(**launch_kwargs)
            except Exception as e:
                first = str(e).strip().splitlines()[0]
                skip("browser (live)",
                     f"could not launch chromium: {first} — run "
                     "`playwright install chromium`, or set FORGE_TEST_CHROMIUM")
                return

            try:
                page = await browser.new_page()
                ctrl = browser_agent.BrowserController(page)

                out = await ctrl.click(0)
                check("acting before reading is refused, not crashed",
                      "call get_page_content" in out, out[:55])

                out = await ctrl.navigate(f"{base}/index.html")
                check("navigate loads the page", "Forge Test Page" in out,
                      out.splitlines()[0])

                content = await ctrl.get_page_content()
                elements = content.split("Page text:")[0]
                check("page text extracted", "We sell widgets" in content)
                check("link is listed", "About us" in elements)
                check("input placeholder surfaced", "Search widgets" in elements)
                check("hidden input excluded", "csrf" not in elements)
                check("disabled control excluded from the element list",
                      "Disabled button" not in elements)
                check("but disabled control still appears in page text",
                      "Disabled button" in content)
                check("indices start at 0", "[0]" in elements)

                reveal = next(e["index"] for e in ctrl._elements
                              if "Reveal" in (e["label"] or ""))
                out = await ctrl.click(reveal)
                check("same-page click reports no navigation",
                      "stayed on the same URL" in out, out.splitlines()[0])
                check("click invalidates the indices", ctrl._elements == [])
                content = await ctrl.get_page_content()
                check("DOM mutation is visible after re-reading",
                      "42 dollars" in content)

                text_i = next(e["index"] for e in ctrl._elements if e["type"] == "text")
                check("type_text works",
                      "Typed into" in await ctrl.type_text(text_i, "sprockets"))

                link_i = next(e["index"] for e in ctrl._elements if e["tag"] == "a")
                out = await ctrl.type_text(link_i, "nope")
                check("typing into a link is refused", "not a text field" in out,
                      out[:55])

                out = await ctrl.click(9999)
                check("out-of-range index explains itself",
                      "No element [9999]" in out, out[:55])

                mail_i = next(e["index"] for e in ctrl._elements if e["type"] == "email")
                out = await ctrl.fill_form([{"index": text_i, "text": "widget"},
                                            {"index": mail_i, "text": "a@b.com"}])
                check("fill_form fills several fields", "Filled 2 field(s)" in out,
                      out[:45])
                values = await page.eval_on_selector_all(
                    "input[type=text],input[type=email]", "els => els.map(e => e.value)")
                check("the values reached the DOM", values == ["widget", "a@b.com"],
                      str(values))
                check("fill_form rejects a bad payload",
                      "non-empty list" in await ctrl.fill_form("not a list"))

                await ctrl.get_page_content()
                about = next(e["index"] for e in ctrl._elements
                             if "About" in (e["label"] or ""))
                out = await ctrl.click(about)
                check("clicking a link navigates", "navigated to" in out,
                      out.splitlines()[0])
                check("the new page is readable",
                      "Founded in 1999" in await ctrl.get_page_content())

                png = await ctrl.screenshot()
                check("screenshot returns real PNG bytes",
                      isinstance(png, bytes) and png[:8] == b"\x89PNG\r\n\x1a\n",
                      f"{len(png)} bytes")

                out = await ctrl.navigate("http://127.0.0.1:1/nothing")
                check("an unreachable URL degrades to a message",
                      "Could not load" in out, out[:45])
            finally:
                await browser.close()
    finally:
        httpd.shutdown()
        shutil.rmtree(site, ignore_errors=True)


def main() -> int:
    print("forge builds: browser_agent, rag, coding_agent")
    test_rag()
    test_semantic_embedder()
    test_coding_agent()
    test_browser()

    print(f"\n{'=' * 60}")
    print(f"passed {len(PASSED)}   failed {len(FAILED)}   skipped {len(SKIPPED)}")
    for s in SKIPPED:
        print(f"  skipped: {s}")
    for f in FAILED:
        print(f"  FAILED:  {f}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
