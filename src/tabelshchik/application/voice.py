"""How the bot says things.

Two rendering paths, in this order: hand-written lines per mood, and, when a model is
available, an AI-written flavour clause in that mood. Anything the model
returns must survive a set of guards; if it does not, the hand-written text is used and
nobody ever finds out. The static corpus is therefore the floor on quality, and the bot
keeps working with the AI switched off, broken, or out of credit.

**The model is never handed a fact, and never handed a substitution token.** It writes a
clause, nothing else. Every date, weekday, office name and person is
placed by this module. That is a structural guarantee rather than a filter: a hallucinating
model can produce an awkward phrase, but it cannot announce the wrong day, the wrong
office, a person who is not on the roster, or the same weekday twice.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from tabelshchik.application.ports import ChatModel
from tabelshchik.domain.mood import SAFE_MOOD, Mood, pick_mood, variant_index

WHEN_TOKEN = "{when}"
DATE_TOKEN = "{date}"
OFFICE_TOKEN = "{office}"
TAIL_TOKEN = "{tail}"
MINUTES_TOKEN = "{minutes}"
HOLIDAY_TOKEN = "{holiday}"

#: Long enough for a real clause, short enough that a runaway generation is rejected.
MAX_TAIL_LENGTH = 160
#: A whole message this time — a greeting or a Tempo nag — but still a chat message, not
#: an essay the whole office scrolls past.
MAX_ANNOUNCEMENT_LENGTH = 500

_BRACE = re.compile(r"[{}]")
_DIGIT = re.compile(r"\d")
_LETTERS = re.compile(r"[A-Za-zА-Яа-яЁё]")
_WORD = re.compile(r"[\w']+", re.UNICODE)
_NUMBER = re.compile(r"\d+")
#: The link is ours. A model that writes its own is at best a duplicate and at worst a
#: plausible-looking address that goes somewhere else.
_URL = re.compile(r"https?:|://|www\.|\.(?:com|ru|kz|org|net|io)\b", re.IGNORECASE)

#: Below this, a word is matched whole rather than stemmed.
_MIN_PREFIX = 4
_SENTENCE_END = ".!?…:"


@dataclass(frozen=True, slots=True)
class CommonText:
    weekdays: tuple[str, ...]
    weekdays_short: tuple[str, ...]
    months: tuple[str, ...]
    tomorrow: str
    on_weekday: str
    #: Prefixed to messages when two offices share one chat.
    office_header: str = "🏢 <b>{office}</b>"
    #: "минуту", "минуты", "минут" — the accusative forms for 1, 2–4 and 5–20.
    minutes: tuple[str, str, str] = ("минуту", "минуты", "минут")

    @property
    def temporal_words(self) -> tuple[str, ...]:
        """Everything the header already says about *when*.

        A generated clause containing any of these is rejected, which is what stops the
        model prefixing its own "В понедельник" to a header that already has one.
        """
        return (*self.weekdays, *self.months, self.tomorrow)


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

    def variant(self, key: str, mood: Mood, *, office_id: str, day: date, nonce: int = 0) -> str:
        lines = self._lines(key, mood)
        index = variant_index(office_id=office_id, day=day, kind=key, count=len(lines), nonce=nonce)
        return lines[index]

    def pool(self, key: str, mood: Mood) -> tuple[str, ...]:
        """Every option for a key, for callers that need several distinct ones."""
        return self._lines(key, mood)

    def line(self, key: str, mood: Mood) -> str:
        return self.per_mood.get(key, {}).get(mood, "")

    def text(self, key: str) -> str:
        return self.plain.get(key, "")

    def _lines(self, key: str, mood: Mood) -> tuple[str, ...]:
        lines = self.variants.get(key, {}).get(mood) or self.variants.get(key, {}).get(SAFE_MOOD)
        if not lines:
            raise KeyError(f"no lines configured for {key!r}")
        return lines


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


def office_header(office_name: str, common: CommonText) -> str:
    """A header naming the office, for chats that carry more than one."""
    return common.office_header.replace("{office}", html.escape(office_name))


def format_date(day: date, common: CommonText) -> str:
    """`16 сентября 2026 года`. The bare date — no weekday, see `lead_in`."""
    return f"{day.day} {common.months[day.month - 1]} {day.year} года"


def format_long_date(day: date, common: CommonText) -> str:
    """`Пятница, 11 сентября 2026 года`, for a heading that stands on its own."""
    return f"{common.weekdays[day.weekday()].capitalize()}, {format_date(day, common)}"


def lead_in(today: date, target: date, common: CommonText) -> str:
    """`Завтра` when it is tomorrow, otherwise `В пятницу`.

    Kept apart from `format_date` on purpose: folding the two together is what produced
    "В понедельник В понедельник, 14 сентября" — the model wrote its own lead-in in front
    of a `{date}` it could not see the contents of.
    """
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


def plural_minutes(count: int, common: CommonText) -> str:
    """`10 минут`, `21 минуту`, `3 минуты` — as the phrase "потратьте последние …" needs."""
    one, few, many = common.minutes
    if 11 <= count % 100 <= 14:
        word = many
    elif count % 10 == 1:
        word = one
    elif 2 <= count % 10 <= 4:
        word = few
    else:
        word = many
    return f"{count} {word}"


def render_template(
    template: str,
    *,
    when: str = "",
    date_text: str = "",
    office_name: str = "",
    tail: str = "",
    minutes: str = "",
    holiday: str = "",
) -> str:
    """Fill one of our own templates.

    Escaping happens first so a name carrying markup cannot inject any, then substitution
    is literal — never ``str.format`` — so a stray brace in the data can neither raise nor
    interpolate something unintended.
    """
    rendered = html.escape(template)
    rendered = rendered.replace(WHEN_TOKEN, html.escape(when))
    rendered = rendered.replace(DATE_TOKEN, html.escape(date_text))
    rendered = rendered.replace(OFFICE_TOKEN, f"«{html.escape(office_name)}»")
    rendered = rendered.replace(MINUTES_TOKEN, html.escape(minutes))
    rendered = rendered.replace(HOLIDAY_TOKEN, html.escape(holiday))
    # The tail is already escaped by its own guard, which has to inspect the raw text.
    return rendered.replace(TAIL_TOKEN, tail)


def render_tail(
    raw: str,
    *,
    office_name: str,
    common: CommonText,
    forbidden: Iterable[str] = (),
    banned_terms: Iterable[str] = (),
    max_length: int = MAX_TAIL_LENGTH,
) -> str | None:
    """Turn the model's flavour clause into something safe to send, or refuse it.

    Returns None whenever anything is off, and every caller has hand-written text to fall
    back to — so rejecting is always cheap and never leaves the bot silent.
    """
    line = _collapse(raw)
    if not line or len(line) > max_length:
        return None
    if not _is_clean(
        line, office_name=office_name, common=common, forbidden=forbidden, banned_terms=banned_terms
    ):
        return None
    if _is_shouting(line):
        return None

    return html.escape(_as_sentence(line))


def render_announcement(
    raw: str,
    *,
    office_name: str,
    common: CommonText,
    forbidden: Iterable[str] = (),
    banned_terms: Iterable[str] = (),
    allow_words: Iterable[str] = (),
    allow_numbers: Iterable[int] = (),
    max_length: int = MAX_ANNOUNCEMENT_LENGTH,
) -> str | None:
    """A whole generated message — a Tempo nag or a holiday greeting — or None.

    The same guards as a tail, loosened in exactly two places and nowhere else: the
    caller may allow today's own weekday and month ("С пятницей!", "С 8 Марта!"), and
    specific numbers it has put there itself (the minutes left in the day). Any other
    weekday, date or number is still the model inventing a fact.
    """
    line = _collapse(raw)
    if not line or len(line) > max_length:
        return None
    if _URL.search(line):
        return None
    if not _is_clean(
        line,
        office_name=office_name,
        common=common,
        forbidden=forbidden,
        banned_terms=banned_terms,
        allow_words=allow_words,
        allow_numbers=allow_numbers,
    ):
        return None
    if _is_shouting(line):
        return None
    return html.escape(_as_sentence(line))


def _as_sentence(line: str) -> str:
    """The tail closes the opening line, which always ends a sentence before it.

    So it is punctuated and capitalised like one — otherwise the message reads
    "…офис «X» никуда не делся. кофемашина уже нервничает."
    """
    if line[0].islower():
        line = line[0].upper() + line[1:]
    return line if line[-1] in _SENTENCE_END else line + "."


#: The icons the model is offered. An allowlist, not a pattern check: a work chat is not
#: the place to discover what a model considers appropriate.
SAFE_EMOJI: frozenset[str] = frozenset(
    # Coffee, food, the morning
    "☕🍵🧋🍩🥐🥪🍫🧃🍪"
    # The road in
    "🚗🚕🚌🚲🛴🗺🧭🚦🛣🚇"
    # Work, desks, meetings
    "💻⌨🖥📊📈📋📌📎🗂🖇📅🗓⏰⌛🔔🤝"
    # Weather and the sky
    "🌞🌤⛅🌧🌫❄🌈⭐🌟✨🌙"
    # Standing firm
    "🛡⚔🏰🧱🗿⚓🔩🧲"
    # Energy and celebration
    "⚡🔥🚀🎉🎯🎲🏆🥇🎪🎨🎸"
    # Nature, quiet things
    "🌱🌳🍀🌵🪴🐝🦉🐢🦫"
    # For the moods that are not cheerful
    "🫠🕳📉⏳🕯🧊🪫"
    # Odds and ends
    "🔧🔑🧩🔍💡📖🧠🎩🪄🧭"
)

EMOJI_CHOICES = 20
ANNOUNCEMENT_EMOJI_CHOICES = 8


def emoji_choices(*, office_id: str, day: date) -> tuple[str, ...]:
    from tabelshchik.domain.rng import stable_hash

    ranked = sorted(
        SAFE_EMOJI, key=lambda icon: stable_hash("emoji-choices", office_id, day.isoformat(), icon)
    )
    return tuple(ranked[:EMOJI_CHOICES])


def _is_clean(
    line: str,
    *,
    office_name: str,
    common: CommonText,
    forbidden: Iterable[str],
    banned_terms: Iterable[str],
    allow_words: Iterable[str] = (),
    allow_numbers: Iterable[int] = (),
) -> bool:
    """The guards shared by every piece of generated text.

    Erring firmly toward refusing: a false positive costs one hand-written line that
    nobody can tell apart from the real thing, while a false negative is visible to
    twenty people in a group chat.
    """
    lowered = line.lower()

    if _BRACE.search(line) or "@" in line or "<" in line or ">" in line:
        return False
    # Digits are how a date, a headcount or an invented statistic gets in. The only ones
    # allowed are those the caller put in the brief itself.
    allowed_numbers = {str(number) for number in allow_numbers}
    if allowed_numbers:
        if any(number not in allowed_numbers for number in _NUMBER.findall(line)):
            return False
    elif _DIGIT.search(line):
        return False
    # The weekday and the date are already in the header, and the office name is already
    # in quotes. Either repeated is the stutter this whole split exists to prevent.
    allowed_words = {word.casefold() for word in allow_words}
    temporal = [word for word in common.temporal_words if word.casefold() not in allowed_words]
    if _repeats(lowered, (*temporal, office_name)):
        return False
    # Names are injected by us, in a fixed format. A model that produced one has either
    # been fed data it should not have, or invented a colleague by coincidence.
    if any(stem in lowered for stem in name_stems(forbidden)):
        return False
    return not any(term and term.lower() in lowered for term in banned_terms)


def _repeats(lowered: str, phrases: Iterable[str]) -> bool:
    """Does this text name something the message already says elsewhere?

    Matched on word-initial stems rather than as bare substrings. Russian declines, so
    "понедельник" has to catch "понедельника" — but a plain substring test cannot be used
    for the short words: `мая` would stem to `ма` and reject "кофемашина", "малиновый"
    and most of the language with it.
    """
    words = _WORD.findall(lowered)
    if not words:
        return False
    return any(word.startswith(stem) for stem in _prefixes(phrases) for word in words)


def _prefixes(phrases: Iterable[str]) -> set[str]:
    prefixes: set[str] = set()
    for phrase in phrases:
        for token in _WORD.findall(phrase.casefold()):
            if len(token) < _MIN_PREFIX:
                continue
            # Long enough to decline: drop the final letter. Short enough that dropping
            # one would match anything: keep the whole word.
            prefixes.add(token[: max(_MIN_PREFIX, len(token) - 1)])
    return prefixes


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


def render_days(
    days: Sequence[tuple[date, Sequence[str]]],
    common: CommonText,
    *,
    limit: int = 4000,
    markup: bool = True,
) -> list[str]:
    """One heading per day, one person per row.

    The comma-joined single line this replaced was unreadable past about four names, and
    unreadable is the same as unread. Returns a list because a long horizon can exceed
    Telegram's 4096-character message limit, and a message that is one character over is
    simply not delivered.

    ``markup=False`` is for a terminal, where bold tags and HTML entities are noise
    rather than formatting.
    """
    template = "<b>{heading}</b>\n{names}" if markup else "{heading}\n{names}"
    escape = html.escape if markup else str
    blocks = [
        template.format(
            heading=escape(format_long_date(day, common)),
            names="\n".join(escape(name) for name in names),
        )
        for day, names in days
        if names
    ]

    chunks: list[str] = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > limit and current:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def roster_fingerprint(employee_ids: Iterable[str]) -> str:
    """Identifies exactly who was announced for a day.

    If an announced day's roster later differs, the reminder may legitimately be sent
    again as a correction — and if it does not, a re-run must stay quiet.
    """
    from tabelshchik.domain.rng import stable_hash

    return f"{stable_hash(*sorted(employee_ids)):016x}"


def name_stems(names: Iterable[str]) -> set[str]:
    """Rough stems of every word in every name, for the forbidden-name check."""
    return _stems(names)


def _stems(phrases: Iterable[str]) -> set[str]:
    stems: set[str] = set()
    for phrase in phrases:
        for token in re.split(r"[\s\-_.]+", phrase.strip()):
            cleaned = token.strip().lower()
            if len(cleaned) < 2:
                continue
            stems.add(cleaned[: max(2, len(cleaned) - 1)])
    return stems


def _collapse(raw: str) -> str:
    """One line, single-spaced. A model that answers in a paragraph still gets a chance."""
    return " ".join(raw.split())


def _is_shouting(line: str) -> bool:
    letters = _LETTERS.findall(line)
    if len(letters) < 12:
        return False
    upper = sum(1 for character in letters if character.isupper())
    return upper / len(letters) > 0.6


@dataclass(frozen=True, slots=True)
class Voice:
    """Picks the mood, picks the words, and asks the model only where that can go wrong
    safely."""

    catalog: Catalog
    moods: MoodPolicy
    model: ChatModel | None = None
    max_tokens: int = 200
    temperature: float = 0.9

    def mood_for(self, *, office_id: str, day: date, nonce: int = 0) -> Mood:
        return self.moods.mood_for(office_id=office_id, day=day, nonce=nonce)

    def static(
        self,
        key: str,
        mood: Mood,
        *,
        office_id: str,
        day: date,
        when: str = "",
        date_text: str = "",
        office_name: str = "",
        tail: str = "",
        minutes: str = "",
        holiday: str = "",
    ) -> str:
        template = self.catalog.variant(key, mood, office_id=office_id, day=day)
        return render_template(
            template,
            when=when,
            date_text=date_text,
            office_name=office_name,
            tail=tail,
            minutes=minutes,
            holiday=holiday,
        )

    async def announce(
        self,
        mood: Mood,
        *,
        brief: str,
        fallback: str,
        office_name: str,
        office_id: str,
        day: date,
        forbidden: Sequence[str] = (),
        allow_words: Sequence[str] = (),
        allow_numbers: Sequence[int] = (),
    ) -> Announcement:
        """A whole message in today's voice, or ``fallback`` if the model is not up to it.

        ``fallback`` is already rendered and escaped. The brief says what the message is
        for; the persona says how it sounds. Links, names and headers are the caller's to
        add — the model is told they exist so it does not write its own.
        """
        persona = self.catalog.line("ai.persona", mood)
        if self.model is None or not self.moods.enabled or not persona:
            return Announcement(fallback)

        choices = emoji_choices(office_id=office_id, day=day)[:ANNOUNCEMENT_EMOJI_CHOICES]
        completion = await self.model.complete(
            _announcement_system_prompt(persona, choices),
            brief,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            json_object=True,
        )
        payload = _parse_json(completion.text) if completion is not None else None
        raw = payload.get("text") if payload is not None else None
        if not isinstance(raw, str):
            return Announcement(fallback)

        cleaned = render_announcement(
            raw,
            office_name=office_name,
            common=self.catalog.common,
            forbidden=forbidden,
            banned_terms=self.moods.banned_terms,
            allow_words=allow_words,
            allow_numbers=allow_numbers,
        )
        return Announcement(cleaned, generated=True) if cleaned else Announcement(fallback)

    async def tail(
        self,
        mood: Mood,
        *,
        office_id: str,
        office_name: str,
        day: date,
        forbidden: Sequence[str] = (),
    ) -> str:
        fallback = self._static_tail(mood, office_id=office_id, day=day)
        persona = self.catalog.line("ai.persona", mood)
        if self.model is None or not self.moods.enabled or not persona:
            return fallback

        completion = await self.model.complete(
            _tail_system_prompt(persona, emoji_choices(office_id=office_id, day=day)),
            TAIL_REQUEST,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            json_object=True,
        )
        payload = _parse_json(completion.text) if completion is not None else None
        raw = payload.get("tail") if payload is not None else None
        if not isinstance(raw, str):
            return fallback

        cleaned = render_tail(
            raw,
            office_name=office_name,
            common=self.catalog.common,
            forbidden=forbidden,
            banned_terms=self.moods.banned_terms,
        )
        return cleaned or fallback

    def _static_tail(self, mood: Mood, *, office_id: str, day: date) -> str:
        line = self.catalog.variant("attendance.tails", mood, office_id=office_id, day=day)
        # Same normalisation the generated tails get.
        return html.escape(_as_sentence(line)) if line else ""


@dataclass(frozen=True, slots=True)
class Announcement:
    text: str
    #: Whether the model wrote it, as opposed to the hand-written fallback.
    generated: bool = False


def _parse_json(raw: str) -> dict[str, object] | None:
    """Models occasionally wrap JSON in a fence even when asked not to."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        _, _, text = text.partition("\n")
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


