# World Cup Draft Bot

A Telegram bot for running a 2026 FIFA World Cup fantasy draft with a small group of friends. You and your friends are added to a Telegram group, the bot joins, everyone runs `/join`, the bot randomises a snake draft, you take turns picking countries until all 48 are drafted, and then the admin enters match results and stage advancements as the tournament plays out.

Deploys via Docker Compose: one bot container + one MySQL container, that's it.

A one-page rulebook PDF is included (`world_cup_draft_rulebook.pdf`) — share that in the group.

---

## What's in this repo

```
World Cup Bot/
├── app/                              ← all Python lives here (one package)
│   ├── bot.py                        ← Telegram bot, long-polling, all commands
│   ├── db.py                         ← pymysql wrappers (sync; called via to_thread)
│   ├── draft.py                      ← snake draft pure logic (no DB)
│   ├── scoring.py                    ← match-win + stage-advancement scoring
│   └── countries.py                  ← the 48 qualified teams + group draw
├── docker/
│   └── Dockerfile                    ← single image for the bot service
├── sql/
│   └── init.sql                      ← MySQL schema; auto-run on first DB start
├── docker-compose.yml                ← worldcup_bot + worldcup_db
├── requirements.txt                  ← python-telegram-bot, pymysql, python-dotenv
├── .env.example                      ← template for required environment variables
├── world_cup_draft_rulebook.pdf      ← one-page rules + commands reference
└── README.md
```

---

## Format

- **48 teams** in 12 groups (the actual 2026 draw is baked into `app/countries.py`).
- **Snake draft** — players randomly seeded into draft order; pick order reverses each round.
- **5 players × 48 teams** → 9 full rounds + a partial 10th round of 3 picks. Three players end up with 10 countries, two with 9. The snake order naturally compensates for picking late in round 1.
- Number of players is configurable via `PLAYERS_PER_LEAGUE` in `.env` (try 4, 6, 8 or 12 — any of these divides 48 cleanly).

## Scoring

Two independent tracks. Admin enters everything manually.

**1. Match points** (via `/set_result`)

Winner earns goal-differential points; loser earns 0; draws score nothing. Stage of the match doesn't matter — group, R32, R16, QF, SF and final all use the same rule.

| Result | Outcome |
|---|---|
| England 3-0 Ghana | England +3 |
| Spain 2-1 Germany | Spain +1 |
| France 1-1 Italy | nothing |
| Brazil 4-0 Korea | Brazil +4 |

**2. Stage advancement** (via `/set_stage_reached`)

One-time bonus per country, awarded by admin after each round's bracket is settled.

| Stage | Points |
|---|---|
| Reach Round of 32 | +2 |
| Reach Round of 16 | +3 |
| Reach Quarter-final | +5 |
| Reach Semi-final | +8 |
| Reach Final | +12 |
| Champion | +20 |

Max from one country running the table: 50 pts of advancement bonuses, plus whatever they pile up from match wins.

## Commands

Run in your group chat:

| Command | Who | What |
|---|---|---|
| `/start_league` | admin | Create a league for this chat (one per chat) |
| `/join` | anyone | Add yourself to the league |
| `/players` | anyone | List signed-up players |
| `/start_draft` | admin | Randomise draft order, begin drafting |
| `/order` | anyone | Show draft order + whose turn it is |
| `/pick <country>` | on the clock | Draft a country (e.g. `/pick brazil`) |
| `/available` | anyone | Undrafted countries, grouped by FIFA group |
| `/undo_pick` | admin | Undo the most recent pick |
| `/myteam` | anyone | Your countries + per-country points |
| `/team <country>` | anyone | Owner + points for any country |
| `/standings` | anyone | Leaderboard with per-team breakdown |
| `/set_result <home> <h_score> <away> <a_score>` | admin | Record a match — winner earns goal-diff points |
| `/undo_result` | admin | Roll back the most recent `/set_result` |
| `/set_stage_reached <stage> <country> [more...]` | admin | Award stage bonus to one or more countries |
| `/help` | anyone | Full command list |

"Admin" = whoever ran `/start_league` for that chat, OR any user ID listed in `ADMIN_USER_IDS` in `.env`.

Country names accept aliases: `/pick usa`, `/pick korea`, `/pick turkey`, `/pick ivory coast`, `/pick dr congo` all work. Use `/available` if you're unsure.

---

## Setup

### 1. Prerequisites

