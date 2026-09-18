"""
MCP Data Analytics Agent — a reference implementation. (mcp 2.x)

Exposes your local data (CSV / Parquet / JSON / Excel sitting in a data folder) and
JSON REST APIs to Claude as an MCP server. Claude can then run SQL analysis, render
charts, and explain the results in plain English — all inside a normal chat.

Engine: DuckDB (queries data files directly with SQL, no manual loading).
Charts:  matplotlib (returned to Claude as inline PNG images).

Requires Python 3.10+ and mcp >= 2.0.

Migrated from mcp 1.x. The only changes from the v1 file:
    from mcp.server.fastmcp import FastMCP, Image   ->  from mcp.server.mcpserver import MCPServer, Image
    mcp = FastMCP(...)                              ->  mcp = MCPServer(...)
Everything else — @mcp.tool(), mcp.run(transport="stdio"), Image(data=..., format=...) —
carries over unchanged. See MIGRATION.md.

Quick start
-----------
    pip install -r requirements.txt
    # put .csv/.parquet/.json/.xlsx files in the ./data folder next to this script

    # smoke-test with the MCP Inspector:
    npx @modelcontextprotocol/inspector python data_analytics_server.py
    # ...or register it with Claude Desktop (see README.md)

Tools exposed to Claude
-----------------------
    list_sources()                          what data is available
    describe_source(name)                   schema + sample rows for one table
    run_sql(query)                          run a read-only SQL query
    load_api(url, table_name, json_path)    pull a JSON API into a queryable table
    make_chart(sql, chart_type, x, y)       render a PNG chart from a query
"""

import io
import os
import re
from pathlib import Path

import duckdb
import httpx
import pandas as pd

import matplotlib
matplotlib.use("Agg")  # headless backend — must be set before importing pyplot
import matplotlib.pyplot as plt

from mcp.server.mcpserver import MCPServer, Image

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent / "data")).expanduser()
DATA_DIR.mkdir(parents=True, exist_ok=True)

ALLOW_WRITES = os.environ.get("ALLOW_WRITES", "").lower() in ("1", "true", "yes")
MAX_ROWS = int(os.environ.get("MAX_ROWS", "100"))  # cap rows returned to the model

# File extension -> DuckDB reader function. Excel is handled separately via pandas.
FILE_READERS = {
    ".csv": "read_csv_auto",
    ".parquet": "read_parquet",
    ".json": "read_json_auto",
    ".ndjson": "read_json_auto",
}

con = duckdb.connect(database=":memory:")  # single in-process analytics database

