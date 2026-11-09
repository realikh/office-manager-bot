from __future__ import annotations

from datetime import date, datetime

from tabelshchik.application import memory
from tabelshchik.application.ports import Note, NoteScope

TODAY = date(2026, 11, 9)
MONTHS = (
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
)


def writes(payload: object, *, limit: int = 3, max_chars: int = 200) -> list[memory.NoteWrite]:
    return memory.parse_writes(payload, limit=limit, max_chars=max_chars, today=TODAY)


def note(about: str | None = "me", topic: str = "день рождения", text: str = "3 марта", **extra):
    return {"about": about, "topic": topic, "text": text, **extra}


# ---------------------------------------------------------------------------- writes


def test_a_personal_note_is_kept_for_the_person() -> None:
    assert writes([note()]) == [memory.NoteWrite(NoteScope.PERSON, "день рождения", "3 марта")]


def test_an_office_note_is_kept_for_the_office() -> None:
    [written] = writes([note(about="office", topic="Кофемашина", text="Сломана")])
    assert written.scope is NoteScope.OFFICE
    assert written.topic == "кофемашина"


def test_an_unknown_scope_is_discarded_rather_than_guessed() -> None:
    assert writes([note(about="everyone")]) == []
    assert writes([note(about=None)]) == []


def test_a_note_without_a_topic_or_a_text_is_discarded() -> None:
    assert writes([note(topic="  ")]) == []
    assert writes([note(text="")]) == []


def test_anything_but_a_list_of_objects_is_nothing_to_keep() -> None:
    assert writes(None) == []
    assert writes({"about": "me"}) == []
    assert writes(["день рождения 3 марта"]) == []


def test_no_more_notes_than_allowed_are_kept_from_one_answer() -> None:
    many = [note(topic=f"тема {index}") for index in range(5)]
    assert len(writes(many, limit=3)) == 3
    assert writes(many, limit=0) == []


def test_a_topic_is_normalised_so_the_same_topic_matches() -> None:
    assert memory.normalise_topic("  День  Рождения! ") == "день рождения"
    assert memory.normalise_topic("<b>Кофе</b>-машина") == "кофе машина"


def test_a_long_topic_is_cut_on_a_word_boundary() -> None:
    topic = memory.normalise_topic("очень " * 20)
    assert len(topic) <= memory.TOPIC_CHARS
    assert topic.endswith("очень")


def test_a_long_text_is_truncated_on_a_word_boundary() -> None:
    [written] = writes([note(text="слово " * 100)], max_chars=30)
    assert len(written.text) <= 31
    assert written.text.endswith("…")
    assert "  " not in written.text


def test_markup_links_and_handles_are_stripped_from_a_note() -> None:
    [written] = writes([note(text="<b>Пишите</b> @boss или на https://evil.example/x сюда")])
    assert written.text == "Пишите или на сюда"


def test_a_temporary_note_keeps_its_end_date() -> None:
    [written] = writes([note(until="2026-11-20")])
    assert written.until == date(2026, 11, 20)


def test_an_end_date_in_the_past_far_ahead_or_garbled_is_dropped() -> None:
    for until in ("2026-11-01", "2028-01-01", "завтра", 20261120):
        [written] = writes([note(until=until)])
        assert written.until is None


# --------------------------------------------------------------------------- forgets


def test_a_forget_names_a_scope_and_a_topic() -> None:
    assert memory.parse_forgets([{"about": "office", "topic": "Кофемашина"}]) == [
        memory.NoteForget(NoteScope.OFFICE, "кофемашина")
    ]


def test_a_forget_without_a_topic_or_a_scope_is_ignored() -> None:
    assert memory.parse_forgets([{"about": "office"}, {"topic": "кофе"}, "кофе"]) == []


# ---------------------------------------------------------------- near-identical notes


def test_the_same_text_twice_is_near_identical() -> None:
    assert memory.is_near_identical("Стендап в 10:30", ["Стендап в 10:30"])


def test_a_different_person_with_the_same_words_is_not() -> None:
    assert not memory.is_near_identical(
        "У Бори день рождения 5 мая", ["У Пети день рождения 5 мая"]
    )


def test_a_different_number_is_not_the_same_fact() -> None:
    assert not memory.is_near_identical("Стендап в 10:30", ["Стендап в 11:30"])


def test_nothing_known_yet_means_nothing_is_near_identical() -> None:
    assert not memory.is_near_identical("Стендап в 10:30", [])


# -------------------------------------------------------------------------- rendering


def test_a_note_says_when_it_was_written_and_until_when_it_holds() -> None:
    line = memory.render_note(
        Note(
            id=1,
            scope=NoteScope.OFFICE,
            subject="ovest",
            topic="кофемашина",
            text="Сломана, ждут мастера",
            updated_at=datetime(2026, 11, 9, 10, 0),
            until=date(2026, 11, 20),
        ),
        MONTHS,
    )
    assert line == (
        "- кофемашина: Сломана, ждут мастера (записано 9 ноября 2026, до 20 ноября 2026)"
    )
