"""Generating the ids that end up inside callback data.

Shared by the roster and by offices, and parameterised by length because their budgets
differ. Telegram caps callback data at 64 bytes in total, and the tightest string in the
admin UI carries an id next to a fixed prefix — so the cap on an id is not cosmetic, it
is the difference between a button that works and one that silently does nothing.
"""

from __future__ import annotations

import re
import unicodedata

#: A person's id. Well under the 63 the config schema allows, so a generated id can never
#: be the thing that makes a button stop working.
MAX_ID_LENGTH = 40

#: An office's id, which is shorter because it rides *alongside* another id or a weekday
#: rather than alone. See the byte table beside the callback prefixes in `routers/admin`.
MAX_OFFICE_ID_LENGTH = 24

_SEPARATORS = re.compile(r"[^a-z0-9]+")

#: Cyrillic to Latin, so "Жания Жакипова" becomes a readable slug rather than an empty
#: string. Practical transliteration, not a standard: this only has to produce a stable,
#: legible identifier.
_TRANSLIT = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
    "і": "i",
    "ң": "n",
    "ғ": "g",
    "ү": "u",
    "ұ": "u",
    "қ": "q",
    "ө": "o",
    "һ": "h",
    "ә": "a",
}


def slugify(name: str, *, max_length: int = MAX_ID_LENGTH) -> str:
    """`Жания Жакипова` -> `zhaniya-zhakipova`, matching the ids already in the seeds."""
    lowered = unicodedata.normalize("NFKC", name).casefold()
    latin = "".join(_TRANSLIT.get(character, character) for character in lowered)
    stripped = "".join(
        character
        for character in unicodedata.normalize("NFKD", latin)
        if not unicodedata.combining(character)
    )
    slug = _SEPARATORS.sub("-", stripped).strip("-")
    return slug[:max_length].strip("-")


def unique_id(
    name: str,
    taken: set[str],
    *,
    max_length: int = MAX_ID_LENGTH,
    fallback: str = "employee",
) -> str:
    """A slug nobody is using, or a numbered one if the obvious choice is gone.

    The stores perform no such check; a collision there surfaces as an IntegrityError out
    of a session scope, which is neither catchable at the call site nor legible to the
    admin who pressed the button.

    Raises a bare ValueError when a hundred candidates are all taken. Each caller catches
    it and re-raises with wording that knows what the admin was trying to do.
    """
    base = slugify(name, max_length=max_length)
    if not base:
        # Nothing transliterable — an id still has to satisfy the identifier pattern.
        base = fallback
    if base not in taken:
        return base

    for suffix in range(2, 100):
        tail = f"-{suffix}"
        candidate = f"{base[: max_length - len(tail)].strip('-')}{tail}"
        if candidate not in taken:
            return candidate
    raise ValueError(f"no free id for {name!r}")
