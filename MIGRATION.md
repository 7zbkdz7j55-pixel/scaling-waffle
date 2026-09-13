# Analytics server: mcp 1.x → 2.x

## What actually changed

Two lines. That's the whole migration.

```diff
- from mcp.server.fastmcp import FastMCP, Image
+ from mcp.server.mcpserver import MCPServer, Image

- mcp = FastMCP(
+ mcp = MCPServer(
      "Data Analytics",
      instructions=(...),
  )
```

Everything else carries over untouched: `@mcp.tool()` takes the same arguments,
`mcp.run(transport="stdio")` is unchanged, and `Image(data=..., format="png")` has the
same constructor. All five tools, both docstring-derived schemas and the read-only
write-guard, work as they did.

## Pin

The old import path was removed, not deprecated, so this file needs v2:

```
mcp>=2.0
```

This is a one-way move — the migrated file will not run on mcp 1.x. If you'd rather keep
the repo working on both while you transition, swap the import for:

```python
try:
    from mcp.server.mcpserver import MCPServer, Image      # mcp 2.x
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer, Image   # mcp 1.x
```

## Verified

Tested against mcp 2.0.0 over stdio through forge.py's MCP bridge:

- `list_sources` — file registration and table listing
- `describe_source` — schema, row count, sample rows
- `run_sql` — aggregation over a CSV
- `make_chart` — PNG returned as an inline image block
- write-guard — `DROP TABLE` correctly refused
- `load_api` — clean error on an unreachable endpoint

One thing I could not test here: Claude Desktop as the client. The v2 SDK serves both
protocol eras, so it should be fine, but run it through Claude Desktop once before you
rely on it.

`forge.py` needs no changes — its bridge already reads both the camelCase (1.x) and
snake_case (2.x) field spellings.

## Committing it

```bash
cd /path/to/your/repo
git checkout -b mcp-v2

# drop in the migrated file, then:
git add data_analytics_server.py MIGRATION.md
git commit -m "Migrate analytics MCP server to mcp 2.x

FastMCP was renamed MCPServer and moved from mcp.server.fastmcp to
mcp.server.mcpserver in mcp 2.0; the old import path was removed rather
than deprecated. Decorators, run() and Image are unchanged.

Requires mcp>=2.0."

git push -u origin mcp-v2
```

Bump the dependency pin to `mcp>=2.0` in the same commit so a fresh install doesn't land
on 1.x and fail the other way. In this repo that pin lives in `requirements.txt` and is
already set.
