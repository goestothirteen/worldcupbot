"""
Database access layer. Thin wrapper over pymysql.

All bot/cron code talks to MySQL through this module. Each helper opens a fresh
connection, does its thing, and closes it — connection pooling isn't worth the
complexity for ~5 people clicking buttons in a Telegram group.

The Telegram bot is async (python-telegram-bot v21+) and pymysql is sync. Call
these helpers via `asyncio.to_thread(db.helper, ...)` from async handlers so the
event loop doesn't block on the DB.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator, Optional

import pymysql
import pymysql.cursors

log = logging.getLogger(__name__)


def _conn_kwargs() -> dict[str, Any]:
    return dict(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


@contextmanager
def connect() -> Iterator[pymysql.connections.Connection]:
    """Context manager — commit on success, rollback on exception, always close."""
    conn = pymysql.connect(**_conn_kwargs())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Leagues ────────────────────────────────────────────────────────────────

def get_league(chat_id: int) -> Optional[dict]:
    with connect() as c, c.cursor() as cur:
        cur.execute("SELECT * FROM leagues WHERE chat_id = %s", (chat_id,))
        return cur.fetchone()


def create_league(chat_id: int, chat_title: Optional[str], admin_user_id: int, players_expected: int) -> None:
    with connect() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO leagues (chat_id, chat_title, admin_user_id, players_expected, status)
            VALUES (%s, %s, %s, %s, 'signup')
            """,
            (chat_id, chat_title, admin_user_id, players_expected),
        )


def set_league_status(chat_id: int, status: str) -> None:
    with connect() as c, c.cursor() as cur:
        cur.execute("UPDATE leagues SET status = %s WHERE chat_id = %s", (status, chat_id))


def set_draft_started(chat_id: int) -> None:
    with connect() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE leagues SET status = 'drafting', draft_started_at = NOW() WHERE chat_id = %s",
            (chat_id,),
        )


# ── Players ────────────────────────────────────────────────────────────────

def add_player(chat_id: int, telegram_user_id: int, username: Optional[str], display_name: str) -> bool:
    """Returns True if inserted, False if player was already in the league."""
    with connect() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT IGNORE INTO players (league_chat_id, telegram_user_id, username, display_name)
            VALUES (%s, %s, %s, %s)
            """,
            (chat_id, telegram_user_id, username, display_name),
        )
        return cur.rowcount > 0


def list_players(chat_id: int, order_by: str = "joined_at") -> list[dict]:
    # order_by is whitelisted, NOT taken from user input — only called internally.
    assert order_by in ("joined_at", "draft_order", "display_name")
    with connect() as c, c.cursor() as cur:
        cur.execute(
            f"SELECT * FROM players WHERE league_chat_id = %s ORDER BY {order_by} ASC",
            (chat_id,),
        )
        return list(cur.fetchall())


def get_player_by_user(chat_id: int, telegram_user_id: int) -> Optional[dict]:
    with connect() as c, c.cursor() as cur:
        cur.execute(
            "SELECT * FROM players WHERE league_chat_id = %s AND telegram_user_id = %s",
            (chat_id, telegram_user_id),
        )
        return cur.fetchone()


def assign_draft_order(chat_id: int, order: list[int]) -> None:
    """`order` is a list of player IDs in draft position 1..N."""
    with connect() as c, c.cursor() as cur:
        for position, player_id in enumerate(order, start=1):
            cur.execute(
                "UPDATE players SET draft_order = %s WHERE id = %s AND league_chat_id = %s",
                (position, player_id, chat_id),
            )


# ── Picks ──────────────────────────────────────────────────────────────────

def list_picks(chat_id: int) -> list[dict]:
    with connect() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT p.*, pl.display_name, pl.telegram_user_id
            FROM picks p
            JOIN players pl ON pl.id = p.player_id
            WHERE p.league_chat_id = %s
            ORDER BY p.pick_number ASC
            """,
            (chat_id,),
        )
        return list(cur.fetchall())


def picks_for_player(chat_id: int, player_id: int) -> list[dict]:
    with connect() as c, c.cursor() as cur:
        cur.execute(
            "SELECT * FROM picks WHERE league_chat_id = %s AND player_id = %s ORDER BY pick_number ASC",
            (chat_id, player_id),
        )
        return list(cur.fetchall())


def owner_of_country(chat_id: int, country_code: str) -> Optional[dict]:
    """Returns the player dict who drafted this country, or None."""
    with connect() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT pl.* FROM picks p
            JOIN players pl ON pl.id = p.player_id
            WHERE p.league_chat_id = %s AND p.country_code = %s
            """,
            (chat_id, country_code),
        )
        return cur.fetchone()


def make_pick(chat_id: int, player_id: int, country_code: str, pick_number: int) -> bool:
    """
    Insert a pick. Returns False if the country is already taken (UNIQUE violation)
    — race-safe because the DB enforces uniq_league_country.
    """
    try:
        with connect() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO picks (league_chat_id, player_id, country_code, pick_number)
                VALUES (%s, %s, %s, %s)
                """,
                (chat_id, player_id, country_code, pick_number),
            )
        return True
    except pymysql.err.IntegrityError as e:
        log.info("Pick rejected by DB integrity check: %s", e)
        return False


