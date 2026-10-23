"""Security regression checks for data_analytics_server.py.

Runs against the real module with DATA_DIR pointed at a temp folder. The attacks
here are the ones that worked before the sandbox and the statement gate: reading
files outside the data folder, stacked writes, filename injection, and EXPLAIN
ANALYZE executing a write. Legitimate paths (every file format, load_api, charts)
are checked too, so the lock cannot silently break them.

    python test_analytics_security.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent.resolve()
P, F = [], []


def check(name, ok, detail=""):
    (P if ok else F).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    return ok


def build_fixture():
    import duckdb

    work = Path(tempfile.mkdtemp(prefix="analytics-sec-"))
    data = work / "data"
    data.mkdir()
    secrets = work / "secrets"
    secrets.mkdir()
    (secrets / "fake_env").write_text("KEY=sk-ant-REDACTED\n")

    sibling = work / "data_evil"          # shares the DATA_DIR prefix
    sibling.mkdir()
    (sibling / "x.csv").write_text("a\n1\n")
    (sibling / "stolen.txt").write_text("sibling-secret\n")

    (data / "sales.csv").write_text("id,amt\n1,10\n2,20\n")
    (data / "it's.csv").write_text("id\n7\n")          # apostrophe in a real name
    (data / "rows.json").write_text(json.dumps([{"k": 1}, {"k": 2}]))
    (data / "events.ndjson").write_text('{"e": "a"}\n{"e": "b"}\n')

    # Parquet is written with a separate, unsandboxed connection (test setup only).
    setup = duckdb.connect()
    setup.execute(f"COPY (SELECT 5 AS p) TO '{data / 'prices.parquet'}' (FORMAT parquet)")

    import pandas as pd
    pd.DataFrame({"x": [1, 2, 3]}).to_excel(data / "book.xlsx", index=False)

    return work, data, secrets, sibling


def main():
    work, data, secrets, sibling = build_fixture()
    os.environ["DATA_DIR"] = str(data)
    os.environ.pop("ALLOW_WRITES", None)
    sys.path.insert(0, str(HERE))
    import data_analytics_server as das

    def tables():
        return {r[0] for r in das.con.execute(
            "SELECT table_name FROM information_schema.tables").fetchall()}

    def fresh_keep():
        das.con.execute("DROP TABLE IF EXISTS keep")
        das.con.execute("CREATE TABLE keep AS SELECT 1 AS a")

    print("Attacks")
    fresh_keep()
    out = das.run_sql(f"SELECT * FROM read_text('{secrets / 'fake_env'}')")
    check("read a file outside the data folder is refused", "sk-ant-REDACTED" not in out, out[:60])

    fresh_keep()
    out = das.run_sql(f"SELECT * FROM read_text('{sibling / 'stolen.txt'}')")
    check("sibling folder sharing the DATA_DIR prefix is refused",
          "sibling-secret" not in out, out[:60])

    fresh_keep()
    out = das.run_sql(f"SELECT * FROM glob('{work}/*')")
    check("glob listing outside the data folder is refused",
          out.startswith("SQL error") and "Permission" in out, out[:60])

    fresh_keep()
    out = das.run_sql(f"SELECT * FROM read_text('{das.DATA_DIR}/../secrets/fake_env')")
    check("'..' out of the data folder is refused", "sk-ant-REDACTED" not in out, out[:60])

    fresh_keep()
    out = das.run_sql("SELECT * FROM read_text('/proc/self/environ')")
    check("the process environment cannot be read as a file", "PATH=" not in out, out[:60])

    fresh_keep()
    out = das.run_sql("SELECT getenv('HOME') AS h")
    check("environment variables are not reachable from SQL", "/root" not in out
          and "/home" not in out, out[:60])

    fresh_keep()
    out = das.run_sql("SELECT 1; DROP TABLE keep")
    check("stacked DROP after a SELECT is refused", "keep" in tables() and "Refused" in out,
          out[:60])

    fresh_keep()
    das.run_sql("EXPLAIN ANALYZE INSERT INTO keep VALUES (5)")
    rows = das.con.execute("SELECT count(*) FROM keep").fetchone()[0]
    check("EXPLAIN ANALYZE does not execute a write", rows == 1, f"rows={rows}")

    fresh_keep()
    out = das.run_sql("SET enable_external_access = true")
    check("SET cannot re-enable external access", "Refused" in out, out[:60])
    out = das.run_sql(f"SELECT * FROM read_text('{secrets / 'fake_env'}')")
    check("file read still blocked after a SET attempt", "sk-ant-REDACTED" not in out)

    das.con.execute("CREATE TABLE keep2 AS SELECT 1 AS a")
    try:
        das.con.execute("SET enable_external_access = true")
        reset = True
    except Exception:
        reset = False
    check("the lock cannot be reset from the connection", not reset)

    (data / "keep_me'); DROP TABLE keep; --.csv").write_text("a\n1\n")
    (data / "keep_me").write_text("a\n1\n")         # the truncated path must resolve
    fresh_keep()
    das._register_file_sources()
    check("filename breakout does not drop a table", "keep" in tables())

    (data / "glob*.csv").write_text("a\n1\n")
    listing = das.list_sources()
    check("a file name with * is skipped, not registered as a glob", "glob" not in listing)

    print("\nLegitimate paths")
    listing = das.list_sources()
    for name in ("sales", "rows", "events", "prices", "book"):
        check(f"file registered: {name}", name in listing)
    check("apostrophe in a file name registers", "it_s" in listing)

    check("plain SELECT works", "6" in das.run_sql("SELECT 2*3 AS six"))
    check("CTE works",
          "9" in das.run_sql("WITH a AS (SELECT 3 AS x) SELECT x*3 AS y FROM a"))
    check("csv view is queryable", "30" in das.run_sql("SELECT sum(amt) FROM sales"))
    check("json view is queryable", "2" in das.run_sql("SELECT count(*) FROM rows"))
    check("ndjson view is queryable", "2" in das.run_sql("SELECT count(*) FROM events"))
    check("parquet view is queryable", "5" in das.run_sql("SELECT p FROM prices"))
    check("excel table is queryable", "6" in das.run_sql("SELECT sum(x) FROM book"))
    check("describe_source works", "Schema" in das.describe_source("sales"))

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"items": [{"n": 1}, {"n": 2}]}

    real_get = das.httpx.get
    das.httpx.get = lambda *a, **k: FakeResp()
    try:
        msg = das.load_api("https://example.invalid/data", "api_rows", "items")
    finally:
        das.httpx.get = real_get
    check("load_api loads into a table under the lock", "Loaded 2 rows" in msg, msg[:60])
    check("loaded api table is queryable", "3" in das.run_sql("SELECT sum(n) AS s FROM api_rows"))

    rel = os.getcwd()
    check("DATA_DIR is absolute, so a later chdir cannot break reads",
          Path(das.DATA_DIR).is_absolute())
    os.chdir(str(work))
    check("file views still read after the working directory changes",
          "30" in das.run_sql("SELECT sum(amt) FROM sales"))
    os.chdir(rel)

    chart = das.make_chart("SELECT id, amt FROM sales", chart_type="bar")
    check("make_chart renders under the lock", chart is not None)

    print("\nALLOW_WRITES=1 keeps the sandbox")
    probe = (
        "import os, sys; sys.path.insert(0, %r); import data_analytics_server as d;"
        "print('LEAK' if 'sk-ant-REDACTED' in d.run_sql(\"SELECT * FROM read_text('%s')\") "
        "else 'SAFE')"
    ) % (str(HERE), secrets / "fake_env")
    env = dict(os.environ, DATA_DIR=str(data), ALLOW_WRITES="1")
    result = subprocess.run([sys.executable, "-c", probe], env=env,
                            capture_output=True, text=True, timeout=120)
    check("file read blocked even with ALLOW_WRITES=1",
          result.stdout.strip().endswith("SAFE"), (result.stdout + result.stderr).strip()[-80:])

    print(f"\n{'=' * 60}\nPASS {len(P)}  FAIL {len(F)}")
    for f in F:
        print("  FAILED:", f)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
