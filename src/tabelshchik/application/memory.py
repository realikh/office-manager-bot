"""What the bot chooses to remember between messages.

Notes in two scopes. ``person`` notes belong to one Telegram account and are shown only
to that person; ``office`` notes are shared by everyone in that office. Each note has a
topic, and writing the same topic again updates it, which is how a fact stays current.

Everything in this module is pure, so the rules about what is worth keeping can be
tested without a database or a model.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from tabelshchik.application.ports import Note, NoteScope

TOPIC_CHARS = 40
MAX_FORGETS = 3
LONGEST_UNTIL = timedelta(days=365)

_ABOUT = {"me": NoteScope.PERSON, "user": NoteScope.PERSON, "office": NoteScope.OFFICE}
_NEAR_IDENTICAL = 0.9

_WORD = re.compile(r"[\w']+", re.UNICODE)
_NUMBER = re.compile(r"\d+")
_TAG = re.compile(r"<[^>]*>?")
_LINK = re.compile(r"https?://\S+|www\.\S+|\S+\.(?:com|ru|kz|org|net|io)\b\S*", re.IGNORECASE)
_HANDLE = re.compile(r"@\w+")


@dataclass(frozen=True, slots=True)
class NoteWrite:
    scope: NoteScope
    topic: str
    text: str
    until: date | None = None


@dataclass(frozen=True, slots=True)
class NoteForget:
    scope: NoteScope
    topic: str


def parse_writes(payload: object, *, limit: int, max_chars: int, today: date) -> list[NoteWrite]:
    if not isinstance(payload, list) or limit <= 0:
        return []
    writes: list[NoteWrite] = []
    for item in payload:
        if len(writes) >= limit:
            break
        if not isinstance(item, dict):
            continue
        scope = _scope(item.get("about"))
        topic = normalise_topic(str(item.get("topic") or ""))
        text = tidy(str(item.get("text") or ""), max_chars=max_chars)
        if scope is None or not topic or not text:
            continue
        writes.append(NoteWrite(scope, topic, text, _until(item.get("until"), today)))
    return writes


def parse_forgets(payload: object, *, limit: int = MAX_FORGETS) -> list[NoteForget]:
    if not isinstance(payload, list) or limit <= 0:
        return []
    forgets: list[NoteForget] = []
    for item in payload:
        if len(forgets) >= limit:
            break
        if not isinstance(item, dict):
            continue
        scope = _scope(item.get("about"))
        topic = normalise_topic(str(item.get("topic") or ""))
        if scope is not None and topic:
            forgets.append(NoteForget(scope, topic))
    return forgets


def normalise_topic(raw: str) -> str:
    topic = ""
    for word in _WORD.findall(_TAG.sub(" ", raw).casefold()):
        candidate = f"{topic} {word}".strip()
        if len(candidate) > TOPIC_CHARS:
            break
        topic = candidate
    return topic


def tidy(raw: str, *, max_chars: int) -> str:
    text = _TAG.sub(" ", raw)
    text = _LINK.sub(" ", text)
    text = _HANDLE.sub(" ", text)
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    clipped = text[:max_chars].rsplit(" ", 1)[0]
    return (clipped or text[:max_chars]).rstrip(" ,;:-") + "…"


def is_near_identical(text: str, existing: Iterable[str]) -> bool:
    words = _words(text)
    if not words:
        return True
    numbers = sorted(_NUMBER.findall(text))
    for other in existing:
        theirs = _words(other)
        union = words | theirs
        if not union or sorted(_NUMBER.findall(other)) != numbers:
            continue
        if len(words & theirs) / len(union) >= _NEAR_IDENTICAL:
            return True
    return False


def render_note(note: Note, months: Sequence[str]) -> str:
    written = note.updated_at.date()
    line = f"- {note.topic}: {note.text} (записано {_long(written, months)}"
    if note.until is not None:
        line += f", до {_long(note.until, months)}"
    return line + ")"


def _scope(raw: object) -> NoteScope | None:
    return _ABOUT.get(str(raw or "").strip().lower())


def _until(raw: object, today: date) -> date | None:
    if not isinstance(raw, str):
        return None
    try:
        until = date.fromisoformat(raw.strip())
    except ValueError:
        return None
    return until if today <= until <= today + LONGEST_UNTIL else None


def _words(text: str) -> frozenset[str]:
    return frozenset(match.group().casefold() for match in _WORD.finditer(text))


def _long(day: date, months: Sequence[str]) -> str:
    return f"{day.day} {months[day.month - 1]} {day.year}"
