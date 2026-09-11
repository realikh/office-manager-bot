"""Tests for the bot's voice, and especially for what it refuses to say."""

from __future__ import annotations

from datetime import date

import pytest

from tabelshchik.application.voice import (
    Catalog,
    CommonText,
    MoodPolicy,
    Voice,
    format_date,
    lead_in,
    render_flavoured,
    render_static,
)
from tabelshchik.domain.mood import SAFE_MOOD, Mood

DAY = date(2026, 9, 16)
COMMON = CommonText(
    weekdays=("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"),
    weekdays_short=("пн", "вт", "ср", "чт", "пт", "сб", "вс"),
    months=(
        "января",
        "февраля",
        "марта",
        "апреля",
        "мая",
        "июня",
        "июля",
        "августа",
        "сентября",
        "октября",
        "ноября",
        "декабря",
    ),
    tomorrow="Завтра",
    on_weekday="В {weekday}",
)


def catalog(**variants) -> Catalog:
    base = {"attendance.intro": {mood: (f"[{mood.value}] {{date}}, {{office}}:",) for mood in Mood}}
    base.update(variants)
    return Catalog(
        common=COMMON,
        variants=base,
        per_mood={"ai.persona": dict.fromkeys(Mood, "Ты — Табельщик.")},
    )


def flavour(raw: str, **kwargs) -> str | None:
    return render_flavoured(raw, date_text="16 сентября 2026 года", office_name="O'Vest", **kwargs)


# ------------------------------------------------------------------------ formatting


def test_dates_are_rendered_in_russian() -> None:
    assert format_date(DAY, COMMON) == "16 сентября 2026 года"


def test_tomorrow_is_called_tomorrow() -> None:
    assert lead_in(date(2026, 9, 15), DAY, COMMON) == "Завтра"


def test_a_further_day_is_named_by_its_weekday_in_the_accusative() -> None:
    # Friday reminding about Monday: "В понедельник", not "В понедельника".
    assert lead_in(date(2026, 9, 18), date(2026, 9, 21), COMMON) == "В понедельник"
    # Feminine weekdays do decline: среда -> в среду.
    assert lead_in(date(2026, 9, 15), date(2026, 9, 16), COMMON) == "Завтра"
    assert lead_in(date(2026, 9, 14), date(2026, 9, 16), COMMON) == "В среду"


def test_static_lines_substitute_and_escape() -> None:
    rendered = render_static(
        "{date} в {office}", date_text="16 сентября", office_name="O'Vest & Co"
    )
    assert rendered == "16 сентября в «O&#x27;Vest &amp; Co»"


# -------------------------------------------------------------- the flavouring guards


def test_a_well_formed_line_is_accepted() -> None:
    result = flavour("Так, {date}, офис {office}. Вызываются:")
    assert result == "Так, 16 сентября 2026 года, офис «O&#x27;Vest». Вызываются:"


def test_a_line_without_the_date_token_is_refused() -> None:
    """The model is never told the date, so a line that does not ask for one has
    invented its own or dropped it."""
    assert flavour("Сегодня в {office} выходят:") is None


def test_a_line_without_the_office_token_is_refused() -> None:
    assert flavour("Так, {date}, в офис выходят:") is None


def test_a_stray_brace_is_refused() -> None:
    assert flavour("{date} в {office} {кто-то}:") is None


def test_the_model_may_not_name_an_employee() -> None:
    """It is never given the roster. A name in the output is invention."""
    assert (
        flavour("{date} в {office}: особенно ждём Alikhan.", forbidden=["Alikhan Khassen"]) is None
    )


def test_a_declined_russian_name_is_still_caught() -> None:
    """Matching is on stems because Russian names inflect: Аня becomes Аню."""
    assert flavour("{date} в {office}: особенно ждём Аню.", forbidden=["Аня"]) is None
    assert flavour("{date} в {office}: ждём Дмитрия.", forbidden=["Дмитрий"]) is None


def test_the_model_may_not_mention_anyone() -> None:
    assert flavour("{date} в {office}, привет @realikh:") is None


def test_banned_terms_are_refused() -> None:
    assert flavour("{date} в {office}, дурацкий день:", banned_terms=["дурацк"]) is None


def test_shouting_is_refused() -> None:
    assert flavour("{date} В ОФИС {office} НЕМЕДЛЕННО ВСЕ СОБРАЛИСЬ:") is None


def test_a_short_all_caps_acronym_is_fine() -> None:
    assert flavour("{date}, {office}, HR ждёт:") is not None


def test_an_overlong_line_is_refused() -> None:
    assert flavour("{date} {office} " + "очень длинно " * 60) is None


