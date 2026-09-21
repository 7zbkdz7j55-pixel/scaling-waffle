# forge.py — the four builds as one app

One agent loop, one shared workspace, four capabilities that feed each other.

| Build | How it's wired in |
|---|---|
| `browser_agent.py` | imported — `BrowserController` + its tool schemas, reused as-is |
| `rag.py` | imported — `KnowledgeBase`, wrapped in three tools |
| `coding_agent.py` | delegated sub-agent — `code_task` runs it and reports back |
| `data_analytics_server.py` | **spawned as a real MCP server over stdio** |

The analytics build stays an MCP server. `forge.py` doesn't import it — it acts as an
MCP *host*: launches the server as a subprocess, discovers its tools at runtime, and
merges them into the same tool list as everything else (namespaced `analytics__*`).

Two things follow from that. Your server keeps working unchanged in Claude Desktop —
this is a second client, not a fork. And adding any other MCP server is one entry in
`MCP_SERVERS`; its tools appear automatically, no code changes.

## Hosting — this is not a web product

This repository is a **local CLI orchestrator**. It has no HTTP entrypoint
(`app.py`, `index.py`, `server.py`, `wsgi.py`, `asgi.py`). It is not the B2
storefront and it is not a Vercel app.

The live catalog is [https://www.b2stealthysolutions.com/](https://www.b2stealthysolutions.com/)
(repo `b2-store`). Support: support@b2stealthysolutions.com.

A Vercel project named `scaling-waffle` was attached to this repo. Production
deploy `dpl_CpyEaR8dyvyFh75qqTw5dNQV7HJY` failed with:

| Field | Value |
| --- | --- |
| Class | framework / agent-caused |
| Code | `PYTHON_ENTRYPOINT_NOT_FOUND` |
| Why | Vercel inferred a Python web runtime from `requirements.txt` |
| Public alias | `https://scaling-waffle.vercel.app/` → 404 `DEPLOYMENT_NOT_FOUND` |

**Do not add a dummy `app.py` to silence that error.** That would ship a fake
web product and hide the class. Retrying the same deploy without a config
change will fail the same way.

`vercel.json` in this repo disables Git deployments so the failure cannot
silently recur. Unlinking the Vercel project is a dashboard step (production
routing) and is intentionally not done in the same change.

See GitHub issue #2.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
npm install -g @anthropic-ai/claude-code    # for the coding sub-agent
export ANTHROPIC_API_KEY=sk-ant-...
```

Put `forge.py` next to the other four files and run it.

**The `mcp>=2.0` pin matters.** `data_analytics_server.py` in this repo is the migrated
v2 file — it imports `MCPServer` from `mcp.server.mcpserver`, and that module does not
exist on mcp 1.x, so installing `mcp<2` makes the server fail to start. The earlier
advice to pin `mcp<2` applied to the pre-migration server and no longer holds here; see
`MIGRATION.md` for the two-line diff and for the try/except import if you need one file
that runs on both. Forge's client side already reads both the camelCase (1.x) and
snake_case (2.x) field spellings, so the host is version-agnostic either way.

Any build whose dependencies are missing is skipped with a warning and the rest still
run. A server that fails to start degrades to a warning rather than taking down the app.

## What's in this repo

All four builds, plus the orchestrator and its tests. With everything installed a run
starts clean:

```
[mcp] analytics: 5 tools (list_sources, describe_source, run_sql, load_api, make_chart)
[forge] 18 tools ready | model claude-sonnet-5 | workspace …/workspace
```

Each build also runs on its own:

```bash
python browser_agent.py "what is on example.com"
python rag.py ingest ./workspace/docs && python rag.py search "pricing"
python coding_agent.py "add a --verbose flag and a test for it"
python data_analytics_server.py          # or register it with Claude Desktop
```

Drop any build's file out of the folder and forge skips it with a warning and keeps the
rest — with none of the three in-process builds present it still runs the analytics and
workspace halves on 8 tools.

### A note on the RAG embedder

`rag.py` computes true semantic embeddings with sentence-transformers when it can. When
that package is missing **or its model cannot be downloaded**, it falls back to a
feature-hashed bag-of-words index and says so on every load:

```
[rag] sentence-transformers not installed — falling back to LEXICAL search
      (keyword overlap only, no paraphrase matching).
```

The fallback is lexical, not semantic: it matches shared wording and misses paraphrase.
It exists so the build stays usable offline and without torch — don't mistake its
results for semantic search. `RAG_FORCE_HASHING=1` selects it deliberately.

## Tests

```bash
python test_forge.py     # the orchestrator: 45 checks
python test_builds.py    # the three in-process builds: 92 checks
```

No API key and no network needed — the agent loop runs against a stub client. Sections
skip rather than fail when a dependency is absent, the same way `forge.py` degrades.
`test_forge.py` spawns the real analytics server over stdio; `test_builds.py` drives
real Chromium against a temporary site on a loopback port. If Playwright's bundled
browser doesn't match your installed `playwright`, point the test at one you have:

```bash
FORGE_TEST_CHROMIUM=/path/to/chrome python test_builds.py
```

One gap worth stating plainly: the sentence-transformers path is covered against a
stand-in model (dimensions, normalisation, dtype, and a real `KnowledgeBase` search),
because downloading the actual model needs network. The lexical fallback is tested for
real end to end.

## Usage

```bash
python forge.py "Pull the top 10 HN stories, save them as CSV in the data folder, then chart score by story."
python forge.py --chat                  # multi-turn, one live session, browser stays open
HEADLESS=1 python forge.py "..."        # no visible browser window
```

## The shared workspace

```
workspace/
  data/      analytics queries these files directly with SQL
  docs/      RAG ingests these
  project/   the coding sub-agent's working directory
  notes/     scratch
```

`save_artifact` is the bridge between builds. Write to `data/` and the file is a
queryable table on the next `analytics__list_sources` — no restart, no reload. I tested
this end to end: saved a CSV mid-run, and the new table showed up in the very next
`list_sources` call.

## What chaining actually looks like

> **You:** Find last quarter's pricing on the three competitor sites in my notes, then
> tell me where we're exposed.

1. `rag_search` — pulls the competitor list out of your own documents
2. `navigate` + `get_page_content` — reads each pricing page
3. `save_artifact` to `data/` as CSV — the scraped prices become a table
4. `analytics__run_sql` — joins and compares them properly instead of by eyeball
5. `analytics__make_chart` — the PNG comes back through MCP and the model *sees* it
6. `finish`

Step 4 is the point. Without the analytics server the model does arithmetic over a
scraped table in its head, which is exactly where these things fabricate. Grounding it
in SQL is the difference between an answer and a guess.

## Things worth knowing

- **Lazy startup.** Chromium doesn't launch unless the task browses; the embedding model
  doesn't load unless the task searches. A pure-SQL task starts in about a second.
- **Context caps.** Page text, SQL results and sub-agent transcripts are clipped
  (`MAX_TOOL_CHARS`, default 8000). Raise it if you're truncating real results.
- **`code_task` is slow and costs tokens.** It's a full autonomous agent per call. The
  system prompt tells the orchestrator to use it for real code changes, not file reads.
- **Session persistence.** The coding sub-agent saves its session in
  `workspace/project/.claude_agent_session` and resumes across runs. Run that folder as a
  git repo and commit between tasks — resume restores the conversation, not your files.
- **`load_api` and the browser both pull untrusted text into the loop.** A scraped page
  can contain text aimed at steering the model. The analytics server's read-only default
  is doing real work here; leave `ALLOW_WRITES` off.
- **The coding sub-agent runs on an allowlist, not a bypass.** Nobody is there to answer
  a permission prompt, so `coding_agent.py` pre-approves a named set of tools
  (`Read/Write/Edit/Glob/Grep/Bash/TodoWrite/NotebookEdit`) under `acceptEdits` rather
  than `bypassPermissions`, and scopes them to the project folder. That matters more
  once a scraped page is in the same conversation: the surface stays auditable in one
  place. Widen it with `CODE_ALLOWED_TOOLS` only if you mean to.
- **Element indices go stale on purpose.** `get_page_content` re-stamps
  `data-forge-idx` on every read, and any click or navigation clears them, so a stale
  number gets a clear error instead of clicking the wrong thing.
- **Long `--chat` sessions grow the message list unboundedly.** If you're running dozens
  of turns, add trimming or start a fresh session.

## Model

Defaults to `claude-sonnet-5`, override with `CLAUDE_MODEL`. Your original files pinned
`claude-sonnet-4-6`, which still resolves — Sonnet 5 is the current generation and is
Anthropic's recommended model for agentic loops like this one.
