-- ============================================================
-- World Cup Draft Bot — schema
-- ============================================================
-- This file is auto-run by the official MySQL Docker image on
-- FIRST start of the worldcup_db container, because docker-compose.yml
-- mounts it at /docker-entrypoint-initdb.d/init.sql.
--
-- After first run, this file is ignored — the volume already has
-- the schema. To change the schema later, write a migration script
-- in Python rather than editing this file (or `docker compose down -v`
-- to wipe the DB and start fresh — only safe before the draft begins).
-- ============================================================

USE worldcup;


-- ============================================================
-- TABLE: leagues
-- One row per Telegram chat that's running a World Cup draft.
-- The bot supports multiple group chats simultaneously, each with
-- their own independent draft + standings (keyed by chat_id).
-- ============================================================
CREATE TABLE IF NOT EXISTS leagues (
  chat_id           BIGINT PRIMARY KEY,
  chat_title        VARCHAR(255) NULL,

  -- Lifecycle: signup → drafting → active → complete
  --   signup    — players running /join
  --   drafting  — snake draft in progress; /pick is open
  --   active    — draft done, matches happening, points accruing
  --   complete  — final whistle blown, no more updates
  status            ENUM('signup', 'drafting', 'active', 'complete')
                    NOT NULL DEFAULT 'signup',

  -- Telegram user_id of whoever ran /start_league. Has admin rights
  -- (alongside anyone listed in ADMIN_USER_IDS env var).
  admin_user_id     BIGINT NOT NULL,

  -- How many players this league expects. The draft can only start
  -- once exactly this many players have joined. Read from PLAYERS_PER_LEAGUE
  -- env var at /start_league time and frozen into this row.
  players_expected  TINYINT NOT NULL,

  -- Set when /start_draft runs.
  draft_started_at  DATETIME NULL,

  created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- TABLE: players
-- One row per (league, person who ran /join).
-- ============================================================
CREATE TABLE IF NOT EXISTS players (
  id                INT AUTO_INCREMENT PRIMARY KEY,
  league_chat_id    BIGINT NOT NULL,

  -- Telegram identity. user_id is the stable numeric ID; username/display_name
  -- are convenience copies (Telegram lets people change those, user_id never changes).
  telegram_user_id  BIGINT NOT NULL,
  username          VARCHAR(64) NULL,     -- @handle, NULL if user has none set
  display_name      VARCHAR(128) NOT NULL,

  -- Set by /start_draft. NULL during signup. 1-indexed in randomized draft order.
  -- Snake draft uses this: round 1 picks in order 1..N, round 2 in N..1, etc.
  draft_order       TINYINT NULL,

  joined_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

  FOREIGN KEY (league_chat_id)
    REFERENCES leagues(chat_id)
    ON DELETE CASCADE,

  -- One row per person per league — protects against double-/join.
  UNIQUE INDEX uniq_league_user (league_chat_id, telegram_user_id),
  INDEX idx_league_order (league_chat_id, draft_order)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- TABLE: picks
-- One row per drafted (league, country). Append-only during the draft.
-- The next pick number for a league = (SELECT COUNT(*) FROM picks WHERE league_chat_id = ?) + 1
-- so we don't store a separate draft-cursor — picks ARE the cursor.
-- ============================================================
CREATE TABLE IF NOT EXISTS picks (
  id                INT AUTO_INCREMENT PRIMARY KEY,
  league_chat_id    BIGINT NOT NULL,
  player_id         INT NOT NULL,

  -- Lowercase canonical country code from app/countries.py.
  -- e.g. "brazil", "korea_republic", "ivory_coast"
  country_code      VARCHAR(64) NOT NULL,

  -- 1-indexed overall pick number within this league.
  pick_number       SMALLINT NOT NULL,

  picked_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

  FOREIGN KEY (league_chat_id)
    REFERENCES leagues(chat_id)
    ON DELETE CASCADE,
  FOREIGN KEY (player_id)
    REFERENCES players(id)
    ON DELETE CASCADE,

  -- A country can only be drafted ONCE per league. This enforces no-duplicates
  -- at the DB level, defending against race conditions if two /pick commands
  -- somehow arrive at the same millisecond.
  UNIQUE INDEX uniq_league_country (league_chat_id, country_code),

  -- For "show me all picks in pick order" — the /order command.
  INDEX idx_league_pick_number (league_chat_id, pick_number)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- TABLE: matches
-- Global table — one row per World Cup match. Shared across all leagues
-- in this DB instance (typically just one league, but the schema doesn't
-- assume that). Populated by app/fetch_results.py (cron) or by admin
-- /set_result commands.
-- ============================================================
CREATE TABLE IF NOT EXISTS matches (
  id                INT AUTO_INCREMENT PRIMARY KEY,

  -- football-data.org's match ID — lets us upsert on re-fetch.
  -- NULL for matches added manually before they're in the API.
  ext_match_id      INT NULL,

  -- Stage of the tournament:
  --   group, round_of_32, round_of_16, quarter_final, semi_final,
  --   third_place, final
  stage             ENUM('group', 'round_of_32', 'round_of_16',
                         'quarter_final', 'semi_final', 'third_place',
                         'final') NOT NULL,

  -- Group letter A-L for group-stage matches; NULL otherwise.
  group_name        CHAR(1) NULL,

  -- Lowercase canonical country codes from app/countries.py.
  home_country      VARCHAR(64) NOT NULL,
  away_country      VARCHAR(64) NOT NULL,

  kickoff_at        DATETIME NULL,

  -- NULL until the match finishes. fetch_results.py only updates these
  -- when the match status is FINISHED (i.e. no in-progress scores).
  home_score        TINYINT NULL,
  away_score        TINYINT NULL,

  --   scheduled, in_progress, finished
  status            ENUM('scheduled', 'in_progress', 'finished')
                    NOT NULL DEFAULT 'scheduled',
  finished_at       DATETIME NULL,

  created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,

  UNIQUE INDEX uniq_ext_match (ext_match_id),
  INDEX idx_kickoff (kickoff_at),
  INDEX idx_stage_status (stage, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ============================================================
-- TABLE: point_events
-- One row per points-awarding event. The leaderboard is computed as
--   SUM(points) GROUP BY player_id
-- so we never have to store a separate "current_score" column.
--
-- Event types (defined in app/scoring.py):
--   group_win          — +1 per group-stage win (one row per win, match-scoped)
--   reach_round_of_32  — +2 (one-time per country)
--   reach_round_of_16  — +3 (one-time per country)
--   reach_quarter      — +5 (one-time per country)
--   reach_semi         — +8 (one-time per country)
--   reach_final        — +12 (one-time per country)
--   champion           — +20 (one-time per country)
-- ============================================================
CREATE TABLE IF NOT EXISTS point_events (
  id                INT AUTO_INCREMENT PRIMARY KEY,
  league_chat_id    BIGINT NOT NULL,
  player_id         INT NOT NULL,
  country_code      VARCHAR(64) NOT NULL,

  event_type        VARCHAR(32) NOT NULL,
  -- DECIMAL(4,1) supports fractional points (half-point goal-diff scoring).
  -- Range -999.9..999.9 — way more than any plausible tournament total.
  points            DECIMAL(4,1) NOT NULL,

  -- For group_win events, the specific match that triggered it.
  -- For one-time stage events, the first match in that stage that the
  -- country played (or NULL if the event was set by admin override).
  match_id          INT NULL,

  awarded_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

  FOREIGN KEY (league_chat_id)
    REFERENCES leagues(chat_id)
    ON DELETE CASCADE,
  FOREIGN KEY (player_id)
    REFERENCES players(id)
    ON DELETE CASCADE,
  FOREIGN KEY (match_id)
    REFERENCES matches(id)
    ON DELETE SET NULL,

  -- Idempotency:
  --   * one-time stage events (reach_*, champion) — UNIQUE on (league, country, event_type)
  --   * group_win events — UNIQUE on (league, country, event_type, match_id)
  -- We model both with a single composite UNIQUE that includes match_id.
  -- For one-time events match_id is set to a sentinel (-1 if no match, or the
  -- triggering match's id) but the rule is enforced in code (scoring.py only
  -- inserts one-time events once). MySQL UNIQUE treats NULLs as distinct, so
  -- we use the (league, country, event_type, COALESCE(match_id, 0)) pattern
  -- below via a generated column trick — or simpler, we rely on app logic.
  --
  -- Keeping the constraint loose at DB level (just an index for speed) and
  -- letting app/scoring.py enforce uniqueness with SELECT-then-INSERT inside
  -- a transaction. This is fine because the cron is single-instance.
  INDEX idx_league_player (league_chat_id, player_id),
  INDEX idx_league_country_event (league_chat_id, country_code, event_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