TAIL_REQUEST = "Напиши фразу для сегодняшнего напоминания."


def _tail_system_prompt(persona: str, choices: Sequence[str]) -> str:
    return (
        f"{persona}\n\n"
        "Ты оформляешь ежедневное напоминание о выходе в офис. Даты, имена и название "
        "офиса подставляются автоматически — тебе их не сообщают и упоминать их нельзя.\n"
        'Верни СТРОГО JSON вида: {"tail": "..."}\n'
        "tail — одна короткая фраза (3–12 слов) в твоём настроении. Она встанет в конец "
        "вступительной строки. Пусть её подскажет сегодняшний набор образов, а не привычные "
        f"темы: {' '.join(choices)}\n"
        "Запрещено в tail: числа и цифры, даты, дни недели, месяцы, названия офисов, имена "
        "людей, @упоминания, эмодзи, разметка, капслок."
    )


def _announcement_system_prompt(persona: str, choices: Sequence[str]) -> str:
    return (
        f"{persona}\n\n"
        "Ты пишешь одно сообщение в рабочий чат. Задание — в сообщении пользователя.\n"
        'Верни СТРОГО JSON вида: {"text": "..."}\n'
        "text — готовое сообщение на русском языке: два-четыре предложения, не длиннее "
        "четырёхсот символов, в твоём сегодняшнем настроении. Можно одно-два эмодзи, "
        f"только из сегодняшнего набора: {' '.join(choices)}\n"
        "Ссылку, если она нужна, бот добавит сам. Не называй людей и офисы.\n"
        "Запрещено: цифры (кроме тех, что прямо разрешены в задании), даты, дни недели и "
        "месяцы (кроме разрешённых в задании), слово «завтра», имена людей, @упоминания, "
        "ссылки, разметка, капслок.\n"
        "Иронизируй над обстоятельствами, никогда над людьми."
    )
