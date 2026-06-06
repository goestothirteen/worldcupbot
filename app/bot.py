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

from app import db, draft, scoring
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
        "  <code>/undo_pick</code> — undo the most recent pick (admin)\n\n"
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
        "<code>final</code> +12, <code>champion</code> +20"
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
    args = ctx.args
    if len(args) < 4:
        await update.effective_message.reply_text(
            "Usage: <code>/set_result &lt;home&gt; &lt;home_score&gt; &lt;away&gt; "
            "&lt;away_score&gt;</code>\n"
            "Example: <code>/set_result england 3 ghana 0</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    home_raw, home_score_raw, away_raw, away_score_raw = args[:4]
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
    resolved = []
    for raw in country_args:
        c = resolve(raw)
        if not c:
            await update.effective_message.reply_text(
                f"Unknown country: <code>{_e(raw)}</code> — nothing was awarded.",
                parse_mode=ParseMode.HTML,
            )
            return
        resolved.append(c)

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


# ── Entry point ────────────────────────────────────────────────────────────

def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set; cannot start.")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("start_league", cmd_start_league))
    app.add_handler(CommandHandler("join", cmd_join))
    app.add_handler(CommandHandler("players", cmd_players))
    app.add_handler(CommandHandler("start_draft", cmd_start_draft))
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
