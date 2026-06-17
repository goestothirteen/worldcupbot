"""
Team Duel — pure game logic. No DB, no Telegram. Mirrors draft.py's style:
everything here is deterministic and unit-testable.

Two modes:

  HANGMAN — the bot picks a secret football word. The two duelists alternate
    /guess <letter>. A correct guess reveals letters and the SAME player keeps
    the turn (reward); a wrong guess adds a body part and passes the turn. The
    duelist who reveals the final letter WINS. If the gallows completes
    (MAX_WRONG wrong guesses), the duelist who made that final wrong guess
    LOSES (the other wins).

  TRIVIA — best-of-N football questions. The bot posts one question at a time;
    both duelists race to /answer. First correct answer wins the question.
    Wrong answers cost nothing — keep trying until someone gets it. First to
    WIN_SCORE questions wins the duel.

The game `state` is a plain JSON-serialisable dict so the bot can persist it in
a single TEXT column on the duels row and reload it on the next command. These
functions take a state dict + an action and return a small result dict; they
never mutate global state.

Player identity inside state uses the integer player_id (our DB players.id),
stored as a plain int. Scores dict keys are str(player_id) because JSON object
keys must be strings — helpers below handle the conversion.
"""

from __future__ import annotations

import random
import unicodedata
from typing import Optional

from app.duel_banks import HANGMAN_WORDS, TRIVIA

MAX_WRONG = 6          # hangman: 6 wrong guesses completes the gallows
TRIVIA_QUESTIONS = 5   # questions drawn per trivia duel
WIN_SCORE = 3          # first to 3 correct wins a best-of-5 trivia duel

MODES = ("hangman", "trivia")


# ── Shared helpers ──────────────────────────────────────────────────────────

