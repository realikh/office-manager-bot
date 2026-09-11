"""How the bot says things.

Two rendering paths, in this order: hand-written lines per mood, and — when a model is
available — an AI-written flavour clause and epithets in that mood. Anything the model
returns must survive a set of guards; if it does not, the hand-written text is used and
nobody ever finds out. The static corpus is therefore the floor on quality, and the bot
keeps working with the AI switched off, broken, or out of credit.

**The model is never handed a fact, and never handed a substitution token.** It writes a
clause and some epithets, nothing else. Every date, weekday, office name and person is
placed by this module. That is a structural guarantee rather than a filter: a hallucinating
model can produce an awkward phrase, but it cannot announce the wrong day, the wrong
office, a person who is not on the roster, or the same weekday twice.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from tabelshchik.application.ports import ChatModel
from tabelshchik.domain.mood import SAFE_MOOD, Mood, pick_mood, variant_index

WHEN_TOKEN = "{when}"
DATE_TOKEN = "{date}"
OFFICE_TOKEN = "{office}"
TAIL_TOKEN = "{tail}"

#: Long enough for a real clause, short enough that a runaway generation is rejected.
MAX_TAIL_LENGTH = 160
#: An epithet is a title, not a sentence.
MAX_EPITHET_LENGTH = 48

_BRACE = re.compile(r"[{}]")
_DIGIT = re.compile(r"\d")
_LETTERS = re.compile(r"[A-Za-zА-Яа-яЁё]")
_WORD = re.compile(r"[\w']+", re.UNICODE)

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
    gendered: Mapping[str, Mapping[Mood, Mapping[str, tuple[str, ...]]]] = field(
        default_factory=dict
    )
    per_mood: Mapping[str, Mapping[Mood, str]] = field(default_factory=dict)
    plain: Mapping[str, str] = field(default_factory=dict)

    def variant(self, key: str, mood: Mood, *, office_id: str, day: date, nonce: int = 0) -> str:
        lines = self._lines(key, mood)
        index = variant_index(office_id=office_id, day=day, kind=key, count=len(lines), nonce=nonce)
        return lines[index]

    def gendered_variant(
        self, key: str, mood: Mood, gender: str, *, office_id: str, day: date, nonce: int = 0
    ) -> str:
        by_gender = self.gendered.get(key, {}).get(mood) or self.gendered.get(key, {}).get(
            SAFE_MOOD, {}
        )
        lines = by_gender.get(gender) or by_gender.get("male") or ()
        if not lines:
            raise KeyError(f"no {gender} lines configured for {key!r}")
        index = variant_index(
            office_id=office_id, day=day, kind=f"{key}:{gender}", count=len(lines), nonce=nonce
        )
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


@dataclass(frozen=True, slots=True)
class Decoration:
    """What the model contributed to one reminder: a clause, and one title per person.

    ``epithets`` is always exactly as long as the roster. Slots the model did not fill, or
    filled badly, hold a hand-written title instead — so the roster loop never has to ask
    whether the AI worked.
    """

    tail: str
    epithets: tuple[str, ...]


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


def render_template(
    template: str, *, when: str = "", date_text: str = "", office_name: str = "", tail: str = ""
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


def _as_sentence(line: str) -> str:
    """The tail closes the opening line, which always ends a sentence before it.

    So it is punctuated and capitalised like one — otherwise the message reads
    "…офис «X» никуда не делся. кофемашина уже нервничает."
    """
    if line[0].islower():
        line = line[0].upper() + line[1:]
    return line if line[-1] in _SENTENCE_END else line + "."


def render_epithet(
    raw: str,
    *,
    gender: str = "male",
    office_name: str,
    common: CommonText,
    forbidden: Iterable[str] = (),
    banned_terms: Iterable[str] = (),
    max_length: int = MAX_EPITHET_LENGTH,
) -> str | None:
    """A title, not a sentence: a couple of words, no punctuation to speak of."""
    line = _collapse(raw).rstrip(" .,;:!?")
    if not line or len(line) > max_length:
        return None
    if len(line.split()) > 4:
        return None
    if not _agrees(line, gender):
        return None
    if not _is_clean(
        line, office_name=office_name, common=common, forbidden=forbidden, banned_terms=banned_terms
    ):
        return None
    if _is_shouting(line):
        return None
    return html.escape(line)


#: Russian adjective endings, which are the reliable half of the agreement signal.
_MASCULINE_ENDINGS = ("ый", "ий", "ой")
_FEMININE_ENDINGS = ("ая", "яя")
_VOWEL_OR_SOFT = "аяеёиоуыэюь"


def _agrees(line: str, gender: str) -> bool:
    """Does this title agree in gender with the person it is about?

    The model is told which gender each slot needs and mostly complies, but "Мужик" in
    front of a woman's name is the kind of mistake nobody wants to see in a group chat —
    so it is checked rather than trusted.

    A heuristic, and deliberately a strict one: it looks at the head word's ending, which
    is unambiguous for adjectives, and treats a lone masculine-looking noun as a
    mismatch. Over-rejecting costs one hand-written title that reads just as well.
    """
    words = _WORD.findall(line.casefold())
    if not words:
        return False
    head = words[0]

    if gender == "female":
        if head.endswith(_MASCULINE_ENDINGS):
            return False
        # "Мужик", "Защитник" — a bare noun ending in a hard consonant.
        return not (len(words) == 1 and head[-1] not in _VOWEL_OR_SOFT)

    if head.endswith(_FEMININE_ENDINGS):
        return False
    return not (len(words) == 1 and head[-1] in "ая")


def _is_clean(
    line: str,
    *,
    office_name: str,
    common: CommonText,
    forbidden: Iterable[str],
    banned_terms: Iterable[str],
) -> bool:
    """The guards shared by every piece of generated text.

    Erring firmly toward refusing: a false positive costs one hand-written line that
    nobody can tell apart from the real thing, while a false negative is visible to
    twenty people in a group chat.
    """
    lowered = line.lower()

    if _BRACE.search(line) or "@" in line or "<" in line or ">" in line:
        return False
    # Digits are how a date, a headcount or an invented statistic gets in.
    if _DIGIT.search(line):
        return False
    # The weekday and the date are already in the header, and the office name is already
    # in quotes. Either repeated is the stutter this whole split exists to prevent.
    if _repeats(lowered, (*common.temporal_words, office_name)):
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
    days: Sequence[tuple[date, Sequence[str]]], common: CommonText, *, limit: int = 4000
) -> list[str]:
    """One heading per day, one person per row.

    The comma-joined single line this replaced was unreadable past about four names, and
    unreadable is the same as unread. Returns a list because a long horizon can exceed
    Telegram's 4096-character message limit, and a message that is one character over is
    simply not delivered.
    """
    blocks = [
        "<b>{heading}</b>\n{names}".format(
            heading=html.escape(format_long_date(day, common)),
            names="\n".join(html.escape(name) for name in names),
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
    ) -> str:
        template = self.catalog.variant(key, mood, office_id=office_id, day=day)
        return render_template(
            template, when=when, date_text=date_text, office_name=office_name, tail=tail
        )

    def emojis(self, mood: Mood, *, office_id: str, day: date, count: int) -> tuple[str, ...]:
        """One per person, distinct while the pool lasts.

        Picked by shuffling the pool rather than by hashing each index independently:
        independent picks collide, and two identical icons three lines apart look like a
        bug rather than like decoration.
        """
        from tabelshchik.domain.rng import stable_hash

        pool = self.catalog.pool("attendance.emojis", mood)
        order = sorted(
            range(len(pool)),
            key=lambda index: stable_hash("emoji", office_id, day.isoformat(), str(index)),
        )
        return tuple(pool[order[index % len(pool)]] for index in range(count))

    def fallback_epithet(
        self, mood: Mood, gender: str, *, office_id: str, day: date, nonce: int
    ) -> str:
        return html.escape(
            self.catalog.gendered_variant(
                "attendance.epithets", mood, gender, office_id=office_id, day=day, nonce=nonce
            )
        )

    async def decorate(
        self,
        mood: Mood,
        *,
        office_id: str,
        office_name: str,
        day: date,
        genders: Sequence[str],
        forbidden: Sequence[str] = (),
    ) -> Decoration:
        """The flavour clause and one epithet per person, in a single request.

        One call rather than two: the tail and the epithets are wanted at the same moment
        for the same message, and folding them together halves what the reminder costs.
        """
        fallback = Decoration(
            tail=self._static_tail(mood, office_id=office_id, day=day),
            epithets=tuple(
                self.fallback_epithet(mood, gender, office_id=office_id, day=day, nonce=index)
                for index, gender in enumerate(genders)
            ),
        )

        persona = self.catalog.line("ai.persona", mood)
        if self.model is None or not self.moods.enabled or not persona:
            return fallback

        completion = await self.model.complete(
            _decoration_system_prompt(persona),
            _decoration_user_prompt(genders),
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            json_object=True,
        )
        if completion is None:
            return fallback

        payload = _parse_json(completion.text)
        if payload is None:
            return fallback

        def clean_tail(raw: str) -> str | None:
            return render_tail(
                raw,
                office_name=office_name,
                common=self.catalog.common,
                forbidden=forbidden,
                banned_terms=self.moods.banned_terms,
            )

        def clean_epithet(raw: str, gender: str) -> str | None:
            return render_epithet(
                raw,
                gender=gender,
                office_name=office_name,
                common=self.catalog.common,
                forbidden=forbidden,
                banned_terms=self.moods.banned_terms,
            )

        tail = clean_tail(str(payload.get("tail", ""))) or fallback.tail

        raw_epithets = payload.get("epithets")
        offered = raw_epithets if isinstance(raw_epithets, list) else []
        epithets = tuple(
            # Per slot, not all-or-nothing: a model that titles one person well and
            # fumbles the next should cost us only the second title.
            _offered(offered, index, gender, clean_epithet) or fallback.epithets[index]
            for index, gender in enumerate(genders)
        )
        return Decoration(tail=tail, epithets=epithets)

    def _static_tail(self, mood: Mood, *, office_id: str, day: date) -> str:
        line = self.catalog.variant("attendance.tails", mood, office_id=office_id, day=day)
        # Same normalisation the generated tails get.
        return html.escape(_as_sentence(line)) if line else ""


def _offered(
    values: list[object], index: int, gender: str, clean: Callable[[str, str], str | None]
) -> str | None:
    if index >= len(values) or not isinstance(values[index], str):
        return None
    return clean(str(values[index]), gender)


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


def _decoration_system_prompt(persona: str) -> str:
    return (
        f"{persona}\n\n"
        "Ты оформляешь ежедневное напоминание о выходе в офис. Даты, имена и название "
        "офиса подставляются автоматически — тебе их не сообщают и упоминать их нельзя.\n"
        "Верни СТРОГО JSON вида: "
        '{"tail": "...", "epithets": ["...", "..."]}\n'
        "tail — одна короткая фраза (3–12 слов) в твоём настроении. Она встанет в конец "
        "вступительной строки.\n"
        "epithets — шуточные титулы по 1–3 слова, ровно столько же, сколько указано, "
        "и строго в том же порядке и роде.\n"
        "Запрещено везде: числа и цифры, даты, дни недели, месяцы, названия офисов, "
        "имена людей, @упоминания, эмодзи, разметка, капслок.\n"
        "Титулы — только доброжелательно-шуточные: про стойкость, кофе, дорогу, "
        "переговорки. Никогда про внешность, компетентность, здоровье или личную жизнь."
    )


def _decoration_user_prompt(genders: Sequence[str]) -> str:
    listed = ", ".join(
        f"{index + 1}) {'женский' if gender == 'female' else 'мужской'} род"
        for index, gender in enumerate(genders)
    )
    return f"Нужно {len(genders)} титулов. Род по порядку: {listed}."
