-- ============================================================
-- World Cup Draft Bot — Team Duel schema (additive migration)
-- ============================================================
-- This file is NOT auto-run. init.sql only runs on the FIRST start of a
-- fresh worldcup_db volume, so an already-running league won't have the
-- duels table. Apply this to a live DB with the bundled migration:
--
--     python -m app.migrate_duels
--
-- (which just executes this CREATE TABLE IF NOT EXISTS). It's also copied
-- into init.sql so fresh installs get it automatically. Safe to run twice.
-- ============================================================

USE worldcup;

-- ============================================================
-- TABLE: duels
-- One row per Team Duel. Two players each stake one country they own; a
-- mini-game (hangman or trivia) decides a winner, and the loser's staked
-- country is transferred to the winner (see db.transfer_team).
--
-- Lifecycle (a two-sided handshake before any game starts):
--   pending, opponent_country IS NULL  -> challenged player must /accept_duel
--   pending, opponent_country IS SET   -> challenger must /confirm_duel
--   active                             -> game in progress (/guess or /answer)
--   complete                           -> winner decided, team transferred
--   cancelled                          -> declined / withdrawn before completion
--   voided                             -> admin reversed a completed result
--
-- ONE open (pending OR active) duel per league at a time. This is enforced at
-- the DB level by the generated column `open_chat_id` + its UNIQUE index:
-- the column equals league_chat_id while the duel is open and flips to NULL
-- once it reaches a terminal state (MySQL allows many NULLs in a UNIQUE index,
-- so completed/cancelled duels don't collide). A second concurrent /duel in
-- the same chat therefore fails the INSERT instead of racing past an
-- application-level check.
-- ============================================================
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

  -- = league_chat_id while the duel is open (pending/active), else NULL.
  -- The UNIQUE index below then permits at most one open duel per chat.
  open_chat_id          BIGINT
                        GENERATED ALWAYS AS (
                          CASE WHEN status IN ('pending', 'active')
                               THEN league_chat_id ELSE NULL END
                        ) STORED,

  created_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                        ON UPDATE CURRENT_TIMESTAMP,

  FOREIGN KEY (league_chat_id)
    REFERENCES leagues(chat_id)
    ON DELETE CASCADE,
  FOREIGN KEY (challenger_player_id)
    REFERENCES players(id)
    ON DELETE CASCADE,
  FOREIGN KEY (opponent_player_id)
    REFERENCES players(id)
    ON DELETE CASCADE,

  UNIQUE INDEX uniq_open_duel (open_chat_id),
  INDEX idx_league_status (league_chat_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
