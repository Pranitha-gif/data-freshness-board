"""
data-freshness-board
--------------------
A simple BI view showing how up to date each table is versus its SLA.

How it works
  1. A config table (freshness_config) lists each table, its layer, the
     timestamp column to check, and the SLA in hours.
  2. For every table we read MAX(timestamp) and the row count.
  3. Age is compared to the SLA to give a status:
        FRESH   age <= SLA
        WARNING age <= 1.5 x SLA
        STALE   age >  1.5 x SLA
        EMPTY   table has no rows
  4. Each run is logged to freshness_log and an HTML board is generated.

Usage
  python freshness_board.py --demo      # build sample data, then run checks
  python freshness_board.py             # run checks on an existing database
  python freshness_board.py --db my.db --out board.html

Only the Python standard library is needed (3.9+).
"""

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone
from html import escape

DEFAULT_DB = "freshness.db"
DEFAULT_OUT = "freshness_board.html"
WARNING_FACTOR = 1.5
TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def quote_ident(name: str) -> str:
    """Safely quote a SQL identifier."""
    return '"' + name.replace('"', '""') + '"'


# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------
def init_meta_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS freshness_config (
            table_name  TEXT PRIMARY KEY,
            layer       TEXT NOT NULL,
            ts_column   TEXT NOT NULL,
            sla_hours   REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS freshness_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at    TEXT NOT NULL,
            table_name    TEXT NOT NULL,
            layer         TEXT NOT NULL,
            last_updated  TEXT,
            age_hours     REAL,
            sla_hours     REAL NOT NULL,
            row_count     INTEGER,
            status        TEXT NOT NULL
        );
        """
    )
    conn.commit()


def seed_demo_data(conn: sqlite3.Connection) -> None:
    """Create sample Bronze/Silver/Gold tables with different ages."""
    now = now_utc()

    # (table, layer, ts_column, sla_hours, hours_since_last_update, rows)
    demo = [
        ("bronze_orders", "bronze", "ingested_at", 2, 0.5, 120),
        ("bronze_customers", "bronze", "ingested_at", 24, 20, 80),
        ("silver_orders", "silver", "updated_at", 6, 8, 110),
        ("silver_customers", "silver", "updated_at", 24, 60, 75),
        ("gold_sales_summary", "gold", "refreshed_at", 12, 3, 30),
        ("gold_customer_ltv", "gold", "refreshed_at", 24, None, 0),
    ]

    conn.execute("DELETE FROM freshness_config")
    for table, layer, ts_col, sla, age_h, rows in demo:
        conn.execute(f"DROP TABLE IF EXISTS {quote_ident(table)}")
        conn.execute(
            f"CREATE TABLE {quote_ident(table)} "
            f"(id INTEGER PRIMARY KEY, {quote_ident(ts_col)} TEXT)"
        )
        if age_h is not None:
            last = now - timedelta(hours=age_h)
            for i in range(rows):
                # spread older rows a bit; the newest row is exactly `last`
                ts = last - timedelta(minutes=i)
                conn.execute(
                    f"INSERT INTO {quote_ident(table)} ({quote_ident(ts_col)}) VALUES (?)",
                    (ts.strftime(TS_FORMAT),),
                )
        conn.execute(
            "INSERT INTO freshness_config VALUES (?, ?, ?, ?)",
            (table, layer, ts_col, sla),
        )
    conn.commit()


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------
def classify(age_hours, sla_hours: float, row_count: int) -> str:
    if row_count == 0 or age_hours is None:
        return "EMPTY"
    if age_hours <= sla_hours:
        return "FRESH"
    if age_hours <= sla_hours * WARNING_FACTOR:
        return "WARNING"
    return "STALE"


def check_table(conn, table, layer, ts_col, sla_hours, checked_at: datetime) -> dict:
    result = {
        "table_name": table,
        "layer": layer,
        "sla_hours": sla_hours,
        "last_updated": None,
        "age_hours": None,
        "row_count": None,
        "status": "ERROR",
        "error": None,
    }
    try:
        row = conn.execute(
            f"SELECT MAX({quote_ident(ts_col)}), COUNT(*) FROM {quote_ident(table)}"
        ).fetchone()
        last_str, count = row[0], row[1]
        result["row_count"] = count
        if last_str:
            last_dt = datetime.strptime(last_str, TS_FORMAT).replace(tzinfo=timezone.utc)
            result["last_updated"] = last_str
            result["age_hours"] = round((checked_at - last_dt).total_seconds() / 3600, 2)
        result["status"] = classify(result["age_hours"], sla_hours, count)
    except (sqlite3.Error, ValueError) as exc:
        result["error"] = str(exc)
    return result


def run_checks(conn: sqlite3.Connection) -> list:
    checked_at = now_utc()
    configs = conn.execute(
        "SELECT table_name, layer, ts_column, sla_hours FROM freshness_config "
        "ORDER BY CASE layer WHEN 'bronze' THEN 1 WHEN 'silver' THEN 2 "
        "WHEN 'gold' THEN 3 ELSE 4 END, table_name"
    ).fetchall()

    results = []
    for table, layer, ts_col, sla in configs:
        res = check_table(conn, table, layer, ts_col, sla, checked_at)
        results.append(res)
        conn.execute(
            "INSERT INTO freshness_log "
            "(checked_at, table_name, layer, last_updated, age_hours, sla_hours, row_count, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                checked_at.strftime(TS_FORMAT),
                res["table_name"],
                res["layer"],
                res["last_updated"],
                res["age_hours"],
                res["sla_hours"],
                res["row_count"],
                res["status"],
            ),
        )
    conn.commit()
    return results


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
def print_console(results: list) -> None:
    print(f"{'TABLE':<22}{'LAYER':<8}{'AGE(h)':>8}{'SLA(h)':>8}  STATUS")
    print("-" * 56)
    for r in results:
        age = "-" if r["age_hours"] is None else f"{r['age_hours']:.1f}"
        print(f"{r['table_name']:<22}{r['layer']:<8}{age:>8}{r['sla_hours']:>8.0f}  {r['status']}")


def render_html(results: list, generated_at: str) -> str:
    counts = {s: 0 for s in ("FRESH", "WARNING", "STALE", "EMPTY", "ERROR")}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    cards = "".join(
        f'<div class="card {s.lower()}"><div class="num">{n}</div><div>{s}</div></div>'
        for s, n in counts.items()
    )

    rows = []
    for r in results:
        age = "-" if r["age_hours"] is None else f"{r['age_hours']:.1f}"
        pct = 0 if r["age_hours"] is None else min(100, round(r["age_hours"] / r["sla_hours"] * 100))
        rows.append(
            "<tr>"
            f"<td>{escape(r['table_name'])}</td>"
            f"<td>{escape(r['layer'])}</td>"
            f"<td>{escape(r['last_updated'] or '-')}</td>"
            f"<td>{age}</td>"
            f"<td>{r['sla_hours']:.0f}</td>"
            f"<td><div class='bar'><div class='fill {r['status'].lower()}' style='width:{pct}%'></div></div></td>"
            f"<td>{'-' if r['row_count'] is None else r['row_count']}</td>"
            f"<td><span class='pill {r['status'].lower()}'>{r['status']}</span></td>"
            "</tr>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Data Freshness Board</title>
<style>
  :root {{ --bg:#f6f7f9; --fg:#1b1f24; --card:#fff; --line:#e3e6ea;
          --fresh:#2e9d5b; --warning:#e0a100; --stale:#d64545; --empty:#7a828c; --error:#7a828c; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#14171a; --fg:#e8eaed; --card:#1e2327; --line:#2d343a; }}
  }}
  body {{ font-family: system-ui, sans-serif; background:var(--bg); color:var(--fg); margin:0; padding:24px; }}
  h1 {{ margin:0 0 4px; font-size:22px; }}
  .sub {{ opacity:.7; margin-bottom:20px; font-size:14px; }}
  .cards {{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom:20px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-left:5px solid var(--empty);
          border-radius:8px; padding:12px 18px; min-width:90px; }}
  .card .num {{ font-size:26px; font-weight:600; }}
  .card.fresh {{ border-left-color:var(--fresh); }} .card.warning {{ border-left-color:var(--warning); }}
  .card.stale {{ border-left-color:var(--stale); }}
  .wrap {{ overflow-x:auto; background:var(--card); border:1px solid var(--line); border-radius:8px; }}
  table {{ border-collapse:collapse; width:100%; font-size:14px; }}
  th, td {{ padding:10px 12px; text-align:left; border-bottom:1px solid var(--line); white-space:nowrap; }}
  th {{ font-weight:600; opacity:.75; }}
  .bar {{ width:120px; height:8px; background:var(--line); border-radius:4px; overflow:hidden; }}
  .fill {{ height:100%; background:var(--empty); }}
  .fill.fresh {{ background:var(--fresh); }} .fill.warning {{ background:var(--warning); }}
  .fill.stale {{ background:var(--stale); }}
  .pill {{ padding:2px 10px; border-radius:12px; font-size:12px; font-weight:600; color:#fff; background:var(--empty); }}
  .pill.fresh {{ background:var(--fresh); }} .pill.warning {{ background:var(--warning); }}
  .pill.stale {{ background:var(--stale); }}
</style>
</head>
<body>
  <h1>Data Freshness Board</h1>
  <div class="sub">Generated {escape(generated_at)} UTC. Bar shows age as a share of each table's SLA.</div>
  <div class="cards">{cards}</div>
  <div class="wrap">
    <table>
      <thead><tr>
        <th>Table</th><th>Layer</th><th>Last updated (UTC)</th><th>Age (h)</th>
        <th>SLA (h)</th><th>Age vs SLA</th><th>Rows</th><th>Status</th>
      </tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
  </div>
</body>
</html>
"""


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Data freshness board")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--out", default=DEFAULT_OUT, help="HTML output path")
    parser.add_argument("--demo", action="store_true", help="create sample tables first")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    try:
        init_meta_tables(conn)
        if args.demo:
            seed_demo_data(conn)

        results = run_checks(conn)
        if not results:
            print("No tables configured. Add rows to freshness_config or use --demo.")
            return

        print_console(results)
        html_doc = render_html(results, now_utc().strftime(TS_FORMAT))
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(html_doc)
        print(f"\nBoard written to {args.out}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
