import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from main import init_db, DB_PATH  # noqa: E402


def migrate() -> None:
    conn = sqlite3.connect(DB_PATH)
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}

    if "sessions" not in tables:
        print("No legacy 'sessions' table found - nothing to migrate.")
        conn.close()
        return

    init_db()

    sessions = conn.execute(
        "SELECT session_id, messages, updated_at FROM sessions"
    ).fetchall()

    inserted = 0
    for session_id, messages_json, updated_at in sessions:
        for msg in json.loads(messages_json):
            conn.execute(
                "INSERT INTO messages (session_id, role, content, created_at) "
                "VALUES (?, ?, ?, ?)",
                (session_id, msg["role"], msg["content"], updated_at),
            )
            inserted += 1

    conn.execute("ALTER TABLE sessions RENAME TO sessions_legacy")
    conn.commit()
    conn.close()
    print(f"Migrated {len(sessions)} sessions ({inserted} messages). "
          f"Old table renamed to 'sessions_legacy'.")


if __name__ == "__main__":
    migrate()
