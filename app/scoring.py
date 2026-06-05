"""
Scoring engine.

There are two INDEPENDENT scoring tracks, each producing point_events rows:

1) Match points  (driven by /set_result → score_match_for_league)
   Winner earns points equal to goal differential. Loser earns 0. Draws
   award nothing. Stage of the match does NOT affect scoring — group,
   R32, R16, QF, SF, F all use the same rule. Examples:
     England 3-0 Ghana → England +3
     Spain 2-1 Germany → Spain +1
     France 1-1 Italy  → no points
   The stored event_type is "match_win", with match_id, so it's idempotent
   per match.

2) Stage-advancement points  (driven by /set_stage_reached → admin only)
   One-time bonus when a country advances to a knockout stage:
     reach_round_of_32   +2
     reach_round_of_16   +3
     reach_quarter       +5
     reach_semi          +8
     reach_final        +12
     champion           +20
   These are NEVER awarded automatically by score_match. The admin calls
   /set_stage_reached after each round's bracket is settled, listing the
   countries that have advanced. This decouples "did this team make it?"
   from match entry, which keeps tie-break/penalty-shootout edge cases
   out of the scoring engine.

Idempotency:
  * match_win is unique per (league, country, event_type, match_id) — re-running
    /set_result on the same match awards nothing.
  * reach_* / champion are unique per (league, country, event_type) — re-running
    /set_stage_reached for the same country+stage awards nothing.
"""

from __future__ import annotations

import logging
from typing import Optional

from app import db

log = logging.getLogger(__name__)

# Stage-advancement bonus points. Consumed ONLY by /set_stage_reached.
# Maps a user-facing stage keyword to (internal event_type, points).
STAGE_REACH_EVENT: dict[str, tuple[str, int]] = {
    "round_of_32":   ("reach_round_of_32", 2),
    "round_of_16":   ("reach_round_of_16", 3),
    "quarter_final": ("reach_quarter",     5),
    "semi_final":    ("reach_semi",        8),
    "final":         ("reach_final",      12),
    "champion":      ("champion",         20),
}


def score_match_for_league(league_chat_id: int, match: dict) -> list[tuple[str, str, int]]:
    """
    Award match-result points for a single finished match in a single league.
    Returns a list of (display_name, event_description, points_awarded) tuples
    for posting back to the chat. Empty list if it was a draw or already scored.

    Points: winner earns goal-differential points; loser 0; draw nothing.
    Stage is ignored — it's only stored on the match row for display.
    """
    if match["status"] != "finished":
        return []
    if match["home_score"] is None or match["away_score"] is None:
        return []

    awarded: list[tuple[str, str, int]] = []
    home = match["home_country"]
    away = match["away_country"]
    match_id = match["id"]
    diff = match["home_score"] - match["away_score"]

    if diff > 0:
        _award_match_win(league_chat_id, home, diff, match_id, awarded)
    elif diff < 0:
        _award_match_win(league_chat_id, away, -diff, match_id, awarded)
    # diff == 0: draw — no one scores.

    return awarded


def _award_match_win(
    league_chat_id: int,
    country: str,
    points: int,
    match_id: int,
    awarded: list[tuple[str, str, int]],
) -> None:
    """Award goal-differential match points to `country`. Idempotent per match."""
    owner = db.owner_of_country(league_chat_id, country)
    if not owner:
        return  # nobody drafted this country in this league
    if db.has_event(league_chat_id, country, "match_win", match_id=match_id):
        return
    db.add_event(league_chat_id, owner["id"], country, "match_win", points, match_id)
    awarded.append(
        (owner["display_name"], f"{country} match_win (+{points} goal diff)", points)
    )


def award_stage_reached(
    league_chat_id: int,
    country: str,
    stage_keyword: str,
) -> Optional[tuple[str, str, int]]:
    """
    Award the stage-advancement bonus for one country.
    Returns (display_name, event_type, points) if awarded, or None if already
    awarded or the country has no owner in this league.
    Raises KeyError if stage_keyword is unknown.
    """
    event_type, points = STAGE_REACH_EVENT[stage_keyword]
    owner = db.owner_of_country(league_chat_id, country)
    if not owner:
        return None
    if db.has_event(league_chat_id, country, event_type, match_id=None):
        return None
    db.add_event(league_chat_id, owner["id"], country, event_type, points, None)
    return (owner["display_name"], event_type, points)


def score_match_for_all_leagues(match: dict) -> dict[int, list[tuple[str, str, int]]]:
    """
    Apply match scoring across every active league. Returns
    {chat_id: [(name, desc, pts), ...]} so the cron caller can post a per-league
    summary to each chat.
    """
    out: dict[int, list[tuple[str, str, int]]] = {}
    for league in db.all_leagues():
        results = score_match_for_league(league["chat_id"], match)
        if results:
            out[league["chat_id"]] = results
    return out
