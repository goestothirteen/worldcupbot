"""
2026 FIFA World Cup — the 48 qualified teams + group assignments.

Source: the official draw held on December 5, 2025 in Washington, D.C.
The groups below match the published draw. If any team is later replaced
(injury withdrawals, FIFA sanctions, etc.) update this file and `docker
compose restart worldcup_bot`.

Each country has:
  code         — lowercase ASCII key used everywhere internally (DB, /pick arg).
                 Stable forever; do not rename after a draft has started.
  display_name — what users see in messages.
  flag         — emoji flag for prettier output. Optional; only used in display.
  group        — group letter A–L.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Country:
    code: str
    display_name: str
    flag: str
    group: str


# 12 groups × 4 teams = 48 nations. Order within a group matches the draw seed.
COUNTRIES: list[Country] = [
    # ── Group A ──
    Country("mexico",                "Mexico",                   "🇲🇽", "A"),
    Country("south_africa",          "South Africa",             "🇿🇦", "A"),
    Country("korea_republic",        "Korea Republic",           "🇰🇷", "A"),
    Country("czechia",               "Czechia",                  "🇨🇿", "A"),

    # ── Group B ──
    Country("canada",                "Canada",                   "🇨🇦", "B"),
    Country("bosnia_herzegovina",    "Bosnia and Herzegovina",   "🇧🇦", "B"),
    Country("qatar",                 "Qatar",                    "🇶🇦", "B"),
    Country("switzerland",           "Switzerland",              "🇨🇭", "B"),

    # ── Group C ──
    Country("brazil",                "Brazil",                   "🇧🇷", "C"),
    Country("morocco",               "Morocco",                  "🇲🇦", "C"),
    Country("scotland",              "Scotland",                 "🏴󠁧󠁢󠁳󠁣󠁴󠁿", "C"),
    Country("haiti",                 "Haiti",                    "🇭🇹", "C"),

    # ── Group D ──
    Country("united_states",         "United States",            "🇺🇸", "D"),
    Country("australia",             "Australia",                "🇦🇺", "D"),
    Country("paraguay",              "Paraguay",                 "🇵🇾", "D"),
    Country("turkiye",               "Türkiye",                  "🇹🇷", "D"),

    # ── Group E ──
    Country("germany",               "Germany",                  "🇩🇪", "E"),
    Country("ecuador",               "Ecuador",                  "🇪🇨", "E"),
    Country("ivory_coast",           "Côte d'Ivoire",            "🇨🇮", "E"),
    Country("curacao",               "Curaçao",                  "🇨🇼", "E"),

    # ── Group F ──
    Country("netherlands",           "Netherlands",              "🇳🇱", "F"),
    Country("japan",                 "Japan",                    "🇯🇵", "F"),
    Country("tunisia",               "Tunisia",                  "🇹🇳", "F"),
    Country("sweden",                "Sweden",                   "🇸🇪", "F"),

    # ── Group G ──
    Country("belgium",               "Belgium",                  "🇧🇪", "G"),
    Country("iran",                  "Iran",                     "🇮🇷", "G"),
    Country("egypt",                 "Egypt",                    "🇪🇬", "G"),
    Country("new_zealand",           "New Zealand",              "🇳🇿", "G"),

    # ── Group H ──
    Country("spain",                 "Spain",                    "🇪🇸", "H"),
    Country("uruguay",               "Uruguay",                  "🇺🇾", "H"),
    Country("saudi_arabia",          "Saudi Arabia",             "🇸🇦", "H"),
    Country("cape_verde",            "Cape Verde",               "🇨🇻", "H"),

    # ── Group I ──
    Country("france",                "France",                   "🇫🇷", "I"),
    Country("norway",                "Norway",                   "🇳🇴", "I"),
    Country("senegal",               "Senegal",                  "🇸🇳", "I"),
    Country("iraq",                  "Iraq",                     "🇮🇶", "I"),

    # ── Group J ──
    Country("argentina",             "Argentina",                "🇦🇷", "J"),
    Country("austria",               "Austria",                  "🇦🇹", "J"),
    Country("algeria",               "Algeria",                  "🇩🇿", "J"),
    Country("jordan",                "Jordan",                   "🇯🇴", "J"),

    # ── Group K ──
    Country("portugal",              "Portugal",                 "🇵🇹", "K"),
    Country("colombia",              "Colombia",                 "🇨🇴", "K"),
    Country("uzbekistan",            "Uzbekistan",               "🇺🇿", "K"),
    Country("dr_congo",              "DR Congo",                 "🇨🇩", "K"),

    # ── Group L ──
    Country("england",               "England",                  "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "L"),
    Country("croatia",               "Croatia",                  "🇭🇷", "L"),
    Country("ghana",                 "Ghana",                    "🇬🇭", "L"),
    Country("panama",                "Panama",                   "🇵🇦", "L"),
]

assert len(COUNTRIES) == 48, f"Expected 48 teams, got {len(COUNTRIES)}"


# ── Lookups ────────────────────────────────────────────────────────────────
BY_CODE: dict[str, Country] = {c.code: c for c in COUNTRIES}

# Common aliases users will type. Lowercase keys, value is the canonical code.
# Keep this generous — better to accept "korea" than make a user type "korea_republic".
ALIASES: dict[str, str] = {
    "usa": "united_states",
    "us": "united_states",
    "america": "united_states",
    "korea": "korea_republic",
    "south_korea": "korea_republic",
    "skorea": "korea_republic",
    "ivory coast": "ivory_coast",
    "cote d'ivoire": "ivory_coast",
    "côte d'ivoire": "ivory_coast",
    "ivory": "ivory_coast",
    "bosnia": "bosnia_herzegovina",
    "bih": "bosnia_herzegovina",
    "drc": "dr_congo",
    "congo": "dr_congo",
    "drcongo": "dr_congo",
    "saudi": "saudi_arabia",
    "ksa": "saudi_arabia",
    "turkey": "turkiye",
    "türkiye": "turkiye",
    "czech": "czechia",
    "czech_republic": "czechia",
    "nz": "new_zealand",
    "netherlands": "netherlands",
    "holland": "netherlands",
    "nederlands": "netherlands",
    "south africa": "south_africa",
    "rsa": "south_africa",
    "cape verde": "cape_verde",
    "new zealand": "new_zealand",
    "dr congo": "dr_congo",
}


def resolve(name: str) -> Optional[Country]:
    """
    Resolve a user-typed country name to a Country object.
    Accepts code form ("brazil"), display name ("Brazil"), or any alias above.
    Case-insensitive, whitespace + dashes normalized to underscore.
    Returns None if no match.
    """
    key = name.strip().lower().replace("-", "_").replace(" ", "_")
    if key in BY_CODE:
        return BY_CODE[key]
    # Also try the raw (with spaces) form against aliases — handles "ivory coast".
    raw = name.strip().lower()
    if raw in ALIASES:
        return BY_CODE[ALIASES[raw]]
    if key in ALIASES:
        return BY_CODE[ALIASES[key]]
    # Display-name match (case-insensitive).
    for c in COUNTRIES:
        if c.display_name.lower() == name.strip().lower():
            return c
    return None


def by_group() -> dict[str, list[Country]]:
    """Returns {group_letter: [countries in that group]}."""
    out: dict[str, list[Country]] = {}
    for c in COUNTRIES:
        out.setdefault(c.group, []).append(c)
    return out
