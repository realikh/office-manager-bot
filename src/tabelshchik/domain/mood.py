"""The bot's moods.

Табельщик has a voice, and it is mostly a bad one. The mood colours anything the bot says
in its own voice; it never touches the facts — the tagged roster, the dates and the
workbook come out the same whatever mood is running.

One mood per office per day. Daily rather than per message because a bot that swings from
bleak to chipper inside one thread reads as broken, whereas a mood that persists for a day
reads as a character.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from enum import StrEnum

from tabelshchik.domain.rng import stable_hash


class Mood(StrEnum):
    TOXIC = "toxic"
    FUN = "fun"
    HAPPY = "happy"
    SAD = "sad"
    DEPRESSIVE = "depressive"


#: What `safeMode` forces, and what anything unparseable falls back to.
SAFE_MOOD = Mood.FUN

#: Mostly toxic, as asked for. `sad` and `depressive` are deliberately rare: they read as
#: flat rather than funny, and a bot that lands there one day in five comes across as
#: miserable rather than as having a personality.
DEFAULT_WEIGHTS: Mapping[Mood, int] = {
    Mood.TOXIC: 55,
    Mood.FUN: 25,
    Mood.HAPPY: 12,
    Mood.SAD: 5,
    Mood.DEPRESSIVE: 3,
}


def pick_mood(
    weights: Mapping[Mood, int],
    *,
    office_id: str,
    day: date,
    nonce: int = 0,
) -> Mood:
    """Choose the day's mood, deterministically.

    Same office and same day always gives the same answer, so a restart mid-afternoon
    does not change the bot's personality halfway through.
    """
    usable = {mood: weight for mood, weight in weights.items() if weight > 0}
    if not usable:
        return SAFE_MOOD

    total = sum(usable.values())
    roll = stable_hash("mood", office_id, day.isoformat(), nonce) % total

    for mood in sorted(usable, key=lambda item: item.value):
        roll -= usable[mood]
        if roll < 0:
            return mood

    return SAFE_MOOD


def variant_index(*, office_id: str, day: date, kind: str, count: int, nonce: int = 0) -> int:
    """Pick one of several hand-written lines, stably for the day."""
    if count <= 0:
        raise ValueError("no variants to choose from")
    return stable_hash("variant", office_id, day.isoformat(), kind, nonce) % count