def test_an_empty_line_is_refused() -> None:
    assert flavour("   ") is None


def test_markup_from_the_model_is_escaped_not_executed() -> None:
    result = flavour("{date} <b>в</b> {office}:")
    assert result is not None
    assert "&lt;b&gt;" in result


def test_quotes_the_model_puts_round_the_office_token_are_dropped() -> None:
    """We add our own « », so a model that quoted the token would double them up."""
    result = flavour("{date} в «{office}»:")
    assert result is not None
    assert "««" not in result
    assert "«O&#x27;Vest»" in result


# -------------------------------------------------------------------- mood selection


def test_moods_are_stable_within_a_day_and_vary_across_days() -> None:
    policy = MoodPolicy(enabled=True, weights=dict.fromkeys(Mood, 1))
    first = policy.mood_for(office_id="ovest", day=DAY)

    assert policy.mood_for(office_id="ovest", day=DAY) is first
    moods = {policy.mood_for(office_id="ovest", day=date(2026, 9, d)) for d in range(1, 29)}
    assert len(moods) > 1


def test_two_offices_can_be_in_different_moods() -> None:
    policy = MoodPolicy(enabled=True, weights=dict.fromkeys(Mood, 1))
    moods = {policy.mood_for(office_id=name, day=DAY) for name in ("a", "b", "c", "d", "e")}
    assert len(moods) > 1


def test_safe_mode_forces_the_gentlest_mood() -> None:
    policy = MoodPolicy(enabled=True, weights={Mood.TOXIC: 100}, safe_mode=True)
    assert policy.mood_for(office_id="ovest", day=DAY) is SAFE_MOOD


def test_disabling_personality_forces_the_gentlest_mood() -> None:
    policy = MoodPolicy(enabled=False, weights={Mood.TOXIC: 100})
    assert policy.mood_for(office_id="ovest", day=DAY) is SAFE_MOOD


def test_a_variant_is_stable_for_the_day() -> None:
    lines = {Mood.TOXIC: ("a {date} {office}", "b {date} {office}", "c {date} {office}")}
    cat = Catalog(common=COMMON, variants={"attendance.intro": lines})
    first = cat.variant("attendance.intro", Mood.TOXIC, office_id="ovest", day=DAY)
    assert cat.variant("attendance.intro", Mood.TOXIC, office_id="ovest", day=DAY) == first


def test_an_unknown_key_fails_loudly() -> None:
    with pytest.raises(KeyError):
        catalog().variant("nope", Mood.TOXIC, office_id="ovest", day=DAY)


# ------------------------------------------------------------------ the Voice façade


class StubModel:
    def __init__(self, reply: str | None) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str, **_kwargs) -> str | None:
        self.calls.append((system, user))
        return self.reply


async def intro(model, mood=Mood.TOXIC, **kwargs) -> str:
    voice = Voice(catalog=catalog(), moods=MoodPolicy(weights=dict.fromkeys(Mood, 1)), model=model)
    return await voice.intro(
        "attendance.intro",
        mood,
        office_id="ovest",
        office_name="O'Vest",
        day=DAY,
        date_text="16 сентября 2026 года",
        **kwargs,
    )


async def test_a_good_model_reply_is_used() -> None:
    result = await intro(StubModel("Внимание, {date}, офис {office}. Идут:"))
    assert result.startswith("Внимание, 16 сентября")


async def test_a_bad_model_reply_falls_back_to_the_hand_written_line() -> None:
    result = await intro(StubModel("Сегодня все идут в офис!"))
    assert result.startswith("[toxic]")


async def test_a_model_failure_falls_back_silently() -> None:
    assert (await intro(StubModel(None))).startswith("[toxic]")


async def test_without_a_model_the_hand_written_line_is_used() -> None:
    assert (await intro(None)).startswith("[toxic]")


async def test_the_model_is_never_told_the_date_or_the_office() -> None:
    """The guard that makes a hallucination harmless: it cannot state a fact it was
    never given."""
    model = StubModel("{date} {office}:")
    await intro(model)

    system, user = model.calls[0]
    assert "16 сентября" not in system + user
    assert "O'Vest" not in system + user
    assert "ovest" not in (system + user).lower()


async def test_the_prompt_carries_the_mood_persona() -> None:
    model = StubModel("{date} {office}:")
    await intro(model, mood=Mood.DEPRESSIVE)
    assert "Табельщик" in model.calls[0][0]


async def test_every_mood_produces_a_usable_line_without_a_model() -> None:
    for mood in Mood:
        assert (await intro(None, mood=mood)).startswith(f"[{mood.value}]")