mcp = MCPServer(
    "Data Analytics",
    instructions=(
        "Analyze the user's local data. Typical flow: call list_sources to see what's "
        "available, describe_source to inspect a table's schema, run_sql to analyze, and "
        "make_chart to visualize. After getting results, explain what they mean in plain "
        "English and surface anything notable (trends, outliers, gaps). Queries are "
        "read-only by default."
    ),
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _safe_ident(name: str) -> str:
    """Turn a filename/label into a safe SQL identifier."""
    ident = re.sub(r"\W+", "_", name).strip("_").lower()
    if not ident:
        ident = "tbl"
    if ident[0].isdigit():
        ident = "_" + ident
    return ident


def _register_file_sources() -> list[str]:
    """(Re)register every supported data file in DATA_DIR as a DuckDB view/table.

    Called at startup and at the top of each tool so newly added or edited files are
    always visible. Tables loaded via load_api are separate and never clobbered here.
    """
    registered = []
    for path in sorted(DATA_DIR.iterdir()):
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        view = _safe_ident(path.stem)
        try:
            if ext in FILE_READERS:
                reader = FILE_READERS[ext]
                con.execute(
                    f'CREATE OR REPLACE VIEW "{view}" AS '
                    f"SELECT * FROM {reader}('{path.as_posix()}')"
                )
                registered.append(view)
            elif ext in (".xlsx", ".xls"):
                df = pd.read_excel(path)
                con.register("_incoming", df)
                con.execute(f'CREATE OR REPLACE TABLE "{view}" AS SELECT * FROM _incoming')
                con.unregister("_incoming")
                registered.append(view)
        except Exception as e:  # skip unreadable files, keep the rest working
            print(f"[warn] could not register {path.name}: {e}")
    return registered


def _is_read_only(query: str) -> bool:
    """True if the query only reads data (SELECT/WITH/DESCRIBE/etc.)."""
    stripped = re.sub(r"^(\s*--[^\n]*\n)+", "", query.lstrip()).lstrip()
    return bool(
        re.match(
            r"(SELECT|WITH|FROM|DESCRIBE|DESC|SHOW|EXPLAIN|PRAGMA|SUMMARIZE)\b",
            stripped,
            re.IGNORECASE,
        )
    )


def _df_to_text(df: pd.DataFrame) -> str:
    total = len(df)
    body = df.head(MAX_ROWS).to_string(index=False)
    if total > MAX_ROWS:
        body += f"\n... ({total - MAX_ROWS} more rows; {total} total)"
    else:
        body += f"\n({total} row{'s' if total != 1 else ''})"
    return body


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
@mcp.tool()
def list_sources() -> str:
    """List the data tables available to query (files in the data folder plus anything
    loaded from an API). Call this first to see what you can analyze."""
    file_views = _register_file_sources()
    rows = con.execute(
        "SELECT table_name, table_type FROM information_schema.tables "
        "WHERE table_schema = 'main' ORDER BY table_name"
    ).fetchall()
    if not rows:
        return (
            f"No data found yet. Drop .csv / .parquet / .json / .xlsx files into:\n"
            f"  {DATA_DIR}\n"
            "or use load_api to pull data from a JSON endpoint, then call list_sources again."
        )
    lines = [f"Data folder: {DATA_DIR}", "", "Available tables:"]
    for name, ttype in rows:
        kind = "file" if name in file_views else ttype.lower()
        lines.append(f"  - {name}  [{kind}]")
    lines.append(
        f"\nQuery any of these by name with run_sql, e.g. SELECT * FROM {rows[0][0]} LIMIT 5;"
    )
    return "\n".join(lines)


@mcp.tool()
def describe_source(name: str) -> str:
    """Show the schema (columns and types), row count, and a few sample rows for one
    table. Use the table names returned by list_sources."""
    _register_file_sources()
    table = _safe_ident(name)
    try:
        schema = con.execute(f'DESCRIBE "{table}"').fetchdf()
        count = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        sample = con.execute(f'SELECT * FROM "{table}" LIMIT 5').fetchdf()
    except Exception as e:
        return f"Could not describe '{name}': {e}\nTry list_sources to see valid names."
    return (
        f"Table: {table}  ({count} rows)\n\n"
        f"Schema:\n{schema.to_string(index=False)}\n\n"
        f"Sample rows:\n{sample.to_string(index=False)}"
    )


@mcp.tool()
def run_sql(query: str) -> str:
    """Run a SQL query against the available tables and return the results (DuckDB SQL).
    Read-only by default — SELECT / WITH / DESCRIBE / SHOW / etc. Reference tables by the
    names shown in list_sources. Supports joins, aggregations, window functions, and CTEs."""
    _register_file_sources()
    if not ALLOW_WRITES and not _is_read_only(query):
        return (
            "Refused: this server runs read-only queries by default. Use a SELECT/WITH "
            "query, or set the ALLOW_WRITES=1 environment variable to enable writes."
        )
    try:
        df = con.execute(query).fetchdf()
    except Exception as e:
        return f"SQL error: {e}"
    if df.empty:
        return "Query ran successfully and returned 0 rows."
    return _df_to_text(df)


@mcp.tool()
def load_api(url: str, table_name: str, json_path: str = "") -> str:
    """Fetch JSON from a REST API (HTTP GET) and load it into a queryable table.
    - url: the endpoint to GET.
    - table_name: name to give the resulting table.
    - json_path: optional dotted path to the array inside the response (e.g. 'data.results').
      Leave empty if the response body is already a list of records."""
    try:
        resp = httpx.get(
            url, timeout=30, follow_redirects=True,
            headers={"User-Agent": "mcp-data-analytics/1.0"},
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        return f"Failed to fetch {url}: {e}"

    if json_path:
        for key in json_path.split("."):
            if isinstance(payload, dict) and key in payload:
                payload = payload[key]
            else:
                return f"json_path '{json_path}' not found (stopped at key '{key}')."

    try:
        df = pd.json_normalize(payload)
    except Exception as e:
        return f"Could not turn the response into a table: {e}"
    if df.empty:
        return "The API returned no rows."

    table = _safe_ident(table_name)
    con.register("_incoming", df)
    con.execute(f'CREATE OR REPLACE TABLE "{table}" AS SELECT * FROM _incoming')
    con.unregister("_incoming")
    return (
        f"Loaded {len(df)} rows into table '{table}' ({len(df.columns)} columns). "
        "Query it with run_sql or visualize it with make_chart."
    )


@mcp.tool()
def make_chart(sql: str, chart_type: str = "bar", x: str = "", y: str = "",
               title: str = "") -> Image:
    """Run a query and render a chart as a PNG image returned inline.
    - sql: a SELECT query returning the columns to plot.
    - chart_type: 'bar', 'line', 'scatter', or 'hist'.
    - x: column for the x-axis (or the value column for 'hist'). Defaults to the 1st column.
    - y: column for the y-axis (not used for 'hist'). Defaults to the 2nd column.
    - title: optional chart title."""
    if not ALLOW_WRITES and not _is_read_only(sql):
        raise ValueError("make_chart only runs read-only SELECT queries.")
    _register_file_sources()
    df = con.execute(sql).fetchdf()
    if df.empty:
        raise ValueError("The query returned no rows to plot.")

    cols = list(df.columns)
    x = x or cols[0]
    if x not in df.columns:
        raise ValueError(f"Column '{x}' not in result. Available: {cols}")
    if chart_type != "hist":
        y = y or (cols[1] if len(cols) > 1 else cols[0])
        if y not in df.columns:
            raise ValueError(f"Column '{y}' not in result. Available: {cols}")

    fig, ax = plt.subplots(figsize=(8, 4.5))
    try:
        if chart_type == "bar":
            ax.bar(df[x].astype(str), df[y])
            ax.set_xlabel(x); ax.set_ylabel(y)
            plt.xticks(rotation=45, ha="right")
        elif chart_type == "line":
            ax.plot(df[x], df[y], marker="o")
            ax.set_xlabel(x); ax.set_ylabel(y)
        elif chart_type == "scatter":
            ax.scatter(df[x], df[y])
            ax.set_xlabel(x); ax.set_ylabel(y)
        elif chart_type == "hist":
            ax.hist(df[x].dropna(), bins=20)
            ax.set_xlabel(x); ax.set_ylabel("count")
        else:
            raise ValueError(
                f"Unknown chart_type '{chart_type}'. Use bar, line, scatter, or hist."
            )
        ax.set_title(title or f"{chart_type} of {y or x}")
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=120)
        return Image(data=buf.getvalue(), format="png")
    finally:
        plt.close(fig)


if __name__ == "__main__":
    _register_file_sources()
    mcp.run(transport="stdio")
