"""
Telegram bot — command handlers + long-polling main loop.

Run as: `python -m app.bot` (this is the Dockerfile's default CMD).

Architecture:
  * python-telegram-bot v21+ (async). Handlers run in the asyncio event loop.
  * pymysql is sync; all DB calls are wrapped in `asyncio.to_thread(...)` to
    avoid blocking the loop.
  * A JobQueue task touches /app/state/heartbeat every 60s so the docker
    healthcheck (see docker-compose.yml) can detect a silently-stuck poll loop.

Parse mode:
  ALL outgoing messages use ParseMode.HTML, not Markdown. Why: Telegram's
  Markdown treats underscores as italics, and we mention commands like
  /start_draft, /set_result, and event types like reach_round_of_32 all the
  time. With Markdown, each underscore opens an italic marker that often
  never closes → "Can't parse entities" 400 errors. HTML has no such issue.
  Any dynamic content that might contain <, >, & must be passed through
  _e() before being interpolated into a message.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import random
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from app import db, draft, duel, scoring
from app.countries import COUNTRIES, BY_CODE, by_group, resolve

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("worldcup_bot")

HEARTBEAT_PATH = Path("/app/state/heartbeat")


# ── Helpers ────────────────────────────────────────────────────────────────

def _e(s) -> str:
    """HTML-escape a value before interpolating into a parse_mode=HTML message.
    Telegram display names, country names from the DB, etc. can theoretically
    contain <, >, &. Always _e() them."""
    return html.escape(str(s), quote=False)


def _fmt_pts(p) -> str:
    """Render a points value cleanly: 1 -> '1', 1.5 -> '1.5', 1.0 -> '1'.
    Accepts int, float, or Decimal (DB SUM(points) returns Decimal)."""
    if p is None:
        return "0"
    return f"{float(p):g}"


def _admin_user_ids() -> set[int]:
    raw = os.environ.get("ADMIN_USER_IDS", "")
    out = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


def _is_admin_user(user_id: int) -> bool:
    return user_id in _admin_user_ids()


async def _is_league_admin(chat_id: int, user_id: int) -> bool:
    """League admin = global admin OR the person who ran /start_league."""
    if _is_admin_user(user_id):
        return True
    league = await asyncio.to_thread(db.get_league, chat_id)
    return bool(league and league["admin_user_id"] == user_id)


async def _heartbeat_job(_context: ContextTypes.DEFAULT_TYPE) -> None:
    """Touch the heartbeat file so the docker healthcheck stays green."""
    try:
        HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_PATH.touch()
    except Exception as e:
        log.warning("Heartbeat write failed: %s", e)


def _display_country(code: str) -> str:
    """Pretty country render. Country names from countries.py are trusted
    (we wrote them), but pass through _e() defensively in case anyone ever
    adds something with an HTML-special char."""
    c = BY_CODE.get(code)
    if not c:
        return _e(code)
    return f"{c.flag} {_e(c.display_name)}"


def _format_pick_announcement(player_name: str, country_code: str, pick_number: int,
                              num_players: int, next_position: Optional[int],
                              next_player_name: Optional[str]) -> str:
    rd = draft.round_of(pick_number, num_players)
    seat = draft.position_in_round(pick_number, num_players)
    msg = (
        f"✅ Pick {pick_number} (R{rd}.{seat}): <b>{_e(player_name)}</b> "
        f"selects {_display_country(country_code)}\n"
    )
    remaining = draft.picks_remaining(pick_number + 1)
    if next_player_name and remaining > 0:
        next_pick = pick_number + 1
        msg += (
            f"\n👉 On the clock: <b>{_e(next_player_name)}</b> "
            f"(pick {next_pick}, {remaining} left)"
        )
    elif remaining == 0:
        msg += "\n🎉 <b>Draft complete!</b> All 48 teams have been selected. Use /standings to track points."
    return msg


# ── Command handlers ──────────────────────────────────────────────────────

async def cmd_help(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "<b>World Cup Draft Bot</b> — commands\n\n"
        "<b>Setup</b> (run in your group chat):\n"
        "  <code>/start_league</code> — create a league for this chat (admin)\n"
        "  <code>/join</code> — add yourself to the league\n"
        "  <code>/players</code> — list signed-up players\n"
        "  <code>/start_draft</code> — randomize order &amp; begin the snake draft (admin)\n\n"
        "<b>Drafting:</b>\n"
        "  <code>/order</code> — show draft order + whose turn it is\n"
        "  <code>/pick &lt;country&gt;</code> — draft a country on your turn\n"
        "  <code>/available</code> — list undrafted countries (grouped by FIFA group)\n"
        "  <code>/undo_pick</code> — undo the most recent pick (admin)\n"
        "  <code>/end_draft</code> — end the draft early &amp; go active, even with teams left (admin)\n\n"
        "<b>Tournament:</b>\n"
        "  <code>/myteam</code> — your countries + points\n"
        "  <code>/team &lt;country&gt;</code> — owner + points for a country\n"
        "  <code>/standings</code> — leaderboard with per-team breakdown\n\n"
        "<b>Admin — match scoring</b> (goal-diff to winner):\n"
        "  <code>/set_result &lt;home&gt; &lt;h_score&gt; &lt;away&gt; &lt;a_score&gt;</code>\n"
        "    e.g. <code>/set_result england 3 ghana 0</code> → England +3.\n"
        "  <code>/undo_result</code> — remove the most recent /set_result and refund its points.\n\n"
        "<b>Admin — stage advancement</b> (one-time bonus per country):\n"
        "  <code>/set_stage_reached &lt;stage&gt; &lt;country&gt; [more countries...]</code>\n"
        "    e.g. <code>/set_stage_reached round_of_16 spain england germany</code>\n"
        "    stages: <code>round_of_32</code> +2, <code>round_of_16</code> +3, "
        "<code>quarter_final</code> +5, <code>semi_final</code> +8, "
        "<code>final</code> +12, <code>champion</code> +20\n\n"
        "<b>Admin — manual team transfer</b> (for bets settled outside Telegram):\n"
        "  <code>/transfer_team &lt;country&gt; @recipient</code> "
        "(or reply to them) — hand a team and its points to another player.\n\n"
        "<b>⚔️ Bonus mini-game:</b> stake a team, play hangman or trivia, "
        "winner takes both. See <code>/help_duels</code>."
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_help_duels(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Dedicated help page for the Team Duel mini-game."""
    text = (
        "<b>⚔️ Team Duel</b> — a bonus side-game\n\n"
        "Two players each stake one country they own and play a quick game. "
        "The <b>winner takes both teams</b> — the loser's country (and every "
        "point it's already banked, plus all future points) transfers to the "
        "winner. Available once the draft is done and matches are underway. "
        "Only one duel runs at a time per chat.\n\n"
        "<b>Setting one up (a two-step handshake)</b>\n"
        "  1. <code>/duel @rival &lt;hangman|trivia&gt; &lt;your_country&gt;</code> — "
        "challenge &amp; stake your team\n"
        "      e.g. <code>/duel @sam trivia brazil</code>\n"
        "      (or <i>reply</i> to your rival's message and drop the @rival: "
        "<code>/duel hangman brazil</code>)\n"
        "  2. <code>/accept_duel &lt;your_country&gt;</code> — the challenged player "
        "accepts and stakes their team\n"
        "  3. <code>/confirm_duel</code> — the <b>challenger</b> sees both stakes and "
        "confirms to start the game\n"
        "  Either side can bail before it starts: <code>/decline_duel</code> "
        "(challenged player) or <code>/cancel_duel</code> (challenger; admin can "
        "also stop an active one).\n\n"
        "<b>🪢 Hangman mode</b>\n"
        "  Guess a (long!) football word, turn by turn.\n"
        "  <code>/guess &lt;letter&gt;</code> — a correct letter keeps your turn; "
        "a wrong one passes it.\n"
        "  Reveal the final letter to <b>win</b> both teams. If the gallows "
        "completes (6 wrong) with the word unsolved, it's a <b>draw</b> — "
        "nobody wins and both teams stay put.\n\n"
        "<b>🧠 Trivia mode</b>\n"
        "  Race to answer World Cup questions — first to 3 correct (best of 5) wins.\n"
        "  <code>/answer &lt;your answer&gt;</code> — either duelist can answer; "
        "fastest correct takes the question. Wrong answers cost nothing.\n\n"
        "<b>Anytime</b>\n"
        "  <code>/duel_status</code> — show the current duel\n"
        "  <code>/void_duel</code> — admin reverses the most recent duel, "
        "returning the seized team"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_start_league(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if chat.type not in ("group", "supergroup"):
        await update.effective_message.reply_text(
            "Run /start_league inside a Telegram group chat — that's the league's home."
        )
        return

    existing = await asyncio.to_thread(db.get_league, chat.id)
    if existing:
        await update.effective_message.reply_text(
            f"This chat already has a league (status: <b>{_e(existing['status'])}</b>).",
            parse_mode=ParseMode.HTML,
        )
        return

    players_expected = int(os.environ.get("PLAYERS_PER_LEAGUE", "5"))
    await asyncio.to_thread(
        db.create_league, chat.id, chat.title, user.id, players_expected
    )
    await update.effective_message.reply_text(
        f"🏆 League created! Expecting <b>{players_expected}</b> players.\n"
        f"Everyone, run /join to sign up. Once {players_expected} players have joined, "
        f"the admin runs /start_draft.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_join(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    league = await asyncio.to_thread(db.get_league, chat.id)
    if not league:
        await update.effective_message.reply_text("No league here yet — admin: run /start_league first.")
        return
    if league["status"] != "signup":
        await update.effective_message.reply_text(
            f"Signup is closed (league is <b>{_e(league['status'])}</b>).",
            parse_mode=ParseMode.HTML,
        )
        return

    display = user.full_name or user.username or f"user_{user.id}"
    inserted = await asyncio.to_thread(
        db.add_player, chat.id, user.id, user.username, display
    )
    players = await asyncio.to_thread(db.list_players, chat.id, "joined_at")
    if inserted:
        msg = f"✅ <b>{_e(display)}</b> joined! ({len(players)}/{league['players_expected']})"
    else:
        msg = f"You're already in. ({len(players)}/{league['players_expected']})"
    if len(players) >= league["players_expected"]:
        msg += "\n\nFull roster! Admin: run /start_draft when ready."
    await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_players(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    league = await asyncio.to_thread(db.get_league, chat.id)
    if not league:
        await update.effective_message.reply_text("No league here. Admin: /start_league.")
        return
    players = await asyncio.to_thread(db.list_players, chat.id, "joined_at")
    if not players:
        await update.effective_message.reply_text("No players yet — run /join.")
        return
    lines = [f"<b>Players ({len(players)}/{league['players_expected']}):</b>"]
    for i, p in enumerate(players, 1):
        lines.append(f"  {i}. {_e(p['display_name'])}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_start_draft(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if not await _is_league_admin(chat.id, user.id):
        await update.effective_message.reply_text("Admin-only command.")
        return
    league = await asyncio.to_thread(db.get_league, chat.id)
    if not league:
        await update.effective_message.reply_text("No league here. Run /start_league first.")
        return
    if league["status"] != "signup":
        await update.effective_message.reply_text(
            f"Can't start draft — league is <b>{_e(league['status'])}</b>.",
            parse_mode=ParseMode.HTML,
        )
        return
    players = await asyncio.to_thread(db.list_players, chat.id, "joined_at")
    if len(players) != league["players_expected"]:
        await update.effective_message.reply_text(
            f"Need exactly {league['players_expected']} players to start "
            f"(currently {len(players)})."
        )
        return

    # Randomize draft order
    ids = [p["id"] for p in players]
    random.shuffle(ids)
    await asyncio.to_thread(db.assign_draft_order, chat.id, ids)
    await asyncio.to_thread(db.set_draft_started, chat.id)

    # Build the announcement
    ordered = await asyncio.to_thread(db.list_players, chat.id, "draft_order")
    lines = ["🎲 <b>Draft order randomized!</b>\n"]
    for p in ordered:
        lines.append(f"  {p['draft_order']}. {_e(p['display_name'])}")
    lines.append(
        f"\nSnake draft begins. <b>{_e(ordered[0]['display_name'])}</b> — "
        f"you're on the clock with pick 1."
    )
    lines.append(
        "Use <code>/pick &lt;country&gt;</code> (e.g. <code>/pick brazil</code>). "
        "Type /available to see remaining teams."
    )
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_order(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    league = await asyncio.to_thread(db.get_league, chat.id)
    if not league:
        await update.effective_message.reply_text("No league here.")
        return
    ordered = await asyncio.to_thread(db.list_players, chat.id, "draft_order")
    if not ordered or ordered[0]["draft_order"] is None:
        await update.effective_message.reply_text("Draft hasn't started yet. Admin: /start_draft.")
        return
    next_pick = await asyncio.to_thread(db.next_pick_number, chat.id)
    n = len(ordered)
    lines = ["<b>Draft order:</b>"]
    for p in ordered:
        lines.append(f"  {p['draft_order']}. {_e(p['display_name'])}")
    if draft.draft_complete(next_pick):
        lines.append("\n<b>Draft complete.</b> Use /standings.")
    else:
        pos = draft.player_for_pick(next_pick, n)
        on_clock = ordered[pos - 1]
        rd = draft.round_of(next_pick, n)
        lines.append(
            f"\n👉 Pick {next_pick} (R{rd}): <b>{_e(on_clock['display_name'])}</b> "
            f"({draft.picks_remaining(next_pick)} teams remaining)"
        )
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    league = await asyncio.to_thread(db.get_league, chat.id)
    if not league:
        await update.effective_message.reply_text("No league here.")
        return
    if league["status"] != "drafting":
        await update.effective_message.reply_text(
            f"Draft is <b>{_e(league['status'])}</b>, can't pick now.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not ctx.args:
        await update.effective_message.reply_text(
            "Usage: <code>/pick &lt;country&gt;</code> e.g. <code>/pick brazil</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    raw = " ".join(ctx.args)
    country = resolve(raw)
    if not country:
        await update.effective_message.reply_text(
            f"Don't recognize \"{_e(raw)}\". Try /available to see remaining teams and their names.",
            parse_mode=ParseMode.HTML,
        )
        return

    # Whose turn is it?
    ordered = await asyncio.to_thread(db.list_players, chat.id, "draft_order")
    n = len(ordered)
    next_pick = await asyncio.to_thread(db.next_pick_number, chat.id)
    if draft.draft_complete(next_pick):
        await update.effective_message.reply_text("Draft already complete.")
        return
    expected_position = draft.player_for_pick(next_pick, n)
    expected_player = ordered[expected_position - 1]
    if expected_player["telegram_user_id"] != user.id:
        await update.effective_message.reply_text(
            f"⏳ Not your turn — it's <b>{_e(expected_player['display_name'])}</b>'s "
            f"pick (pick {next_pick}).",
            parse_mode=ParseMode.HTML,
        )
        return

    # Is the country still available?
    existing_owner = await asyncio.to_thread(db.owner_of_country, chat.id, country.code)
    if existing_owner:
        await update.effective_message.reply_text(
            f"{_display_country(country.code)} is already drafted by "
            f"<b>{_e(existing_owner['display_name'])}</b>.",
            parse_mode=ParseMode.HTML,
        )
        return

    me = await asyncio.to_thread(db.get_player_by_user, chat.id, user.id)
    success = await asyncio.to_thread(db.make_pick, chat.id, me["id"], country.code, next_pick)
    if not success:
        # Race condition: someone else's pick landed between our checks and the insert.
        await update.effective_message.reply_text(
            f"Couldn't claim {_display_country(country.code)} — try /available and pick again.",
            parse_mode=ParseMode.HTML,
        )
        return

    # Build announcement
    new_next = next_pick + 1
    if draft.draft_complete(new_next):
        # Flip league to active
        await asyncio.to_thread(db.set_league_status, chat.id, "active")
        text = _format_pick_announcement(me["display_name"], country.code, next_pick, n, None, None)
    else:
        next_pos = draft.player_for_pick(new_next, n)
        next_p = ordered[next_pos - 1]
        text = _format_pick_announcement(me["display_name"], country.code, next_pick, n,
                                         next_pos, next_p["display_name"])
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_available(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    picks = await asyncio.to_thread(db.list_picks, chat.id)
    taken = {p["country_code"] for p in picks}
    lines = [f"<b>Available teams</b> ({48 - len(taken)} left):"]
    for grp, members in by_group().items():
        avail = [c for c in members if c.code not in taken]
        if not avail:
            continue
        names = ", ".join(f"{c.flag} {_e(c.display_name)}" for c in avail)
        lines.append(f"  <b>Group {grp}</b>: {names}")
    if len(taken) == 48:
        await update.effective_message.reply_text("All teams drafted. /standings.")
        return
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_undo_pick(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if not await _is_league_admin(chat.id, user.id):
        await update.effective_message.reply_text("Admin-only command.")
        return
    deleted = await asyncio.to_thread(db.undo_last_pick, chat.id)
    if not deleted:
        await update.effective_message.reply_text("No picks to undo.")
        return
    # If the league had been flipped to active by the final pick, roll back.
    league = await asyncio.to_thread(db.get_league, chat.id)
    if league and league["status"] == "active":
        await asyncio.to_thread(db.set_league_status, chat.id, "drafting")
    await update.effective_message.reply_text(
        f"⏪ Undid pick {deleted['pick_number']}: {_display_country(deleted['country_code'])} "
        f"is back in the pool.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_end_draft(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: end the draft early (before all 48 teams are picked) and move the
    league into its active phase. Any undrafted teams simply have no owner —
    they accrue no points and can't be staked in duels. Match scoring and the
    Team Duel mini-game open up once the league is active."""
    chat = update.effective_chat
    user = update.effective_user
    if not await _is_league_admin(chat.id, user.id):
        await update.effective_message.reply_text("Admin-only command.")
        return
    league = await asyncio.to_thread(db.get_league, chat.id)
    if not league:
        await update.effective_message.reply_text("No league here. Run /start_league first.")
        return
    if league["status"] != "drafting":
        await update.effective_message.reply_text(
            f"Nothing to end — the league is <b>{_e(league['status'])}</b>, not drafting.",
            parse_mode=ParseMode.HTML,
        )
        return

    picks = await asyncio.to_thread(db.list_picks, chat.id)
    n = len(picks)
    remaining = 48 - n
    await asyncio.to_thread(db.set_league_status, chat.id, "active")
    tail = (
        f" {remaining} team{'s' if remaining != 1 else ''} left undrafted (no owner)."
        if remaining > 0 else ""
    )
    await update.effective_message.reply_text(
        f"🏁 <b>Draft ended.</b> {n} of 48 teams drafted.{tail}\n"
        f"The league is now <b>active</b> — match scoring and ⚔️ duels are open. "
        f"See /standings and /help_duels.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_myteam(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    me = await asyncio.to_thread(db.get_player_by_user, chat.id, user.id)
    if not me:
        await update.effective_message.reply_text("You're not in this league. /join.")
        return
    my_picks = await asyncio.to_thread(db.picks_for_player, chat.id, me["id"])
    if not my_picks:
        await update.effective_message.reply_text("You haven't drafted any teams yet.")
        return
    total = 0.0
    lines = [f"<b>{_e(me['display_name'])}'s team:</b>"]
    for p in my_picks:
        pts = await asyncio.to_thread(db.points_for_country, chat.id, p["country_code"])
        total += float(pts)
        lines.append(f"  {_display_country(p['country_code'])} — {_fmt_pts(pts)} pts")
    lines.append(f"\n<b>Total:</b> {_fmt_pts(total)}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_team(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not ctx.args:
        await update.effective_message.reply_text(
            "Usage: <code>/team &lt;country&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    country = resolve(" ".join(ctx.args))
    if not country:
        await update.effective_message.reply_text("Don't recognize that country.")
        return
    owner = await asyncio.to_thread(db.owner_of_country, chat.id, country.code)
    pts = await asyncio.to_thread(db.points_for_country, chat.id, country.code)
    if not owner:
        await update.effective_message.reply_text(
            f"{_display_country(country.code)} is undrafted.",
            parse_mode=ParseMode.HTML,
        )
        return
    await update.effective_message.reply_text(
        f"{_display_country(country.code)} — owned by <b>{_e(owner['display_name'])}</b>, "
        f"{_fmt_pts(pts)} pts.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_standings(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Leaderboard with per-team breakdown for every player."""
    chat = update.effective_chat
    rows = await asyncio.to_thread(db.standings_detailed, chat.id)
    if not rows:
        await update.effective_message.reply_text("No players in this league.")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>Standings</b>"]
    for i, r in enumerate(rows):
        prefix = medals[i] if i < len(medals) else f"{i+1}."
        lines.append("")  # blank line between players
        lines.append(
            f"{prefix} <b>{_e(r['display_name'])}</b> — {_fmt_pts(r['total_points'])} pts "
            f"({len(r['teams'])} teams)"
        )
        for t in r["teams"]:
            lines.append(
                f"     {_display_country(t['country_code'])} — {_fmt_pts(t['points'])} pts"
            )
        if not r["teams"]:
            lines.append("     <i>(no teams drafted yet)</i>")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


def _is_int(s: str) -> bool:
    try:
        int(s)
        return True
    except ValueError:
        return False


def _parse_result_args(args: list[str]) -> Optional[tuple[str, str, str, str]]:
    """
    Split /set_result args into (home, home_score, away, away_score),
    allowing multi-word country names ("south africa", "new zealand", ...).
    Telegram splits on spaces, so we re-join: everything before the first
    integer token is the home team, the last token is the away score, and
    whatever sits between the two scores is the away team.
      ["south", "africa", "2", "mexico", "0"]
        → ("south africa", "2", "mexico", "0")
    Returns None if the args don't fit that shape.
    """
    if len(args) < 4 or not _is_int(args[-1]):
        return None
    first_int = next((i for i, a in enumerate(args) if _is_int(a)), None)
    # Need ≥1 home token before the score and ≥1 away token between scores.
    if first_int is None or first_int == 0 or first_int >= len(args) - 1:
        return None
    away = " ".join(args[first_int + 1 : -1])
    if not away:
        return None
    return " ".join(args[:first_int]), args[first_int], away, args[-1]


def _resolve_country_tokens(tokens: list[str]):
    """
    Resolve a flat token list into countries, merging multi-word names
    greedily (longest match first, up to 3 tokens). Returns (countries, None)
    on success or (None, bad_token) on the first unresolvable token.
    """
    out, i = [], 0
    while i < len(tokens):
        for n in range(min(3, len(tokens) - i), 0, -1):
            c = resolve(" ".join(tokens[i : i + n]))
            if c:
                out.append(c)
                i += n
                break
        else:
            return None, tokens[i]
    return out, None


async def cmd_set_result(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Usage: /set_result <home_country> <home_score> <away_country> <away_score>
    Records a finished match and awards goal-differential points to the winner.
    Stage of the match doesn't matter — scoring is identical for group and
    knockout matches.
    """
    chat = update.effective_chat
    user = update.effective_user
    if not await _is_league_admin(chat.id, user.id):
        await update.effective_message.reply_text("Admin-only command.")
        return
    parsed = _parse_result_args(ctx.args)
    if parsed is None:
        await update.effective_message.reply_text(
            "Usage: <code>/set_result &lt;home&gt; &lt;home_score&gt; &lt;away&gt; "
            "&lt;away_score&gt;</code>\n"
            "Examples: <code>/set_result england 3 ghana 0</code>, "
            "<code>/set_result south africa 2 mexico 0</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    home_raw, home_score_raw, away_raw, away_score_raw = parsed
    home = resolve(home_raw)
    away = resolve(away_raw)
    if not home or not away:
        await update.effective_message.reply_text("Unknown country name.")
        return
    try:
        hs, as_ = int(home_score_raw), int(away_score_raw)
    except ValueError:
        await update.effective_message.reply_text("Scores must be integers.")
        return

    # Stage column on the match row is no longer surfaced anywhere; we still
    # store something sane to satisfy the NOT NULL constraint on init.sql.
    match_id = await asyncio.to_thread(
        db.upsert_match, None, "group", None, home.code, away.code, None, hs, as_, "finished"
    )
    match = next(
        (m for m in await asyncio.to_thread(db.finished_matches) if m["id"] == match_id),
        None,
    )
    if not match:
        await update.effective_message.reply_text(
            "Saved the match, but couldn't reload it for scoring."
        )
        return
    awarded = await asyncio.to_thread(scoring.score_match_for_league, chat.id, match)
    body = (
        f"📝 Recorded: {_display_country(home.code)} {hs}-{as_} {_display_country(away.code)}\n"
    )
    if awarded:
        body += "\n<b>Match points (0.5 per goal of differential):</b>\n"
        for name, desc, pts in awarded:
            body += f"  +{_fmt_pts(pts)} → {_e(name)}\n"
        body += (
            "\n<i>Reminder: stage-advancement bonuses are separate — "
            "use /set_stage_reached after each round.</i>"
        )
    else:
        body += "\n<i>(draw — no points awarded)</i>"
    await update.effective_message.reply_text(body, parse_mode=ParseMode.HTML)


async def cmd_undo_result(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove the most recently entered match and refund its match-win points."""
    chat = update.effective_chat
    user = update.effective_user
    if not await _is_league_admin(chat.id, user.id):
        await update.effective_message.reply_text("Admin-only command.")
        return
    last = await asyncio.to_thread(db.last_match)
    if not last:
        await update.effective_message.reply_text("No matches recorded yet — nothing to undo.")
        return
    n_events = await asyncio.to_thread(db.delete_match_and_events, last["id"])
    hs, as_ = last["home_score"], last["away_score"]
    score = f"{hs}-{as_}" if hs is not None and as_ is not None else "(no score)"
    await update.effective_message.reply_text(
        f"⏪ Undid: {_display_country(last['home_country'])} {score} "
        f"{_display_country(last['away_country'])} (<code>{_e(last['stage'])}</code>)\n"
        f"<i>Refunded {n_events} point event{'s' if n_events != 1 else ''}.</i>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_set_stage_reached(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Award the one-time stage-advancement bonus to one or more countries.
    Usage: /set_stage_reached <stage> <country1> [country2 ...]
    Idempotent — re-running for the same country+stage does nothing.
    """
    chat = update.effective_chat
    user = update.effective_user
    if not await _is_league_admin(chat.id, user.id):
        await update.effective_message.reply_text("Admin-only command.")
        return
    if len(ctx.args) < 2:
        await update.effective_message.reply_text(
            "Usage: <code>/set_stage_reached &lt;stage&gt; &lt;country&gt; [more countries...]</code>\n"
            "Stages: <code>round_of_32</code> +2, <code>round_of_16</code> +3, "
            "<code>quarter_final</code> +5, <code>semi_final</code> +8, "
            "<code>final</code> +12, <code>champion</code> +20\n"
            "Example: <code>/set_stage_reached round_of_16 spain england germany brazil</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    stage = ctx.args[0]
    country_args = ctx.args[1:]
    if stage not in scoring.STAGE_REACH_EVENT:
        await update.effective_message.reply_text(
            f"Unknown stage <code>{_e(stage)}</code>. "
            f"Valid: {', '.join(f'<code>{s}</code>' for s in scoring.STAGE_REACH_EVENT)}",
            parse_mode=ParseMode.HTML,
        )
        return

    # Resolve every country first — fail loudly if any are unknown so admin
    # doesn't half-apply a batch and have to figure out which ones landed.
    # Multi-word names ("south africa") are merged automatically.
    resolved, bad = _resolve_country_tokens(country_args)
    if resolved is None:
        await update.effective_message.reply_text(
            f"Unknown country: <code>{_e(bad)}</code> — nothing was awarded.",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = [f"<b>Stage:</b> <code>{_e(stage)}</code>"]
    for c in resolved:
        result = await asyncio.to_thread(
            scoring.award_stage_reached, chat.id, c.code, stage
        )
        if result is None:
            # Either undrafted, or already awarded
            owner = await asyncio.to_thread(db.owner_of_country, chat.id, c.code)
            if owner is None:
                lines.append(f"  ⏭ {_display_country(c.code)} — undrafted, skipped")
            else:
                lines.append(f"  ⏭ {_display_country(c.code)} — already awarded, skipped")
        else:
            display_name, event_type, points = result
            lines.append(
                f"  +{points} → <b>{_e(display_name)}</b>  ({_display_country(c.code)})"
            )
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ── Team Duel ───────────────────────────────────────────────────────────────
#
# Two players each stake one country they own; a mini-game (hangman or trivia)
# decides a winner; the loser's staked team transfers to the winner. State for
# the in-progress game lives as JSON in duels.state — see app/duel.py. Only one
# open (pending/active) duel per league at a time, so /guess and /answer never
# have to disambiguate which duel they belong to.

async def _duel_names(chat_id: int, d: dict) -> dict[int, str]:
    """Map both duelists' player_id → display_name for rendering."""
    ch = await asyncio.to_thread(db.get_player_by_id, chat_id, d["challenger_player_id"])
    op = await asyncio.to_thread(db.get_player_by_id, chat_id, d["opponent_player_id"])
    return {
        d["challenger_player_id"]: ch["display_name"] if ch else "?",
        d["opponent_player_id"]: op["display_name"] if op else "?",
    }


def _hangman_board(state: dict, names: dict[int, str]) -> str:
    masked = duel.hangman_masked(state)
    gallows = duel.hangman_gallows(len(state["wrong"]))
    wrong = ", ".join(state["wrong"]) if state["wrong"] else "—"
    turn_name = names.get(state["turn"], "?")
    return (
        f"<pre>{gallows}</pre>\n"
        f"Word: <code>{masked}</code>\n"
        f"Wrong ({len(state['wrong'])}/{duel.MAX_WRONG}): {_e(wrong)}\n"
        f"👉 <b>{_e(turn_name)}</b>'s turn — <code>/guess &lt;letter&gt;</code>"
    )


def _trivia_question_text(state: dict, names: dict[int, str], prefix: str = "") -> str:
    q = duel.trivia_current_question(state)
    if q is None:
        return prefix.strip()
    qnum = state["current"] + 1
    a, b = state["challenger_id"], state["opponent_id"]
    score = (f"{_e(names[a])} {duel.trivia_score(state, a)} — "
             f"{duel.trivia_score(state, b)} {_e(names[b])}")
    return (
        f"{prefix}<b>Q{qnum}</b> (first to {duel.WIN_SCORE} wins): {_e(q['q'])}\n"
        f"Answer with <code>/answer &lt;your answer&gt;</code>\n"
        f"<i>Score: {score}</i>"
    )


def _staked_country(d: dict, player_id: int) -> Optional[str]:
    """The country a given duelist staked, or None."""
    if player_id == d["challenger_player_id"]:
        return d["challenger_country"]
    if player_id == d["opponent_player_id"]:
        return d["opponent_country"]
    return None


async def _finalize_duel(update: Update, chat_id: int, d: dict, state: dict,
                         winner_id: int, loser_id: int) -> str:
    """Transfer the loser's staked team to the winner and return an announcement."""
    loser_country = _staked_country(d, loser_id)
    names = await _duel_names(chat_id, d)
    await asyncio.to_thread(
        db.finish_duel_and_transfer, d["id"], chat_id, winner_id,
        loser_country, json.dumps(state),
    )
    winner_country = _staked_country(d, winner_id)
    return (
        f"🏆 <b>{_e(names[winner_id])}</b> wins the duel!\n"
        f"{_display_country(loser_country)} is seized from "
        f"<b>{_e(names[loser_id])}</b> and joins "
        f"<b>{_e(names[winner_id])}</b>'s squad alongside "
        f"{_display_country(winner_country)}.\n"
        f"<i>Use /myteam or /standings to see the updated rosters. "
        f"Admin can reverse this with /void_duel.</i>"
    )


async def cmd_duel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Challenge another player to a Team Duel.
      Reply to their message:  /duel <mode> <your_country>
      Or @-mention them:       /duel @rival <mode> <your_country>
    mode is 'hangman' or 'trivia'. You stake a country you own; they reply with
    /accept_duel <their_country>.
    """
    chat = update.effective_chat
    user = update.effective_user
    league = await asyncio.to_thread(db.get_league, chat.id)
    if not league:
        await update.effective_message.reply_text("No league here. Admin: /start_league.")
        return
    if league["status"] != "active":
        await update.effective_message.reply_text(
            f"Duels open once the draft is done and matches are underway "
            f"(league is <b>{_e(league['status'])}</b>).",
            parse_mode=ParseMode.HTML,
        )
        return

    existing = await asyncio.to_thread(db.get_open_duel, chat.id)
    if existing:
        await update.effective_message.reply_text(
            "A duel is already in progress in this chat. Finish or /cancel_duel it first."
        )
        return

    me = await asyncio.to_thread(db.get_player_by_user, chat.id, user.id)
    if not me:
        await update.effective_message.reply_text("You're not in this league. /join first.")
        return

    # Resolve opponent + mode + country from either the reply target or an @mention.
    reply = update.effective_message.reply_to_message
    args = list(ctx.args)
    opponent = None
    if reply and reply.from_user and not (args and args[0].startswith("@")):
        opponent = await asyncio.to_thread(
            db.get_player_by_user, chat.id, reply.from_user.id
        )
        if not opponent:
            await update.effective_message.reply_text(
                "That person isn't in the league."
            )
            return
        mode_country = args
    else:
        if not args or not args[0].startswith("@"):
            await update.effective_message.reply_text(
                "Usage: reply to your rival with <code>/duel &lt;mode&gt; &lt;your_country&gt;</code>, "
                "or <code>/duel @rival &lt;mode&gt; &lt;your_country&gt;</code>.\n"
                "Modes: <code>hangman</code>, <code>trivia</code>.",
                parse_mode=ParseMode.HTML,
            )
            return
        opponent = await asyncio.to_thread(
            db.get_player_by_username, chat.id, args[0]
        )
        if not opponent:
            await update.effective_message.reply_text(
                f"Couldn't find {_e(args[0])} in the league. They may have no @username set — "
                f"try replying to one of their messages with /duel instead.",
                parse_mode=ParseMode.HTML,
            )
            return
        mode_country = args[1:]

    if opponent["id"] == me["id"]:
        await update.effective_message.reply_text("You can't duel yourself. 🙂")
        return
    if not mode_country:
        await update.effective_message.reply_text(
            "Tell me the mode and the country you're staking: "
            "<code>/duel … &lt;hangman|trivia&gt; &lt;your_country&gt;</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    mode = mode_country[0].lower()
    if mode not in duel.MODES:
        await update.effective_message.reply_text(
            f"Unknown mode <code>{_e(mode_country[0])}</code>. "
            f"Choose <code>hangman</code> or <code>trivia</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    country = resolve(" ".join(mode_country[1:]))
    if not country:
        await update.effective_message.reply_text(
            "Don't recognize that country. Stake one you own — see /myteam."
        )
        return
    owner = await asyncio.to_thread(db.owner_of_country, chat.id, country.code)
    if not owner or owner["id"] != me["id"]:
        await update.effective_message.reply_text(
            f"You can only stake a country you own. {_display_country(country.code)} "
            f"isn't yours — see /myteam.",
            parse_mode=ParseMode.HTML,
        )
        return

    duel_id = await asyncio.to_thread(
        db.create_duel, chat.id, me["id"], country.code, opponent["id"], mode
    )
    if duel_id is None:
        # Lost a race to another /duel — the DB's one-open-duel-per-chat guard
        # rejected this insert.
        await update.effective_message.reply_text(
            "Another duel just started in this chat — only one runs at a time. "
            "Try again once it's finished."
        )
        return
    await update.effective_message.reply_text(
        f"⚔️ <b>{_e(me['display_name'])}</b> challenges "
        f"<b>{_e(opponent['display_name'])}</b> to a <b>{_e(mode)}</b> duel, "
        f"staking {_display_country(country.code)}!\n\n"
        f"<b>{_e(opponent['display_name'])}</b> — accept by staking one of your teams: "
        f"<code>/accept_duel &lt;your_country&gt;</code>, or <code>/decline_duel</code>.\n"
        f"<i>Winner takes both teams.</i>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_accept_duel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Opponent accepts a challenge and names their stake. This does NOT start
    the game — the challenger must /confirm_duel first (two-sided handshake)."""
    chat = update.effective_chat
    user = update.effective_user
    d = await asyncio.to_thread(db.get_open_duel, chat.id)
    if not d or d["status"] != "pending":
        await update.effective_message.reply_text("No pending duel to accept.")
        return
    me = await asyncio.to_thread(db.get_player_by_user, chat.id, user.id)
    if not me or me["id"] != d["opponent_player_id"]:
        await update.effective_message.reply_text(
            "Only the challenged player can accept this duel."
        )
        return
    if d["opponent_country"] is not None:
        names = await _duel_names(chat.id, d)
        await update.effective_message.reply_text(
            f"You've already staked {_display_country(d['opponent_country'])}. "
            f"Waiting on <b>{_e(names[d['challenger_player_id']])}</b> to "
            f"<code>/confirm_duel</code>.",
            parse_mode=ParseMode.HTML,
        )
        return
    if not ctx.args:
        await update.effective_message.reply_text(
            "Stake a team you own: <code>/accept_duel &lt;your_country&gt;</code>.",
            parse_mode=ParseMode.HTML,
        )
        return
    country = resolve(" ".join(ctx.args))
    if not country:
        await update.effective_message.reply_text("Don't recognize that country. See /myteam.")
        return
    owner = await asyncio.to_thread(db.owner_of_country, chat.id, country.code)
    if not owner or owner["id"] != me["id"]:
        await update.effective_message.reply_text(
            f"You can only stake a country you own — {_display_country(country.code)} isn't yours.",
            parse_mode=ParseMode.HTML,
        )
        return
    if country.code == d["challenger_country"]:
        # Can't happen (different owners) but guard anyway.
        await update.effective_message.reply_text("Pick a different team from the one already staked.")
        return

    await asyncio.to_thread(db.stake_opponent, d["id"], country.code)
    names = await _duel_names(chat.id, d)
    challenger_name = names[d["challenger_player_id"]]
    await update.effective_message.reply_text(
        f"🤝 <b>{_e(me['display_name'])}</b> accepts, staking "
        f"{_display_country(country.code)}!\n\n"
        f"<b>Stakes:</b> {_display_country(d['challenger_country'])} "
        f"(<b>{_e(challenger_name)}</b>) vs {_display_country(country.code)} "
        f"(<b>{_e(me['display_name'])}</b>)\n\n"
        f"<b>{_e(challenger_name)}</b> — you challenged, so you get the final say. "
        f"<code>/confirm_duel</code> to start the <b>{_e(d['mode'])}</b> game, "
        f"or <code>/cancel_duel</code> to back out.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_confirm_duel(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Challenger confirms the matchup after the opponent has staked — this is
    what actually starts the game."""
    chat = update.effective_chat
    user = update.effective_user
    d = await asyncio.to_thread(db.get_open_duel, chat.id)
    if not d or d["status"] != "pending" or d["opponent_country"] is None:
        await update.effective_message.reply_text(
            "Nothing to confirm — the challenged player needs to /accept_duel "
            "and stake a team first."
        )
        return
    me = await asyncio.to_thread(db.get_player_by_user, chat.id, user.id)
    if not me or me["id"] != d["challenger_player_id"]:
        names = await _duel_names(chat.id, d)
        await update.effective_message.reply_text(
            f"Only <b>{_e(names[d['challenger_player_id']])}</b> (who issued the "
            f"challenge) can confirm.",
            parse_mode=ParseMode.HTML,
        )
        return

    names = await _duel_names(chat.id, d)
    ch_id, op_id = d["challenger_player_id"], d["opponent_player_id"]
    matchup = (
        f"{_display_country(d['challenger_country'])} vs "
        f"{_display_country(d['opponent_country'])}"
    )
    if d["mode"] == "hangman":
        state = duel.new_hangman_state(ch_id, op_id)
        await asyncio.to_thread(db.activate_duel, d["id"], json.dumps(state))
        intro = (
            f"✅ Duel confirmed! {matchup}.\n"
            f"🪢 <b>Hangman</b> — guess the football word. "
            f"Correct letter keeps your turn; a wrong one passes it. "
            f"Reveal the last letter to win; if the gallows completes (6 wrong) "
            f"with the word unsolved, it's a draw and no one wins.\n\n"
        )
        await update.effective_message.reply_text(
            intro + _hangman_board(state, names), parse_mode=ParseMode.HTML
        )
    else:  # trivia
        state = duel.new_trivia_state(ch_id, op_id)
        await asyncio.to_thread(db.activate_duel, d["id"], json.dumps(state))
        intro = (
            f"✅ Duel confirmed! {matchup}.\n"
            f"🧠 <b>Trivia</b> — first to {duel.WIN_SCORE} correct wins. "
            f"Either player answers; fastest correct takes the question.\n\n"
        )
        await update.effective_message.reply_text(
            intro + _trivia_question_text(state, names), parse_mode=ParseMode.HTML
        )


async def cmd_decline_duel(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    d = await asyncio.to_thread(db.get_open_duel, chat.id)
    if not d or d["status"] != "pending":
        await update.effective_message.reply_text("No pending duel to decline.")
        return
    me = await asyncio.to_thread(db.get_player_by_user, chat.id, user.id)
    if not me or me["id"] != d["opponent_player_id"]:
        await update.effective_message.reply_text("Only the challenged player can decline.")
        return
    await asyncio.to_thread(db.set_duel_status, d["id"], "cancelled")
    await update.effective_message.reply_text("🚫 Duel declined.")


async def cmd_cancel_duel(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Challenger withdraws a still-pending duel. Admin can cancel an active one."""
    chat = update.effective_chat
    user = update.effective_user
    d = await asyncio.to_thread(db.get_open_duel, chat.id)
    if not d:
        await update.effective_message.reply_text("No duel in progress.")
        return
    me = await asyncio.to_thread(db.get_player_by_user, chat.id, user.id)
    is_challenger = bool(me and me["id"] == d["challenger_player_id"])
    is_admin = await _is_league_admin(chat.id, user.id)
    if d["status"] == "pending" and not (is_challenger or is_admin):
        await update.effective_message.reply_text("Only the challenger (or an admin) can cancel.")
        return
    if d["status"] == "active" and not is_admin:
        await update.effective_message.reply_text(
            "The duel's already underway — only an admin can cancel it now."
        )
        return
    await asyncio.to_thread(db.set_duel_status, d["id"], "cancelled")
    await update.effective_message.reply_text("🚫 Duel cancelled. No teams changed hands.")


async def cmd_guess(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    d = await asyncio.to_thread(db.get_open_duel, chat.id)
    if not d or d["status"] != "active":
        await update.effective_message.reply_text("No active duel to guess in.")
        return
    if d["mode"] != "hangman":
        await update.effective_message.reply_text("This duel is trivia — use /answer.")
        return
    me = await asyncio.to_thread(db.get_player_by_user, chat.id, user.id)
    if not me or me["id"] not in (d["challenger_player_id"], d["opponent_player_id"]):
        await update.effective_message.reply_text("You're not in this duel.")
        return
    if not ctx.args:
        await update.effective_message.reply_text(
            "Guess a letter: <code>/guess a</code>.", parse_mode=ParseMode.HTML
        )
        return

    state = json.loads(d["state"])
    names = await _duel_names(chat.id, d)
    result = duel.hangman_guess(state, me["id"], ctx.args[0])
    status = result["status"]

    if status == "not_turn":
        await update.effective_message.reply_text(
            f"⏳ Not your turn — waiting on <b>{_e(names[state['turn']])}</b>.",
            parse_mode=ParseMode.HTML,
        )
        return
    if status == "invalid":
        await update.effective_message.reply_text("Guess a single letter A–Z.")
        return
    if status == "repeat":
        await update.effective_message.reply_text("That letter's already been tried.")
        return

    # State changed — persist it.
    await asyncio.to_thread(db.update_duel_state, d["id"], json.dumps(state))

    if status == "hit":
        await update.effective_message.reply_text(
            "✅ Hit! Keep going.\n\n" + _hangman_board(state, names),
            parse_mode=ParseMode.HTML,
        )
    elif status == "miss":
        await update.effective_message.reply_text(
            "❌ Miss.\n\n" + _hangman_board(state, names),
            parse_mode=ParseMode.HTML,
        )
    elif status == "win":
        word_line = f"The word was <b>{_e(state['word'])}</b>.\n\n"
        announce = await _finalize_duel(
            update, chat.id, d, state, result["winner"], result["loser"]
        )
        await update.effective_message.reply_text(word_line + announce, parse_mode=ParseMode.HTML)
    elif status == "draw":
        # Gallows complete, word unsolved — nobody wins, no team changes hands.
        await asyncio.to_thread(db.draw_duel, d["id"], json.dumps(state))
        await update.effective_message.reply_text(
            f"{_hangman_board(state, names)}\n\n"
            f"💀 The hangman's dead and the word was never cracked — "
            f"it was <b>{_e(state['word'])}</b>.\n"
            f"🤝 <b>It's a draw.</b> No one wins, both teams stay put.",
            parse_mode=ParseMode.HTML,
        )


async def cmd_answer(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    d = await asyncio.to_thread(db.get_open_duel, chat.id)
    if not d or d["status"] != "active":
        await update.effective_message.reply_text("No active duel to answer in.")
        return
    if d["mode"] != "trivia":
        await update.effective_message.reply_text("This duel is hangman — use /guess.")
        return
    me = await asyncio.to_thread(db.get_player_by_user, chat.id, user.id)
    if not me or me["id"] not in (d["challenger_player_id"], d["opponent_player_id"]):
        await update.effective_message.reply_text("You're not in this duel.")
        return
    if not ctx.args:
        await update.effective_message.reply_text(
            "Answer the question: <code>/answer &lt;your answer&gt;</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    state = json.loads(d["state"])
    names = await _duel_names(chat.id, d)
    result = duel.trivia_answer(state, me["id"], " ".join(ctx.args))
    status = result["status"]

    if status == "over":
        await update.effective_message.reply_text("That question's already settled.")
        return
    if status == "wrong":
        await update.effective_message.reply_text(
            f"❌ <b>{_e(me['display_name'])}</b>: not it — keep trying!",
            parse_mode=ParseMode.HTML,
        )
        return

    await asyncio.to_thread(db.update_duel_state, d["id"], json.dumps(state))

    if status == "correct":
        prefix = (
            f"✅ <b>{_e(me['display_name'])}</b> nails it — "
            f"the answer was <b>{_e(result['canonical'])}</b>!\n\n"
        )
        await update.effective_message.reply_text(
            _trivia_question_text(state, names, prefix), parse_mode=ParseMode.HTML
        )
    elif status == "win":
        head = (
            f"✅ <b>{_e(me['display_name'])}</b> — the answer was "
            f"<b>{_e(result['canonical'])}</b>!\n"
        )
        announce = await _finalize_duel(
            update, chat.id, d, state, result["winner"], result["loser"]
        )
        await update.effective_message.reply_text(head + "\n" + announce, parse_mode=ParseMode.HTML)


async def cmd_duel_status(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    d = await asyncio.to_thread(db.get_open_duel, chat.id)
    if not d:
        await update.effective_message.reply_text("No duel in progress. Start one with /duel.")
        return
    names = await _duel_names(chat.id, d)
    ch, op = d["challenger_player_id"], d["opponent_player_id"]
    if d["status"] == "pending":
        if d["opponent_country"] is None:
            # Awaiting the opponent's acceptance + stake.
            await update.effective_message.reply_text(
                f"⚔️ <b>{_e(names[ch])}</b> ({_display_country(d['challenger_country'])}) "
                f"challenged <b>{_e(names[op])}</b> to a <b>{_e(d['mode'])}</b> duel.\n"
                f"Waiting on <b>{_e(names[op])}</b> to <code>/accept_duel</code>.",
                parse_mode=ParseMode.HTML,
            )
        else:
            # Both staked — awaiting the challenger's confirmation.
            await update.effective_message.reply_text(
                f"⚔️ <b>{_e(d['mode'])}</b> duel — both teams staked:\n"
                f"{_display_country(d['challenger_country'])} (<b>{_e(names[ch])}</b>) "
                f"vs {_display_country(d['opponent_country'])} (<b>{_e(names[op])}</b>)\n"
                f"Waiting on <b>{_e(names[ch])}</b> to <code>/confirm_duel</code> "
                f"(or <code>/cancel_duel</code>).",
                parse_mode=ParseMode.HTML,
            )
        return
    state = json.loads(d["state"])
    if d["mode"] == "hangman":
        await update.effective_message.reply_text(
            f"🪢 <b>Hangman duel</b> in progress:\n\n" + _hangman_board(state, names),
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.effective_message.reply_text(
            f"🧠 <b>Trivia duel</b> in progress:\n\n" + _trivia_question_text(state, names),
            parse_mode=ParseMode.HTML,
        )


async def cmd_void_duel(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: reverse the most recent completed duel, returning the seized team."""
    chat = update.effective_chat
    user = update.effective_user
    if not await _is_league_admin(chat.id, user.id):
        await update.effective_message.reply_text("Admin-only command.")
        return
    d = await asyncio.to_thread(db.last_completed_duel, chat.id)
    if not d:
        await update.effective_message.reply_text("No completed duel to void.")
        return
    winner_id = d["winner_player_id"]
    loser_id = (d["opponent_player_id"] if winner_id == d["challenger_player_id"]
                else d["challenger_player_id"])
    loser_country = _staked_country(d, loser_id)
    names = await _duel_names(chat.id, d)
    await asyncio.to_thread(
        db.void_duel_and_revert, d["id"], chat.id, loser_id, loser_country
    )
    await update.effective_message.reply_text(
        f"↩️ Voided the last duel. {_display_country(loser_country)} returns to "
        f"<b>{_e(names[loser_id])}</b>.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_transfer_team(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Admin: manually hand a country (and its banked points) to another player.
    Used to settle bets made outside Telegram.
      Reply to the recipient:  /transfer_team <country>
      Or @-mention them:       /transfer_team <country> @recipient
    """
    chat = update.effective_chat
    user = update.effective_user
    if not await _is_league_admin(chat.id, user.id):
        await update.effective_message.reply_text("Admin-only command.")
        return

    usage = (
        "Usage: reply to the recipient with <code>/transfer_team &lt;country&gt;</code>, "
        "or <code>/transfer_team &lt;country&gt; @recipient</code>."
    )
    args = list(ctx.args)
    reply = update.effective_message.reply_to_message

    # Resolve the recipient: trailing @mention takes priority, else the reply target.
    recipient = None
    if args and args[-1].startswith("@"):
        recipient = await asyncio.to_thread(db.get_player_by_username, chat.id, args[-1])
        if not recipient:
            await update.effective_message.reply_text(
                f"Couldn't find {_e(args[-1])} in the league. They may have no @username set — "
                f"try replying to one of their messages instead.",
                parse_mode=ParseMode.HTML,
            )
            return
        country_tokens = args[:-1]
    elif reply and reply.from_user:
        recipient = await asyncio.to_thread(db.get_player_by_user, chat.id, reply.from_user.id)
        if not recipient:
            await update.effective_message.reply_text("That person isn't in the league.")
            return
        country_tokens = args
    else:
        await update.effective_message.reply_text(usage, parse_mode=ParseMode.HTML)
        return

    if not country_tokens:
        await update.effective_message.reply_text(usage, parse_mode=ParseMode.HTML)
        return

    country = resolve(" ".join(country_tokens))
    if not country:
        await update.effective_message.reply_text("Don't recognize that country.")
        return

    owner = await asyncio.to_thread(db.owner_of_country, chat.id, country.code)
    if not owner:
        await update.effective_message.reply_text(
            f"{_display_country(country.code)} is undrafted — nothing to transfer.",
            parse_mode=ParseMode.HTML,
        )
        return
    if owner["id"] == recipient["id"]:
        await update.effective_message.reply_text(
            f"{_display_country(country.code)} is already owned by "
            f"<b>{_e(recipient['display_name'])}</b>.",
            parse_mode=ParseMode.HTML,
        )
        return

    await asyncio.to_thread(db.transfer_team, chat.id, country.code, recipient["id"])
    await update.effective_message.reply_text(
        f"🔁 Transferred {_display_country(country.code)} from "
        f"<b>{_e(owner['display_name'])}</b> to <b>{_e(recipient['display_name'])}</b> "
        f"(points moved too).",
        parse_mode=ParseMode.HTML,
    )


# ── Entry point ────────────────────────────────────────────────────────────

def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set; cannot start.")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("help_duels", cmd_help_duels))
    app.add_handler(CommandHandler("start_league", cmd_start_league))
    app.add_handler(CommandHandler("join", cmd_join))
    app.add_handler(CommandHandler("players", cmd_players))
    app.add_handler(CommandHandler("start_draft", cmd_start_draft))
    app.add_handler(CommandHandler("end_draft", cmd_end_draft))
    app.add_handler(CommandHandler("order", cmd_order))
    app.add_handler(CommandHandler("pick", cmd_pick))
    app.add_handler(CommandHandler("available", cmd_available))
    app.add_handler(CommandHandler("undo_pick", cmd_undo_pick))
    app.add_handler(CommandHandler("myteam", cmd_myteam))
    app.add_handler(CommandHandler("team", cmd_team))
    app.add_handler(CommandHandler("standings", cmd_standings))
    app.add_handler(CommandHandler("set_result", cmd_set_result))
    app.add_handler(CommandHandler("undo_result", cmd_undo_result))
    app.add_handler(CommandHandler("set_stage_reached", cmd_set_stage_reached))
    app.add_handler(CommandHandler("transfer_team", cmd_transfer_team))

    # Team Duel mini-game
    app.add_handler(CommandHandler("duel", cmd_duel))
    app.add_handler(CommandHandler("accept_duel", cmd_accept_duel))
    app.add_handler(CommandHandler("confirm_duel", cmd_confirm_duel))
    app.add_handler(CommandHandler("decline_duel", cmd_decline_duel))
    app.add_handler(CommandHandler("cancel_duel", cmd_cancel_duel))
    app.add_handler(CommandHandler("guess", cmd_guess))
    app.add_handler(CommandHandler("answer", cmd_answer))
    app.add_handler(CommandHandler("duel_status", cmd_duel_status))
    app.add_handler(CommandHandler("void_duel", cmd_void_duel))

    # Heartbeat job — every 60s, touch the file the healthcheck watches.
    if app.job_queue is not None:
        app.job_queue.run_repeating(_heartbeat_job, interval=60, first=5)
    else:
        log.warning("JobQueue unavailable — install python-telegram-bot[job-queue]. "
                    "Healthcheck will fail until this is fixed.")

    log.info("Bot starting (long polling)...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
# end
