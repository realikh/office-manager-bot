"""Tests for the bot's voice, and especially for what it refuses to say."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from tabelshchik.application.ports import Completion
from tabelshchik.application.voice import (
    EMOJI_CHOICES,
    SAFE_EMOJI,
    Announcement,
    Catalog,
    CommonText,
    MoodPolicy,
    Voice,
    emoji_choices,
    format_date,
    format_long_date,
    lead_in,
    plural_minutes,
    render_announcement,
    render_days,
    render_tail,
    render_template,
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
    base = {
        "attendance.intro": {
            mood: (f"[{mood.value}] {{when}}, {{date}}, {{office}}. {{tail}}",) for mood in Mood
        },
        "attendance.tails": {mood: (f"[запасной-{mood.value}]",) for mood in Mood},
    }
    base.update(variants)
    return Catalog(
        common=COMMON,
        variants=base,
        per_mood={"ai.persona": dict.fromkeys(Mood, "Ты — Табельщик.")},
    )


def tail(raw: str, **kwargs) -> str | None:
    return render_tail(raw, office_name="O'Vest", common=COMMON, **kwargs)


# ------------------------------------------------------------------------ formatting


def test_dates_are_rendered_in_russian() -> None:
    assert format_date(DAY, COMMON) == "16 сентября 2026 года"


def test_a_long_date_leads_with_the_weekday() -> None:
    assert format_long_date(DAY, COMMON) == "Среда, 16 сентября 2026 года"


def test_tomorrow_is_called_tomorrow() -> None:
    assert lead_in(date(2026, 9, 15), DAY, COMMON) == "Завтра"


def test_a_further_day_is_named_by_its_weekday_in_the_accusative() -> None:
    # Friday reminding about Monday: "В понедельник", not "В понедельника".
    assert lead_in(date(2026, 9, 18), date(2026, 9, 21), COMMON) == "В понедельник"
    # Feminine weekdays do decline: среда -> в среду.
    assert lead_in(date(2026, 9, 15), date(2026, 9, 16), COMMON) == "Завтра"
    assert lead_in(date(2026, 9, 14), date(2026, 9, 16), COMMON) == "В среду"


def test_the_weekday_and_the_date_stay_in_separate_tokens() -> None:
    """The whole point of the split.

    They used to be concatenated into one `{date}`, which meant every template had to be
    written around a string starting with a capitalised "В понедельник".
    """
    rendered = render_template(
        "{when}, {date} — офис {office}. {tail}",
        when=lead_in(date(2026, 9, 18), date(2026, 9, 21), COMMON),
        date_text=format_date(date(2026, 9, 21), COMMON),
        office_name="O'Vest",
        tail="Кофе остывает.",
    )
    assert rendered == ("В понедельник, 21 сентября 2026 года — офис «O&#x27;Vest». Кофе остывает.")
    assert rendered.count("понедельник") == 1


def test_templates_substitute_and_escape() -> None:
    rendered = render_template(
        "{date} в {office}", date_text="16 сентября", office_name="O'Vest & Co"
    )
    assert rendered == "16 сентября в «O&#x27;Vest &amp; Co»"


# ------------------------------------------------------------------------ tail guards


def test_a_well_formed_tail_is_accepted() -> None:
    """Capitalised and punctuated: the tail closes the opening sentence."""
    assert tail("кофемашина уже нервничает") == "Кофемашина уже нервничает."


def test_existing_punctuation_is_kept_rather_than_doubled() -> None:
    assert tail("собирайтесь!") == "Собирайтесь!"


def test_a_tail_naming_a_weekday_is_refused() -> None:
    """This is the bug. The header already says which day it is; a model that adds its
    own produces "В понедельник В понедельник, 14 сентября"."""
    assert tail("снова понедельник, как всегда") is None
    # Declined forms too — Russian inflects, and the guard matches on stems.
    assert tail("доживём до понедельника") is None


def test_a_tail_naming_a_month_or_saying_tomorrow_is_refused() -> None:
    assert tail("сентябрь выдался тяжёлым") is None
    assert tail("завтра будет не легче") is None


def test_a_tail_with_a_number_is_refused() -> None:
    """A digit is how an invented date, headcount or statistic gets in."""
    assert tail("ждём всех 14 человек") is None


def test_a_tail_naming_the_office_is_refused() -> None:
    """The office name is placed by us, in quotes. A second one reads as a stutter."""
    assert tail("в O'Vest снова весело") is None


def test_a_tail_may_not_name_an_employee() -> None:
    assert tail("особенно ждём Alikhan", forbidden=["Alikhan Khassen"]) is None


def test_a_declined_russian_name_is_still_caught() -> None:
    """Matching is on stems because Russian names inflect: Аня becomes Аню."""
    assert tail("особенно ждём Аню", forbidden=["Аня"]) is None
    assert tail("ждём Дмитрия", forbidden=["Дмитрий"]) is None


def test_a_tail_may_not_mention_anyone() -> None:
    assert tail("привет @realikh") is None


def test_banned_terms_are_refused() -> None:
    assert tail("дурацкий сбор", banned_terms=["дурацк"]) is None


def test_shouting_is_refused() -> None:
    assert tail("ВСЕ НЕМЕДЛЕННО СОБРАЛИСЬ И ПОШЛИ") is None


def test_a_short_all_caps_acronym_is_fine() -> None:
    assert tail("HR ждёт") is not None


def test_an_overlong_tail_is_refused() -> None:
    assert tail("очень длинно " * 60) is None


def test_an_empty_tail_is_refused() -> None:
    assert tail("   ") is None


def test_markup_from_the_model_is_refused_outright() -> None:
    assert tail("<b>жирно</b>") is None


def test_a_stray_brace_is_refused() -> None:
    """A brace would survive into the final text and read as a failed substitution."""
    assert tail("привет {кто-то}") is None


def test_a_multiline_answer_is_collapsed_rather_than_rejected() -> None:
    assert tail("кофемашина\n уже   нервничает") == "Кофемашина уже нервничает."


# ----------------------------------------------------------------- the day-list render


def test_days_are_rendered_one_person_per_row() -> None:
    chunks = render_days([(DAY, ["Adilet Askar", "Alikhan Khassen"])], COMMON)
    assert chunks == ["<b>Среда, 16 сентября 2026 года</b>\nAdilet Askar\nAlikhan Khassen"]


def test_an_empty_day_is_skipped_entirely() -> None:
    assert render_days([(DAY, [])], COMMON) == []


def test_a_long_horizon_is_split_rather_than_truncated() -> None:
    """Telegram silently refuses a message over 4096 characters, so splitting is the
    difference between a long preview and no preview."""
    days = [(date(2026, 9, d), [f"Человек Номер {n}" for n in range(20)]) for d in range(1, 29)]
    chunks = render_days(days, COMMON)
    assert len(chunks) > 1
    assert all(len(chunk) <= 4000 for chunk in chunks)
    # Nothing is lost in the splitting.
    assert sum(chunk.count("Человек") for chunk in chunks) == 28 * 20


def test_names_are_escaped_in_the_day_list() -> None:
    assert "&lt;b&gt;" in render_days([(DAY, ["<b>x</b>"])], COMMON)[0]


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
    lines = {Mood.TOXIC: ("a", "b", "c")}
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

    async def complete(self, system: str, user: str, **_kwargs) -> Completion | None:
        self.calls.append((system, user))
        return Completion(text=self.reply) if self.reply is not None else None


def voice(model) -> Voice:
    return Voice(catalog=catalog(), moods=MoodPolicy(weights=dict.fromkeys(Mood, 1)), model=model)


async def phrase(model, mood=Mood.TOXIC, **kwargs) -> str:
    return await voice(model).tail(mood, office_id="ovest", office_name="O'Vest", day=DAY, **kwargs)


def reply(tail_text: str) -> str:
    return json.dumps({"tail": tail_text}, ensure_ascii=False)


async def test_a_good_model_reply_is_used() -> None:
    assert await phrase(StubModel(reply("кофе стынет"))) == "Кофе стынет."


async def test_a_bad_phrase_falls_back_to_the_written_one() -> None:
    assert await phrase(StubModel(reply("снова понедельник"))) == "[запасной-toxic]."


async def test_a_model_failure_falls_back_silently() -> None:
    assert await phrase(StubModel(None)) == "[запасной-toxic]."


async def test_prose_instead_of_json_falls_back() -> None:
    assert await phrase(StubModel("Конечно! Вот ваша строка.")) == "[запасной-toxic]."


async def test_json_in_a_code_fence_is_still_read() -> None:
    fenced = "```json\n" + reply("кофе стынет") + "\n```"
    assert await phrase(StubModel(fenced)) == "Кофе стынет."


async def test_a_hand_written_tail_is_punctuated_like_a_generated_one() -> None:
    """The tail closes the opening sentence either way, so it has to end like one."""
    assert (await phrase(None)).endswith(".")


async def test_without_a_model_the_hand_written_text_is_used() -> None:
    assert await phrase(None) == "[запасной-toxic]."


async def test_the_model_is_never_told_the_date_the_office_or_a_name() -> None:
    """The guard that makes a hallucination harmless: it cannot state a fact it was
    never given."""
    model = StubModel(reply("кофе стынет"))
    await phrase(model, forbidden=["Alikhan Khassen"])

    system, user = model.calls[0]
    prompt = system + user
    assert "16 сентября" not in prompt
    assert "O'Vest" not in prompt
    assert "ovest" not in prompt.lower()
    assert "Alikhan" not in prompt
    assert "среда" not in prompt.lower()


async def test_the_prompt_carries_the_mood_persona() -> None:
    model = StubModel(reply("кофе стынет"))
    await phrase(model, mood=Mood.DEPRESSIVE)
    assert "Табельщик" in model.calls[0][0]


async def test_the_model_is_asked_for_the_phrase_and_no_titles() -> None:
    model = StubModel(reply("кофе стынет"))
    await phrase(model)

    system, user = model.calls[0]
    assert '{"tail": "..."}' in system
    assert "epithets" not in system
    assert "титул" not in system
    assert user


def test_the_icons_on_offer_change_from_day_to_day() -> None:
    today = emoji_choices(office_id="ovest", day=DAY)
    assert len(today) == EMOJI_CHOICES
    assert set(today) <= SAFE_EMOJI
    assert emoji_choices(office_id="ovest", day=DAY) == today
    assert emoji_choices(office_id="ovest", day=DAY + timedelta(days=1)) != today


async def test_the_prompt_offers_only_todays_icons_and_names_no_topics() -> None:
    model = StubModel(reply("кофе стынет"))
    await phrase(model)

    system = model.calls[0][0]
    offered = set(emoji_choices(office_id="ovest", day=DAY))
    assert all(icon in system for icon in offered)
    assert not any(icon in system for icon in SAFE_EMOJI - offered)
    assert "кофе" not in system.lower()
    assert "дорог" not in system.lower()


async def test_every_mood_produces_usable_text_without_a_model() -> None:
    for mood in Mood:
        assert await phrase(None, mood=mood) == f"[запасной-{mood.value}]."


# -------------------------------------------------------------------- the icon allowlist


def test_the_allowlist_holds_single_codepoints_only() -> None:
    """Membership is an exact string match, so a multi-codepoint entry would be
    unreachable — present in the list and never matching anything."""
    assert all(len(icon) == 1 for icon in SAFE_EMOJI)


# ------------------------------------------------------------------ whole messages


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (1, "1 минуту"),
        (2, "2 минуты"),
        (4, "4 минуты"),
        (5, "5 минут"),
        (10, "10 минут"),
        (11, "11 минут"),
        (14, "14 минут"),
        (21, "21 минуту"),
        (22, "22 минуты"),
        (25, "25 минут"),
        (30, "30 минут"),
    ],
)
def test_minutes_are_declined_for_the_number(count: int, expected: str) -> None:
    assert plural_minutes(count, COMMON) == expected


def test_the_minutes_token_is_filled_and_escaped() -> None:
    assert render_template("Потратьте {minutes}.", minutes="10 минут") == "Потратьте 10 минут."
    assert render_template("С праздником — {holiday}!", holiday="«<b>»") == (
        "С праздником — «&lt;b&gt;»!"
    )


def announcement(raw: str, **kwargs) -> str | None:
    return render_announcement(raw, office_name="O'Vest", common=COMMON, **kwargs)


def test_a_whole_message_is_accepted_and_escaped() -> None:
    assert announcement("Неделя позади & Tempo ждёт") == "Неделя позади &amp; Tempo ждёт."


def test_todays_weekday_may_be_named_when_allowed() -> None:
    line = "С пятницей! Заполните Tempo."
    assert announcement(line) is None
    assert announcement(line, allow_words=["пятница"]) == line


def test_allowing_one_weekday_does_not_allow_the_others() -> None:
    assert announcement("До понедельника! Tempo.", allow_words=["пятница"]) is None


def test_only_the_numbers_given_are_allowed() -> None:
    assert announcement("Потратьте 10 минут на Tempo.", allow_numbers=[10]) is not None
    assert announcement("Потратьте 15 минут на Tempo.", allow_numbers=[10]) is None
    assert announcement("Потратьте 100 минут на Tempo.", allow_numbers=[10]) is None


@pytest.mark.parametrize(
    "line",
    [
        "Заполните на https://tempo.example",
        "Зайдите на tempo.kz и заполните",
        "Откройте www.tempo и заполните",
    ],
)
def test_a_message_with_its_own_link_is_refused(line: str) -> None:
    assert announcement(line) is None


def test_a_message_longer_than_a_chat_message_should_be_is_refused() -> None:
    assert announcement("Очень длинно. " * 60) is None


async def test_announce_uses_the_model_when_it_behaves() -> None:
    model = StubModel(json.dumps({"text": "Неделя позади, Tempo ждёт"}, ensure_ascii=False))

    result = await voice(model).announce(
        Mood.TOXIC,
        brief="задание",
        fallback="запасной",
        office_name="O'Vest",
        office_id="ovest",
        day=DAY,
    )

    assert result.generated
    assert result.text == "Неделя позади, Tempo ждёт."
    system, user = model.calls[0]
    assert "Ты — Табельщик." in system
    assert user == "задание"


@pytest.mark.parametrize(
    "reply_text",
    [None, "не json", json.dumps({"other": "x"}), json.dumps({"text": "Снова понедельник"})],
)
async def test_announce_falls_back_when_the_model_does_not(reply_text) -> None:
    result = await voice(StubModel(reply_text)).announce(
        Mood.TOXIC,
        brief="задание",
        fallback="запасной",
        office_name="O'Vest",
        office_id="ovest",
        day=DAY,
    )
    assert result == Announcement("запасной")


async def test_announce_without_a_model_is_the_fallback() -> None:
    result = await voice(None).announce(
        Mood.TOXIC,
        brief="задание",
        fallback="запасной",
        office_name="O'Vest",
        office_id="ovest",
        day=DAY,
    )
    assert result == Announcement("запасной")
