"""
One-off migration: add the `duels` table to an already-running league DB.

Run once, from the project root, against the live database:

    python -m app.migrate_duels

It executes sql/duels.sql (CREATE TABLE IF NOT EXISTS), so it's idempotent —
running it a second time is a no-op. Fresh installs don't need this; the same
table is included in sql/init.sql, which the MySQL image auto-runs on first
boot of a clean volume.

Reads DB connection settings from the same env vars as app/db.py
(DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME).
"""

from __future__ import annotations

import logging
from pathlib import Path

from dotenv import load_dotenv

from app import db

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("migrate_duels")

SQL_PATH = Path(__file__).resolve().parent.parent / "sql" / "duels.sql"


def _statements(sql_text: str) -> list[str]:
    """Split a .sql file into individual statements, dropping comments and the
    `USE worldcup;` line (the connection already selects the DB)."""
    cleaned_lines = []
    for line in sql_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("--") or not stripped:
            continue
        if stripped.upper().startswith("USE "):
            continue
        cleaned_lines.append(line)
    body = "\n".join(cleaned_lines)
    return [s.strip() for s in body.split(";") if s.strip()]


def main() -> None:
    load_dotenv()
    sql_text = SQL_PATH.read_text(encoding="utf-8")
    statements = _statements(sql_text)
    if not statements:
        log.warning("No statements found in %s", SQL_PATH)
        return
    with db.connect() as conn, conn.cursor() as cur:
        for stmt in statements:
            log.info("Executing: %s ...", stmt.splitlines()[0][:60])
            cur.execute(stmt)
    log.info("Migration complete — `duels` table is ready.")


if __name__ == "__main__":
    main()
