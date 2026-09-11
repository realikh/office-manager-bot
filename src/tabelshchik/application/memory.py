"""What the bot chooses to remember between messages.

Two scopes. ``office`` facts are shared by everyone in a chat; ``employee`` facts are
shown only to the person they are about. Both are deliberately small: the point of a
memory here is to stop the bot re-asking what it was told a minute ago, not to build a
profile of anybody.

Everything in this module is pure, so the rules about what is worth keeping — and what is
merely the same thing said twice — can be tested without a database or a model.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

OFFICE = "office"
EMPLOYEE = "employee"

#: What the model is allowed to say in the `scope` field, and what we store it as. A null
#: or unrecognised scope means "not worth keeping", which is the common case.
_SCOPES = {"office": OFFICE, "general": OFFICE, "user": EMPLOYEE, "employee": EMPLOYEE}

#: Above this, two facts are treated as the same fact and the newer one wins. Chosen to
#: catch rephrasing ("Алик сидит у окна" / "Алик сидит возле окна") without merging two
#: genuinely different statements that happen to share a subject.
_SIMILARITY = 0.6

_WORD = re.compile(r"[\w']+", re.UNICODE)
_TAG = re.compile(r"<[^>]*>?")


@dataclass(frozen=True, slots=True)
class Remembered:
    """One fact the model asked to keep, already cleaned and scoped."""

    scope: str
    fact: str


def distil(payload: object, *, max_chars: int) -> Remembered | None:
    """Read the model's `remember` field, or decide there is nothing to keep.

    Returns None far more often than not, which is correct: most messages in a work chat
    are not worth a line of memory, and a bot that remembers everything is both expensive
    and unsettling.
    """
    if not isinstance(payload, dict):
        return None

    scope = _SCOPES.get(str(payload.get("scope", "")).strip().lower())
    if scope is None:
        return None

    fact = _tidy(str(payload.get("fact", "")), max_chars=max_chars)
    return Remembered(scope=scope, fact=fact) if fact else None


def _tidy(raw: str, *, max_chars: int) -> str:
    """One line, no markup, no run-on. Truncation is on a word boundary."""
    # Whole tags, not just the angle brackets: dropping the brackets alone would leave
    # "b...\/b" behind in a fact that is read back into a later prompt.
    text = _TAG.sub(" ", raw)
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    clipped = text[:max_chars].rsplit(" ", 1)[0]
    return (clipped or text[:max_chars]).rstrip(" ,;:-") + "…"


def is_redundant(fact: str, existing: Iterable[str]) -> bool:
    """True when we already know this, in whatever words it was put the first time."""
    words = _words(fact)
    if not words:
        return True
    return any(_overlap(words, _words(other)) >= _SIMILARITY for other in existing)


def _words(text: str) -> frozenset[str]:
    return frozenset(match.group().casefold() for match in _WORD.finditer(text))


def _overlap(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    # Against the smaller set rather than the union, so "Алик сидит у окна" still matches
    # "Алик сидит у окна и не любит созвоны" — the longer one restates the shorter.
    return len(left & right) / min(len(left), len(right))


def render(general: Sequence[str], personal: Sequence[str]) -> str:
    """The memory block as it appears in the prompt, or nothing at all.

    Oldest first, so the most recent thing the bot was told reads last and nearest to the
    question — which is the half that usually matters.
    """
    blocks: list[str] = []
    if general:
        blocks.append("Что ты знаешь об этом чате:\n" + _bullets(general))
    if personal:
        blocks.append("Что ты знаешь о собеседнике:\n" + _bullets(personal))
    return "\n\n".join(blocks)


def _bullets(facts: Sequence[str]) -> str:
    return "\n".join(f"- {fact}" for fact in facts)
