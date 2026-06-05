"""
Snake draft logic — pure functions, no DB or Telegram dependencies.

Rules:
  * N players, randomized into draft order [p1, p2, ..., pN].
  * Round 1: picks 1..N go p1, p2, ..., pN.
  * Round 2: picks N+1..2N go pN, pN-1, ..., p1 (reversed).
  * Round 3: same as round 1. Etc.
  * Continues until all 48 countries are picked, even if the last round
    is partial (e.g. 5 players × 9 full rounds = 45, then 3 leftover picks
    in round 10 go to p1, p2, p3 only).

The snake pattern ensures that whoever picks last in round 1 picks first
in round 2 — the standard fantasy-draft fairness mechanism.
"""

from __future__ import annotations

from typing import Optional

TOTAL_COUNTRIES = 48


def player_for_pick(pick_number: int, num_players: int) -> int:
    """
    Returns the 1-indexed draft-order position whose turn it is at pick_number.
    pick_number is also 1-indexed.

    Examples (num_players=5):
      pick 1  -> position 1   (round 1, forward)
      pick 5  -> position 5
      pick 6  -> position 5   (round 2, reversed)
      pick 10 -> position 1
      pick 11 -> position 1   (round 3, forward again)
    """
    assert pick_number >= 1
    assert num_players >= 1
    round_idx = (pick_number - 1) // num_players          # 0-indexed round
    pos_in_round = (pick_number - 1) % num_players        # 0-indexed
    if round_idx % 2 == 0:
        return pos_in_round + 1
    else:
        return num_players - pos_in_round


def draft_complete(pick_number: int) -> bool:
    """All 48 countries have been picked once pick_number exceeds 48."""
    return pick_number > TOTAL_COUNTRIES


def picks_remaining(pick_number: int) -> int:
    return max(0, TOTAL_COUNTRIES - (pick_number - 1))


def round_of(pick_number: int, num_players: int) -> int:
    """1-indexed round number."""
    return (pick_number - 1) // num_players + 1


def position_in_round(pick_number: int, num_players: int) -> int:
    """1-indexed seat within the current round."""
    return (pick_number - 1) % num_players + 1


def picks_per_player(num_players: int) -> dict[int, int]:
    """
    How many picks each draft-order position ends up with at the end of the draft.
    For 5 players × 48 teams: positions 1,2,3 get 10 each, positions 4,5 get 9 each.

    The math: full rounds floor(48/N), then the last partial round of (48 mod N)
    picks. In odd-numbered partial rounds positions 1..M get the extra pick;
    in even-numbered partial rounds positions (N-M+1)..N get the extra pick.
    """
    base = TOTAL_COUNTRIES // num_players
    extra = TOTAL_COUNTRIES % num_players
    out = {pos: base for pos in range(1, num_players + 1)}
    if extra:
        last_round = round_of(TOTAL_COUNTRIES, num_players)
        if last_round % 2 == 1:
            # forward direction — positions 1..extra get the extra pick
            for pos in range(1, extra + 1):
                out[pos] += 1
        else:
            # reversed direction — positions (N-extra+1)..N get the extra pick
            for pos in range(num_players - extra + 1, num_players + 1):
                out[pos] += 1
    return out
