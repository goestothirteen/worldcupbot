"""
Static content banks for the Team Duel mini-games.

Kept separate from app/duel.py so the game *logic* stays small and testable,
and so adding more words / questions is a one-file edit with no risk of
touching the state machines.

HANGMAN_WORDS
  Football-themed words, UPPERCASE, A-Z only (no spaces, digits or accents).
  Deliberately long/compound for a real challenge — duel.py asserts the shape
  at import time, and _validate() below also enforces a minimum length.

TRIVIA
  A list of {"q": <question>, "answers": [<accepted answer>, ...]}.
  The FIRST entry in "answers" is the canonical answer shown when a round
  ends. The rest are accepted aliases. All answers are matched after
  normalization (see duel.normalize): lowercased, accents stripped,
  punctuation removed, whitespace collapsed — so you don't need to list
  case/accent variants here.
"""

from __future__ import annotations

MIN_WORD_LEN = 8  # keep hangman challenging

# ── Hangman ────────────────────────────────────────────────────────────────
# Long and compound football vocabulary. All >= MIN_WORD_LEN letters.

HANGMAN_WORDS: list[str] = [
    "GOALKEEPER", "MIDFIELDER", "DEFENDER", "ATTACKER", "PLAYMAKER",
    "SUBSTITUTE", "SUBSTITUTION", "TOURNAMENT", "QUARTERFINAL", "SEMIFINAL",
    "GOALSCORER", "EQUALISER", "COUNTERATTACK", "POSSESSION", "FORMATION",
    "CENTREBACK", "FULLBACK", "WINGBACK", "STOPPAGE", "INJURYTIME",
    "EXTRATIME", "PENALTYBOX", "FREEKICK", "CORNERKICK", "HANDBALL",
    "CLEARANCE", "INTERCEPTION", "DEFLECTION", "CROSSBAR", "GOALPOST",
    "GOALLINE", "HALFTIME", "LINESMAN", "DRIBBLING", "TACKLING",
    "PRESSING", "HATTRICK", "KNOCKOUT", "CHAMPION", "CHAMPIONS",
    "QUALIFIER", "QUALIFICATION", "GROUPSTAGE", "AGGREGATE", "SHOOTOUT",
    "GOLDENBOOT", "GOLDENBALL", "GOLDENGLOVE", "WORLDCUP", "NATIONALTEAM",
    "INTERNATIONAL", "ATTACKING", "DEFENDING", "SUPPORTERS", "CELEBRATION",
    "DISALLOWED", "ADVANTAGE", "STALEMATE", "BREAKAWAY", "OVERLAPPING",
    "VOLLEYED", "DEFENSIVE", "OFFENSIVE", "TECHNICAL", "TACTICAL",
    "SCISSORKICK", "BICYCLEKICK", "LONGRANGE", "THROUGHBALL", "MANAGERIAL",
    "RELEGATION", "CONSOLATION", "GROUNDSMAN", "TURNAROUND", "SUPERSUB",
]

# ── Trivia ─────────────────────────────────────────────────────────────────
# Harder, deeper-cut World Cup questions. Each answer set kept short and
# forgiving (aliases for the common spellings).

