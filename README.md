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

`forge.py`, `data_analytics_server.py` and the tests. The three in-process builds —
`browser_agent.py`, `rag.py` and `coding_agent.py` — are **not** included yet. Forge
detects that at startup and runs without them:

```
[warn] browser tools unavailable: ModuleNotFoundError: No module named 'browser_agent'
[warn] RAG tools unavailable (ModuleNotFoundError: No module named 'rag') …
[warn] coding sub-agent unavailable: ModuleNotFoundError: No module named 'claude_agent_sdk'
[mcp] analytics: 5 tools (list_sources, describe_source, run_sql, load_api, make_chart)
[forge] 8 tools ready | model claude-sonnet-5 | workspace …/workspace
```

Drop the three files alongside `forge.py` and their tools appear on the next run, no
code changes. Until then the analytics and workspace halves are fully usable.

## Tests

```bash
python test_forge.py
```

No API key needed — the agent loop runs against a stub client. The MCP section spawns
the real analytics server over stdio and skips itself if duckdb/pandas/matplotlib are
missing, the same way `forge.py` degrades.

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
- **Long `--chat` sessions grow the message list unboundedly.** If you're running dozens
  of turns, add trimming or start a fresh session.

## Model

Defaults to `claude-sonnet-5`, override with `CLAUDE_MODEL`. Your original files pinned
`claude-sonnet-4-6`, which still resolves — Sonnet 5 is the current generation and is
Anthropic's recommended model for agentic loops like this one.