- Docker + Docker Compose installed on the server (or laptop) you're deploying to.
- A Telegram bot token from [@BotFather](https://t.me/BotFather):
  1. `/newbot` → pick a name + username
  2. **Important:** `/setprivacy` → choose your bot → **Disable**. Otherwise the bot only sees commands like `/pick@your_botname` in groups, which is annoying. Disabling lets `/pick brazil` work directly.
  3. Copy the token (looks like `123456789:ABCdef...`).
- Your Telegram user ID (message [@userinfobot](https://t.me/userinfobot) to get it).

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in:

- `TELEGRAM_BOT_TOKEN` — from BotFather.
- `ADMIN_USER_IDS` — comma-separated user IDs of anyone who should be able to run admin commands (you, at minimum).
- `PLAYERS_PER_LEAGUE` — number of players (default 5).
- `MYSQL_ROOT_PASSWORD` — generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- `DB_PASSWORD` — same generation method.

Never put real secrets in `.env.example` — that file is committed to git. Only `.env` is gitignored.

### 3. Build and start

```bash
docker compose up --build -d
```

Docker will:
- Pull the `mysql:8.0` image (one-time, ~600 MB).
- Create the `worldcup_db_data` persistent volume.
- Run `sql/init.sql` on first start to create the schema.
- Start the bot once MySQL passes its healthcheck.

### 4. Verify it's running

```bash
docker compose ps                          # both services should be Up
docker compose logs -f worldcup_bot        # should show "Bot starting (long polling)..."
```

In Telegram, add the bot to your group chat, give it admin (so it can read messages), and run `/help`.

---

## A typical flow

1. Admin adds the bot to the group, gives it admin permissions.
2. Admin: `/start_league` → bot says "expecting N players, run /join".
3. Everyone: `/join`. Bot announces each signup.
4. Admin: `/start_draft` → bot randomises order, announces pick 1.
5. Players take turns: `/pick spain`, `/pick brazil`, etc. The bot says whose turn is next after every pick.
6. After all 48 are drafted, the league flips to *active*.
7. As matches finish:
   - Admin: `/set_result usa 2 mexico 1` → USA earns +1 (goal diff).
   - After a knockout round is settled, admin: `/set_stage_reached round_of_16 spain england brazil ...` → each of those countries gets the R16 bonus.
8. Anyone: `/standings`, `/myteam`, `/team brazil` whenever they want.
9. Typo? Admin: `/undo_result` rolls back the most recent match (refunding its points), or `/undo_pick` during the draft.

---

## Data persistence

DB lives in a named Docker volume (`worldcup_db_data`) and survives:

- `docker compose up --build -d` (code rebuild)
- `docker compose restart`
- `docker compose down` (without `-v`)
- server reboots

DB is wiped by:

- `docker compose down -v` (`-v` deletes named volumes)
- `docker volume rm worldcupbot_worldcup_db_data`

**Schema changes via `sql/init.sql` only run on a fresh volume.** If you ever need to change the schema after data exists, run an ALTER manually inside the DB container (or wipe + reseed, only safe before any matches are played).

### Backup recommendation (cheap insurance for tournament window)

Add to the host crontab:

```bash
0 3 * * * docker exec worldcupbot-worldcup_db-1 mysqldump -u root -p"$MYSQL_ROOT_PASSWORD" worldcup | gzip > ~/wc_backup_$(date +\%F).sql.gz
0 4 * * * find ~ -maxdepth 1 -name "wc_backup_*.sql.gz" -mtime +30 -delete
```

(Replace container name with what `docker compose ps` shows — Compose adds a project prefix.)

---

## Architecture

```
                    ┌─────────────────────────────────┐
                    │  Telegram group chat            │
                    │  (friends + the bot)            │
                    └────────────────┬────────────────┘
                                     │  long-polling (outbound HTTPS)
                                     ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  worldcup_bot   (python-telegram-bot, asyncio)               │
   │     ├─ /join /pick /standings /myteam /team /etc.            │
   │     ├─ /set_result /set_stage_reached (admin)                │
   │     ├─ Heartbeat job → /app/state/heartbeat (healthcheck)    │
   │     └─ db.py ──┐                                             │
   └────────────────│─────────────────────────────────────────────┘
                    │
                    ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  worldcup_db    (MySQL 8.0 — leagues/players/picks/matches/  │
   │                  point_events)                               │
   └──────────────────────────────────────────────────────────────┘
```

No external network calls (no API fetch, no webhook). The bot connects out to `api.telegram.org` and that's it.

---

## Tweaking after deploy

- **Change scoring** → edit `app/scoring.py` (the `STAGE_REACH_EVENT` dict and `_award_match_win` function), then `docker compose up --build -d worldcup_bot`. Don't change scoring mid-tournament unless everyone agrees.
- **Change number of players** → edit `PLAYERS_PER_LEAGUE` in `.env` before `/start_league`. Once a league is created the count is frozen on that row.
- **Change country list** → edit `app/countries.py`. Stable codes (lowercase) must not be renamed once a draft has used them.
