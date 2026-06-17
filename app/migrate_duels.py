"""
One-off migration: add the `duels` table to an already-running league DB.

Run once, against the live database:

    docker compose run --rm worldcup_bot python -m app.migrate_duels

It executes a single CREATE TABLE IF NOT EXISTS, so it's idempotent — running
it a second time is a no-op. Fresh installs don't need this; the same table is
included in sql/init.sql, which the MySQL image auto-runs on first boot of a
clean volume.

The DDL is embedded below (NOT read from sql/duels.sql) on purpose: the bot's
Docker image only copies app/, so sql/ isn't available at runtime inside the
container. Keep this in sync with sql/duels.sql and the duels block in
sql/init.sql.

Reads DB connection settings from the same env vars as app/db.py
(DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME).
"""

from __future__ import annotations

import logging

from dotenv import load_dotenv

from app import db

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("migrate_duels")

# Keep in sync with sql/duels.sql and the duels block in sql/init.sql.
DUELS_DDL = """
CREATE TABLE IF NOT EXISTS duels (
  id                    INT AUTO_INCREMENT PRIMARY KEY,
  league_chat_id        BIGINT NOT NULL,

  challenger_player_id  INT NOT NULL,
  challenger_country    VARCHAR(64) NOT NULL,
  opponent_player_id    INT NOT NULL,
  opponent_country      VARCHAR(64) NULL,

  mode                  ENUM('hangman', 'trivia') NOT NULL,
  status                ENUM('pending', 'active', 'complete',
                             'cancelled', 'voided') NOT NULL DEFAULT 'pending',

  winner_player_id      INT NULL,
  state                 TEXT NULL,

  open_chat_id          BIGINT
                        GENERATED ALWAYS AS (
                          CASE WHEN status IN ('pending', 'active')
                               THEN league_chat_id ELSE NULL END
                        ) STORED,

  created_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                        ON UPDATE CURRENT_TIMESTAMP,

  -- NOTE: no FK on league_chat_id. InnoDB forbids a cascading foreign key on a
  -- column that is the base of an INDEXED stored generated column, and
  -- open_chat_id (UNIQUE) is generated from league_chat_id. Deleting a league
  -- still cleans up its duels transitively: leagues -> players (CASCADE) ->
  -- duels (CASCADE via the player FKs below).
  FOREIGN KEY (challenger_player_id)
    REFERENCES players(id)
    ON DELETE CASCADE,
  FOREIGN KEY (opponent_player_id)
    REFERENCES players(id)
    ON DELETE CASCADE,

  UNIQUE INDEX uniq_open_duel (open_chat_id),
  INDEX idx_league_status (league_chat_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
""".strip()


def main() -> None:
    load_dotenv()
    with db.connect() as conn, conn.cursor() as cur:
        log.info("Creating `duels` table if it doesn't already exist...")
        cur.execute(DUELS_DDL)
    log.info("Migration complete — `duels` table is ready.")


if __name__ == "__main__":
    main()