def next_pick_number(chat_id: int) -> int:
    with connect() as c, c.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM picks WHERE league_chat_id = %s", (chat_id,))
        row = cur.fetchone()
        return (row["n"] if row else 0) + 1


def undo_last_pick(chat_id: int) -> Optional[dict]:
    """Remove the highest-numbered pick. Returns the deleted row or None."""
    with connect() as c, c.cursor() as cur:
        cur.execute(
            "SELECT * FROM picks WHERE league_chat_id = %s ORDER BY pick_number DESC LIMIT 1",
            (chat_id,),
        )
        last = cur.fetchone()
        if not last:
            return None
        cur.execute("DELETE FROM picks WHERE id = %s", (last["id"],))
        return last


# ── Matches ────────────────────────────────────────────────────────────────

def upsert_match(
    ext_match_id: Optional[int],
    stage: str,
    group_name: Optional[str],
    home_country: str,
    away_country: str,
    kickoff_at,
    home_score: Optional[int],
    away_score: Optional[int],
    status: str,
) -> int:
    """
    Insert or update by ext_match_id (when set). Returns the match id.
    Sets finished_at when status flips to 'finished' for the first time.
    """
    with connect() as c, c.cursor() as cur:
        if ext_match_id is not None:
            cur.execute("SELECT id, status FROM matches WHERE ext_match_id = %s", (ext_match_id,))
            row = cur.fetchone()
        else:
            row = None

        if row:
            became_finished = row["status"] != "finished" and status == "finished"
            cur.execute(
                """
                UPDATE matches
                SET stage=%s, group_name=%s, home_country=%s, away_country=%s,
                    kickoff_at=%s, home_score=%s, away_score=%s, status=%s,
                    finished_at = CASE WHEN %s THEN NOW() ELSE finished_at END
                WHERE id=%s
                """,
                (stage, group_name, home_country, away_country, kickoff_at,
                 home_score, away_score, status, became_finished, row["id"]),
            )
            return row["id"]
        else:
            cur.execute(
                """
                INSERT INTO matches
                  (ext_match_id, stage, group_name, home_country, away_country,
                   kickoff_at, home_score, away_score, status, finished_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, CASE WHEN %s = 'finished' THEN NOW() ELSE NULL END)
                """,
                (ext_match_id, stage, group_name, home_country, away_country,
                 kickoff_at, home_score, away_score, status, status),
            )
            return cur.lastrowid


def finished_matches() -> list[dict]:
    with connect() as c, c.cursor() as cur:
        cur.execute("SELECT * FROM matches WHERE status = 'finished' ORDER BY finished_at ASC")
        return list(cur.fetchall())


def last_match() -> Optional[dict]:
    """Most recently inserted match (any league). For /undo_result."""
    with connect() as c, c.cursor() as cur:
        cur.execute("SELECT * FROM matches ORDER BY id DESC LIMIT 1")
        return cur.fetchone()


def delete_match_and_events(match_id: int) -> int:
    """
    Delete a match AND every point_event that referenced it (across all leagues).
    We need the explicit delete on point_events because the FK uses
    ON DELETE SET NULL — orphaned events would otherwise keep counting toward
    standings. Returns the number of point_events removed.
    """
    with connect() as c, c.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM point_events WHERE match_id = %s", (match_id,))
        deleted_events = cur.fetchone()["n"]
        cur.execute("DELETE FROM point_events WHERE match_id = %s", (match_id,))
        cur.execute("DELETE FROM matches WHERE id = %s", (match_id,))
        return int(deleted_events)


def matches_by_stage(stage: str, status: Optional[str] = None) -> list[dict]:
    with connect() as c, c.cursor() as cur:
        if status:
            cur.execute(
                "SELECT * FROM matches WHERE stage=%s AND status=%s ORDER BY kickoff_at",
                (stage, status),
            )
        else:
            cur.execute("SELECT * FROM matches WHERE stage=%s ORDER BY kickoff_at", (stage,))
        return list(cur.fetchall())


# ── Point events ───────────────────────────────────────────────────────────