TRIVIA: list[dict] = [
    {"q": "Who scored the fastest goal in World Cup history (about 11 seconds, in 2002)?",
     "answers": ["hakan sukur", "sukur", "hakan"]},
    {"q": "Who is the only player to win the World Cup three times (1958, 1962, 1970)?",
     "answers": ["pele"]},
    {"q": "Which national team has lost the most World Cup finals (four)?",
     "answers": ["germany", "west germany"]},
    {"q": "How many goals did Just Fontaine score at the 1958 World Cup, still a single-tournament record?",
     "answers": ["13", "thirteen"]},
    {"q": "Which country beat defending champions Spain 5-1 in the 2014 group stage?",
     "answers": ["netherlands", "holland", "the netherlands"]},
    {"q": "Germany beat Brazil 7-1 in the semi-final of which World Cup?",
     "answers": ["2014"]},
    {"q": "Which was the first African nation to reach a World Cup quarter-final, in 1990?",
     "answers": ["cameroon"]},
    {"q": "Who was the top scorer (Golden Boot) at the 2018 World Cup?",
     "answers": ["harry kane", "kane"]},
    {"q": "Which goalkeeper captained Spain to their 2010 World Cup title?",
     "answers": ["iker casillas", "casillas"]},
    {"q": "Which country did Zinedine Zidane score twice against in the 1998 final?",
     "answers": ["brazil"]},
    {"q": "In which year did the World Cup first use VAR (video assistant referee)?",
     "answers": ["2018"]},
    {"q": "Which player has appeared in the most World Cup matches (26, by the end of 2022)?",
     "answers": ["lionel messi", "messi"]},
    {"q": "Which country hosted and won the 1978 World Cup?",
     "answers": ["argentina"]},
    {"q": "Which country finished third (won the bronze) at the 2022 World Cup?",
     "answers": ["croatia"]},
    {"q": "Which country hosted the 1970 World Cup, the first held outside Europe and South America's usual rotation that decade?",
     "answers": ["mexico"]},
    {"q": "Lothar Matthäus held the record for most World Cup appearances before which Argentine broke it?",
     "answers": ["lionel messi", "messi"]},
    {"q": "Which nation knocked England out of the 1986 World Cup via the 'Hand of God'?",
     "answers": ["argentina"]},
    {"q": "Who captained France to the 1998 title and then coached them to the 2018 title?",
     "answers": ["didier deschamps", "deschamps"]},
    {"q": "Which stadium hosted the 2014 World Cup final in Rio de Janeiro?",
     "answers": ["maracana", "the maracana"]},
    {"q": "Which country eliminated the Netherlands in the 2014 semi-final on penalties?",
     "answers": ["argentina"]},
    {"q": "Roger Milla famously danced at the corner flag for which country at the 1990 World Cup?",
     "answers": ["cameroon"]},
    {"q": "Which Italian goalkeeper won the Golden Glove and the title in 2006?",
     "answers": ["gianluigi buffon", "buffon"]},
    {"q": "Which country won the very first World Cup in 1930?",
     "answers": ["uruguay"]},
    {"q": "Diego Maradona's 'Goal of the Century' in 1986 was scored against which country?",
     "answers": ["england"]},
    {"q": "Which South Korean co-hosts reached the semi-finals of the 2002 World Cup?",
     "answers": ["south korea", "korea republic", "korea"]},
    {"q": "Which country won the 2006 World Cup, beating France in a penalty shootout?",
     "answers": ["italy"]},
    {"q": "Which country has appeared in every World Cup finals tournament?",
     "answers": ["brazil"]},
    {"q": "How many minutes long is each half of extra time?",
     "answers": ["15", "fifteen"]},
    {"q": "Mario Götze scored the winning goal in which World Cup final?",
     "answers": ["2014"]},
    {"q": "Which country did Portugal beat 6-1 in the 2022 World Cup round of 16?",
     "answers": ["switzerland"]},
    {"q": "Andrés Iniesta scored the winning goal in which World Cup final?",
     "answers": ["2010"]},
    {"q": "Which African country reached the semi-finals of the 2022 World Cup?",
     "answers": ["morocco"]},
    {"q": "Which player won the Golden Ball (best player) at the 2014 World Cup?",
     "answers": ["lionel messi", "messi"]},
    {"q": "Geoff Hurst is the only player to score a hat-trick in a World Cup final — in which year?",
     "answers": ["1966"]},
]


def _validate() -> None:
    for w in HANGMAN_WORDS:
        assert w.isalpha() and w.isupper(), f"bad hangman word: {w!r}"
        assert len(w) >= MIN_WORD_LEN, f"hangman word too short: {w!r}"
    seen = set()
    for entry in TRIVIA:
        assert entry["q"] and entry["answers"], f"bad trivia entry: {entry!r}"
        assert entry["q"] not in seen, f"duplicate question: {entry['q']!r}"
        seen.add(entry["q"])


_validate()
