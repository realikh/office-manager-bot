"""How the bot says things.

Two rendering paths, in this order: hand-written lines per mood, and — when a model is
available — an AI rewrite of the opening line in that mood. Anything the model returns
must survive a set of guards; if it does not, the hand-written line is used and nobody
ever finds out. The static corpus is therefore the floor on quality, and the bot keeps
working with the AI switched off, broken, or out of credit.

The model is never told the date, the office name, or anyone's name. It must emit the
literal tokens ``{date}`` and ``{office}``, which are substituted afterwards. So a
hallucinating model can produce an awkward sentence, but it cannot announce the wrong
day, the wrong office, or a person who is not on the roster.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from tabelshchik.application.ports import ChatModel
from tabelshchik.domain.mood import SAFE_MOOD, Mood, pick_mood, variant_index

DATE_TOKEN = "{date}"
OFFICE_TOKEN = "{office}"

#: Long enough for a real sentence, short enough that a runaway generation is rejected.
MAX_FLAVOURED_LENGTH = 400

_BRACE = re.compile(r"[{}]")
_LETTERS = re.compile(r"[A-Za-zА-Яа-яЁё]")

#: Quote pairs a model likes to wrap the office token in. We add our own «», so theirs go.
_QUOTE_PAIRS = (("«", "»"), ('"', '"'), ("„", "“"), ("'", "'"), ("“", "”"))


@dataclass(frozen=True, slots=True)
class CommonText:
    weekdays: tuple[str, ...]
    weekdays_short: tuple[str, ...]
    months: tuple[str, ...]
    tomorrow: str
    on_weekday: str


@dataclass(frozen=True, slots=True)
class Catalog:
    """Message templates, already validated by the config loader.

    Keyed by dotted path (``"attendance.intro"``) so the mapping from YAML stays a short
    loop rather than a field-by-field transcription.
    """

    common: CommonText
    variants: Mapping[str, Mapping[Mood, tuple[str, ...]]] = field(default_factory=dict)
    per_mood: Mapping[str, Mapping[Mood, str]] = field(default_factory=dict)
    plain: Mapping[str, str] = field(default_factory=dict)

    def variant(self, key: str, mood: Mood, *, office_id: str, day: date) -> str:
        lines = self.variants.get(key, {}).get(mood) or self.variants.get(key, {}).get(SAFE_MOOD)
        if not lines:
            raise KeyError(f"no lines configured for {key!r}")
        index = variant_index(office_id=office_id, day=day, kind=key, count=len(lines))
        return lines[index]

    def line(self, key: str, mood: Mood) -> str:
        return self.per_mood.get(key, {}).get(mood, "")

    def text(self, key: str) -> str:
        return self.plain.get(key, "")


@dataclass(frozen=True, slots=True)
class MoodPolicy:
    enabled: bool = True
    weights: Mapping[Mood, int] = field(default_factory=dict)
    safe_mode: bool = False
    banned_terms: tuple[str, ...] = ()

    def mood_for(self, *, office_id: str, day: date, nonce: int = 0) -> Mood:
        if not self.enabled or self.safe_mode:
            return SAFE_MOOD
        return pick_mood(self.weights, office_id=office_id, day=day, nonce=nonce)


def format_date(day: date, common: CommonText) -> str:
    """`16 сентября 2026 года`."""
    return f"{day.day} {common.months[day.month - 1]} {day.year} года"


def lead_in(today: date, target: date, common: CommonText) -> str:
    """`Завтра` when it is tomorrow, otherwise `В пятницу`."""
    if (target - today).days == 1:
        return common.tomorrow
    weekday = common.weekdays[target.weekday()]
    return common.on_weekday.format(weekday=_accusative(weekday))


def _accusative(weekday: str) -> str:
    """Russian weekday in the accusative, as `в пятницу` needs.

    Only the feminine -а forms change; the rest are already correct after `в`.
    """
    if weekday.endswith("а"):
        return weekday[:-1] + "у"
    return weekday


def render_flavoured(
    raw: str,
    *,
    date_text: str,
    office_name: str,
    forbidden: Iterable[str] = (),
    banned_terms: Iterable[str] = (),
    max_length: int = MAX_FLAVOURED_LENGTH,
) -> str | None:
    """Turn a model's line into something safe to send, or refuse it.

    Returns None whenever anything is off, and every caller has a hand-written line to
    fall back to — so rejecting is always cheap and never leaves the bot silent.
    """
    line = raw.strip()
    if not line or len(line) > max_length:
        return None
    if DATE_TOKEN not in line or OFFICE_TOKEN not in line:
        return None

    lowered = line.lower()
    # Names are injected by us, in a fixed format. A model that produced one has either
    # been fed data it should not have, or invented a colleague by coincidence. Matching
    # is on stems, because Russian names decline — "Аня" appears as "Аню" — and a false
    # positive costs nothing: we simply use the hand-written line instead. So this errs
    # firmly toward refusing.
    if any(stem in lowered for stem in name_stems(forbidden)):
        return None
    if any(term and term.lower() in lowered for term in banned_terms):
        return None
    if "@" in line:
        return None
    if _is_shouting(line):
        return None

    # Escape first so the model cannot inject markup, then substitute — the braces
    # survive escaping, and substitution is literal, never str.format, so a stray brace
    # can neither raise nor interpolate something unintended.
    rendered = html.escape(line)
    for opening, closing in _QUOTE_PAIRS:
        rendered = rendered.replace(
            f"{html.escape(opening)}{OFFICE_TOKEN}{html.escape(closing)}", OFFICE_TOKEN
        )
    rendered = rendered.replace(DATE_TOKEN, html.escape(date_text))
    rendered = rendered.replace(OFFICE_TOKEN, f"«{html.escape(office_name)}»")

    if _BRACE.search(rendered):
        return None
    return rendered


def render_static(template: str, *, date_text: str, office_name: str) -> str:
    """Render one of our own lines. Same substitution rules, no guards needed."""
    rendered = html.escape(template)
    rendered = rendered.replace(DATE_TOKEN, html.escape(date_text))
    return rendered.replace(OFFICE_TOKEN, f"«{html.escape(office_name)}»")


def mention(
    full_name: str, *, telegram_user_id: int | None = None, username: str | None = None
) -> str:
    """Tag one person.

    A ``tg://user?id=`` link is preferred because it reaches people who have no
    @username at all, which a plain @mention cannot do. The username is the fallback,
    and a bare name is better than nothing.
    """
    name = html.escape(full_name)
    if telegram_user_id is not None:
        return f'<a href="tg://user?id={telegram_user_id}">{name}</a>'
    if username:
        return f"{name} (@{html.escape(username.lstrip('@'))})"
    return name


def roster_fingerprint(employee_ids: Iterable[str]) -> str:
    """Identifies exactly who was announced for a day.

    If an announced day's roster later differs, the reminder may legitimately be sent
    again as a correction — and if it does not, a re-run must stay quiet.
    """
    from tabelshchik.domain.rng import stable_hash

    return f"{stable_hash(*sorted(employee_ids)):016x}"


def name_stems(names: Iterable[str]) -> set[str]:
    """Rough stems of every word in every name, for the forbidden-name check."""
    stems: set[str] = set()
    for name in names:
        for token in re.split(r"[\s\-_.]+", name.strip()):
            cleaned = token.strip().lower()
            if len(cleaned) < 2:
                continue
            stems.add(cleaned[: max(2, len(cleaned) - 1)])
    return stems


def _is_shouting(line: str) -> bool:
    letters = _LETTERS.findall(line)
    if len(letters) < 12:
        return False
    upper = sum(1 for character in letters if character.isupper())
    return upper / len(letters) > 0.6


@dataclass(frozen=True, slots=True)
class Voice:
    """Picks the mood, picks the words, and asks the model only if that can go wrong
    safely."""

    catalog: Catalog
    moods: MoodPolicy
    model: ChatModel | None = None
    max_tokens: int = 200
    temperature: float = 0.9

    def mood_for(self, *, office_id: str, day: date, nonce: int = 0) -> Mood:
        return self.moods.mood_for(office_id=office_id, day=day, nonce=nonce)

    def static(self, key: str, mood: Mood, *, office_id: str, day: date, **fields: str) -> str:
        template = self.catalog.variant(key, mood, office_id=office_id, day=day)
        return render_static(
            template,
            date_text=fields.get("date_text", ""),
            office_name=fields.get("office_name", ""),
        )

    async def intro(
        self,
        key: str,
        mood: Mood,
        *,
        office_id: str,
        office_name: str,
        day: date,
        date_text: str,
        forbidden: Sequence[str] = (),
    ) -> str:
        """The opening line. AI-flavoured when possible, hand-written otherwise."""
        fallback = render_static(
            self.catalog.variant(key, mood, office_id=office_id, day=day),
            date_text=date_text,
            office_name=office_name,
        )

        if self.model is None or not self.moods.enabled:
            return fallback

        persona = self.catalog.line("ai.persona", mood)
        if not persona:
            return fallback

        raw = await self.model.complete(
            _flavour_system_prompt(persona),
            _flavour_user_prompt(key),
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )
        if raw is None:
            return fallback

        flavoured = render_flavoured(
            raw,
            date_text=date_text,
            office_name=office_name,
            forbidden=forbidden,
            banned_terms=self.moods.banned_terms,
        )
        return flavoured or fallback


def _flavour_system_prompt(persona: str) -> str:
    return (
        f"{persona}\n\n"
        "Ты пишешь ОДНУ вступительную строку к списку сотрудников, которые выходят "
        "в офис. Строго обязательно:\n"
        "1. Ровно одно предложение, не длиннее 200 символов, на русском языке.\n"
        "2. В строке должны быть подстановки {date} и {office} — именно в таком виде, "
        "с фигурными скобками. Не заменяй их и не бери в кавычки.\n"
        "3. Других фигурных скобок быть не должно.\n"
        "4. Не придумывай имена, должности, числа и факты. Никаких @упоминаний.\n"
        "5. Не пиши капслоком.\n"
        "6. Ирония допустима только в адрес обстоятельств: понедельников, дороги, "
        "опенспейса, совещаний, работы как явления. Никогда — в адрес людей, их "
        "внешности, способностей, здоровья или личной жизни.\n"
        "Ответь только этой строкой, без пояснений."
    )


def _flavour_user_prompt(key: str) -> str:
    topics = {
        "attendance.intro": "Список тех, кто выходит в офис, идёт сразу после твоей строки.",
        "attendance.empty": "В этот день в офис никто не выходит.",
    }
    return topics.get(key, "Напиши вступительную строку.")