def has_event(chat_id: int, country_code: str, event_type: str, match_id: Optional[int] = None) -> bool:
    """
    Check whether this event has already been recorded. For one-time events
    (reach_* / champion) pass match_id=None to look up by (league, country, event_type).
    For group_win, pass the match_id to allow multiple events of the same type
    across different matches.
    """
    with connect() as c, c.cursor() as cur:
        if match_id is None:
            cur.execute(
                """
                SELECT 1 FROM point_events
                WHERE league_chat_id=%s AND country_code=%s AND event_type=%s LIMIT 1
                """,
                (chat_id, country_code, event_type),
            )
        else:
            cur.execute(
                """
                SELECT 1 FROM point_events
                WHERE league_chat_id=%s AND country_code=%s AND event_type=%s AND match_id=%s LIMIT 1
                """,
                (chat_id, country_code, event_type, match_id),
            )
        return cur.fetchone() is not None


def add_event(chat_id: int, player_id: int, country_code: str, event_type: str, points: int, match_id: Optional[int]) -> None:
    with connect() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO point_events
              (league_chat_id, player_id, country_code, event_type, points, match_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (chat_id, player_id, country_code, event_type, points, match_id),
        )


def standings(chat_id: int) -> list[dict]:
    """Players ordered by total points DESC. Includes 0-point players too.

    NOTE: do NOT combine point_events + picks into a single LEFT JOIN with
    aggregates. They are unrelated rowsets (a player has many picks AND many
    events independently), so a join multiplies them, inflating SUM(points)
    by num_picks and COUNT(picks) by num_events. Use correlated subqueries
    instead — they evaluate independently and aggregate cleanly.
    """
    with connect() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT pl.id, pl.display_name, pl.telegram_user_id,
                   COALESCE(
                     (SELECT SUM(ev.points) FROM point_events ev
                      WHERE ev.player_id = pl.id
                        AND ev.league_chat_id = pl.league_chat_id),
                     0
                   ) AS total_points,
                   (SELECT COUNT(*) FROM picks pk
                    WHERE pk.player_id = pl.id
                      AND pk.league_chat_id = pl.league_chat_id) AS num_countries
            FROM players pl
            WHERE pl.league_chat_id = %s
            ORDER BY total_points DESC, pl.display_name ASC
            """,
            (chat_id,),
        )
        return list(cur.fetchall())


def standings_detailed(chat_id: int) -> list[dict]:
    """
    Like standings(), but each player dict also has a `teams` field —
    a list of {country_code, points} sorted by points DESC.
    One query for the whole league; we shape it in Python.

    Returned shape (sorted by total_points DESC, display_name ASC):
      [
        {
          id, display_name, telegram_user_id, total_points,
          teams: [{country_code, points}, ...]
        },
        ...
      ]
    Players with no picks are included with teams=[] and total_points=0.
    """
    with connect() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT pl.id          AS player_id,
                   pl.display_name,
                   pl.telegram_user_id,
                   pk.country_code,
                   pk.pick_number,
                   COALESCE(
                     (SELECT SUM(ev.points) FROM point_events ev
                      WHERE ev.player_id     = pl.id
                        AND ev.country_code  = pk.country_code
                        AND ev.league_chat_id = pl.league_chat_id),
                     0
                   ) AS country_points
            FROM players pl
            LEFT JOIN picks pk
                   ON pk.player_id = pl.id
                  AND pk.league_chat_id = pl.league_chat_id
            WHERE pl.league_chat_id = %s
            ORDER BY pl.id, pk.pick_number
            """,
            (chat_id,),
        )
        rows = list(cur.fetchall())

    # Fold the (player, country) rows into one dict per player.
    by_player: dict[int, dict] = {}
    for r in rows:
        p = by_player.setdefault(r["player_id"], {
            "id": r["player_id"],
            "display_name": r["display_name"],
            "telegram_user_id": r["telegram_user_id"],
            "teams": [],
            "total_points": 0,
        })
        # LEFT JOIN gives a NULL country row for players with zero picks.
        if r["country_code"] is None:
            continue
        pts = int(r["country_points"])
        p["teams"].append({"country_code": r["country_code"], "points": pts})
        p["total_points"] += pts

    # Per-player: best-performing teams first (ties broken by country_code).
    for p in by_player.values():
        p["teams"].sort(key=lambda t: (-t["points"], t["country_code"]))

    # Overall: leaderboard order, ties broken by display_name.
    return sorted(
        by_player.values(),
        key=lambda p: (-p["total_points"], p["display_name"]),
    )


def points_for_country(chat_id: int, country_code: str) -> int:
    with connect() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(SUM(points), 0) AS pts FROM point_events
            WHERE league_chat_id = %s AND country_code = %s
            """,
            (chat_id, country_code),
        )
        row = cur.fetchone()
        return int(row["pts"]) if row else 0


def all_leagues() -> list[dict]:
    """All currently-running leagues — used when applying a match result across chats."""
    with connect() as c, c.cursor() as cur:
        cur.execute("SELECT * FROM leagues WHERE status IN ('active', 'drafting')")
        return list(cur.fetchall())