def normalize(s: str) -> str:
    """Canonicalise a free-text answer / letter for comparison:
    lowercase, strip accents, drop anything that isn't a-z/0-9/space, and
    collapse whitespace. 'Côte d'Ivoire' -> 'cote divoire', 'FRANCE ' -> 'france'."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    out = []
    for ch in s:
        if ch.isalnum():
            out.append(ch)
        elif ch.isspace():
            out.append(" ")
        # else: drop punctuation
    return " ".join("".join(out).split())


def other_player(state: dict, player_id: int) -> int:
    """The opponent of player_id within a duel state."""
    a, b = state["challenger_id"], state["opponent_id"]
    return b if player_id == a else a


# ── Hangman ──────────────────────────────────────────────────────────────────

def new_hangman_state(challenger_id: int, opponent_id: int,
                      word: Optional[str] = None) -> dict:
    """Initialise a hangman duel. Challenger guesses first."""
    chosen = (word or random.choice(HANGMAN_WORDS)).upper()
    return {
        "mode": "hangman",
        "challenger_id": challenger_id,
        "opponent_id": opponent_id,
        "word": chosen,
        "revealed": [],          # correctly-guessed letters
        "wrong": [],             # wrong letters, in order
        "turn": challenger_id,   # whose /guess we're waiting on
    }


def hangman_masked(state: dict) -> str:
    """Render the word with unrevealed letters as '_', spaced for readability."""
    revealed = set(state["revealed"])
    return " ".join(ch if ch in revealed else "_" for ch in state["word"])


def hangman_is_solved(state: dict) -> bool:
    return all(ch in set(state["revealed"]) for ch in state["word"])


def hangman_gallows(wrong_count: int) -> str:
    """ASCII gallows for 0..MAX_WRONG wrong guesses. Wrap in <pre> for Telegram."""
    stages = [
        "  +---+\n  |   |\n      |\n      |\n      |\n      |\n=========",
        "  +---+\n  |   |\n  O   |\n      |\n      |\n      |\n=========",
        "  +---+\n  |   |\n  O   |\n  |   |\n      |\n      |\n=========",
        "  +---+\n  |   |\n  O   |\n /|   |\n      |\n      |\n=========",
        "  +---+\n  |   |\n  O   |\n /|\\  |\n      |\n      |\n=========",
        "  +---+\n  |   |\n  O   |\n /|\\  |\n /    |\n      |\n=========",
        "  +---+\n  |   |\n  O   |\n /|\\  |\n / \\  |\n      |\n=========",
    ]
    return stages[max(0, min(wrong_count, MAX_WRONG))]


def hangman_guess(state: dict, player_id: int, letter: str) -> dict:
    """
    Apply a /guess. Returns a result dict:
      {"status": <str>, "state": <dict>, ...}
    status is one of:
      "not_turn"   — it's the other player's turn (state unchanged)
      "invalid"    — not a single A-Z letter (state unchanged)
      "repeat"     — that letter was already tried (state unchanged)
      "hit"        — correct letter, same player continues
      "miss"       — wrong letter, turn passes
      "win"        — that guess completed the word; `winner` set
      "lose"       — that wrong guess completed the gallows; `winner` (the
                     OTHER player) set, `loser` = player_id
    On terminal results the result dict also carries "winner" and "loser".
    """
    if player_id != state["turn"]:
        return {"status": "not_turn", "state": state}

    norm = normalize(letter)
    if len(norm) != 1 or not ("a" <= norm <= "z"):
        return {"status": "invalid", "state": state}
    up = norm.upper()

    if up in state["revealed"] or up in state["wrong"]:
        return {"status": "repeat", "state": state}

    if up in state["word"]:
        state["revealed"].append(up)
        if hangman_is_solved(state):
            return {"status": "win", "state": state,
                    "winner": player_id, "loser": other_player(state, player_id)}
        # correct → keep the turn
        return {"status": "hit", "state": state}
    else:
        state["wrong"].append(up)
        if len(state["wrong"]) >= MAX_WRONG:
            return {"status": "lose", "state": state,
                    "winner": other_player(state, player_id), "loser": player_id}
        # wrong → pass the turn
        state["turn"] = other_player(state, player_id)
        return {"status": "miss", "state": state}


# ── Trivia ───────────────────────────────────────────────────────────────────

def new_trivia_state(challenger_id: int, opponent_id: int,
                     question_indices: Optional[list[int]] = None) -> dict:
    """Initialise a trivia duel with a random sample of questions."""
    if question_indices is None:
        n = min(TRIVIA_QUESTIONS, len(TRIVIA))
        question_indices = random.sample(range(len(TRIVIA)), n)
    return {
        "mode": "trivia",
        "challenger_id": challenger_id,
        "opponent_id": opponent_id,
        "questions": list(question_indices),
        "current": 0,
        "scores": {str(challenger_id): 0, str(opponent_id): 0},
    }


def trivia_current_question(state: dict) -> Optional[dict]:
    """The bank entry for the current question, or None if the duel is over."""
    i = state["current"]
    if i >= len(state["questions"]):
        return None
    return TRIVIA[state["questions"][i]]


def trivia_score(state: dict, player_id: int) -> int:
    return state["scores"].get(str(player_id), 0)


def _trivia_winner(state: dict) -> Optional[int]:
    """player_id who has reached WIN_SCORE, or None."""
    for k, v in state["scores"].items():
        if v >= WIN_SCORE:
            return int(k)
    return None


def trivia_answer(state: dict, player_id: int, answer: str) -> dict:
    """
    Apply an /answer to the current question. Returns a result dict:
      status one of:
        "over"     — no current question (duel already finished)
        "wrong"    — answer didn't match (state unchanged)
        "correct"  — point awarded; `scorer` set, `canonical` = right answer,
                     `next_question` = the next bank entry or None
        "win"      — point awarded AND reached WIN_SCORE; `winner` + `loser` set
    """
    # Already decided (someone hit WIN_SCORE) or out of questions.
    if _trivia_winner(state) is not None:
        return {"status": "over", "state": state}
    q = trivia_current_question(state)
    if q is None:
        return {"status": "over", "state": state}

    given = normalize(answer)
    if not given:
        return {"status": "wrong", "state": state}

    accepted = {normalize(a) for a in q["answers"]}
    if given not in accepted:
        return {"status": "wrong", "state": state}

    # Correct.
    key = str(player_id)
    state["scores"][key] = state["scores"].get(key, 0) + 1
    state["current"] += 1
    canonical = q["answers"][0]

    winner = _trivia_winner(state)
    if winner is not None:
        return {"status": "win", "state": state,
                "winner": winner, "loser": other_player(state, winner),
                "canonical": canonical}

    if trivia_current_question(state) is None:
        # Ran out of questions without anyone hitting WIN_SCORE — decide on
        # score, breaking a tie in favour of the challenger (who staked first).
        a = state["challenger_id"]
        b = state["opponent_id"]
        winner = a if trivia_score(state, a) >= trivia_score(state, b) else b
        return {"status": "win", "state": state,
                "winner": winner, "loser": other_player(state, winner),
                "canonical": canonical, "exhausted": True}

    return {"status": "correct", "state": state,
            "scorer": player_id, "canonical": canonical,
            "next_question": trivia_current_question(state)}
