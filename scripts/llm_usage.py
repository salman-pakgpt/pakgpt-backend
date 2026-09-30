import csv
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from main import DB_PATH  # noqa: E402

REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"

QUERIES = {
    "by_session": (
        """
        SELECT
            session_id,
            COUNT(*) AS calls,
            SUM(input_tokens) AS input_tokens,
            SUM(output_tokens) AS output_tokens,
            SUM(reasoning_tokens) AS reasoning_tokens,
            SUM(cost_usd) AS cost_usd,
            AVG(latency_ms) AS avg_latency_ms,
            MIN(created_at) AS first_call_at,
            MAX(created_at) AS last_call_at
        FROM llm_calls
        WHERE is_test = 0
        GROUP BY session_id
        ORDER BY cost_usd DESC
        """,
        ["session_id", "calls", "input_tokens", "output_tokens", "reasoning_tokens",
         "cost_usd", "avg_latency_ms", "first_call_at", "last_call_at"],
    ),
    "by_model": (
        """
        SELECT
            model,
            call_type,
            COUNT(*) AS calls,
            SUM(input_tokens) AS input_tokens,
            SUM(output_tokens) AS output_tokens,
            SUM(reasoning_tokens) AS reasoning_tokens,
            SUM(cost_usd) AS cost_usd,
            AVG(latency_ms) AS avg_latency_ms
        FROM llm_calls
        WHERE is_test = 0
        GROUP BY model, call_type
        ORDER BY cost_usd DESC
        """,
        ["model", "call_type", "calls", "input_tokens", "output_tokens", "reasoning_tokens",
         "cost_usd", "avg_latency_ms"],
    ),
    "by_day": (
        """
        SELECT
            date(created_at) AS day,
            COUNT(*) AS calls,
            SUM(input_tokens) AS input_tokens,
            SUM(output_tokens) AS output_tokens,
            SUM(reasoning_tokens) AS reasoning_tokens,
            SUM(cost_usd) AS cost_usd,
            AVG(latency_ms) AS avg_latency_ms
        FROM llm_calls
        WHERE is_test = 0
        GROUP BY day
        ORDER BY day
        """,
        ["day", "calls", "input_tokens", "output_tokens", "reasoning_tokens",
         "cost_usd", "avg_latency_ms"],
    ),
}


def write_report(conn: sqlite3.Connection, name: str, sql: str, columns: list[str]) -> int:
    rows = conn.execute(sql).fetchall()
    path = REPORTS_DIR / f"llm_usage_{name}.csv"
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        writer.writerows(rows)
    return len(rows)


def main() -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    for name, (sql, columns) in QUERIES.items():
        count = write_report(conn, name, sql, columns)
        print(f"reports/llm_usage_{name}.csv - {count} rows")
    conn.close()


if __name__ == "__main__":
    main()
