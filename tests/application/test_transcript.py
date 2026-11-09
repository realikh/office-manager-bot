from __future__ import annotations

from datetime import date, datetime

from tabelshchik.application import transcript
from tabelshchik.application.ports import CachedMessage

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
WIDE = transcript.Budget(messages=60, chars=6000, message_chars=1000)


def message(
    message_id: int,
    text: str,
    *,
    who: str = "Аня",
    parent: int | None = None,
    at: datetime | None = None,
) -> CachedMessage:
    return CachedMessage(
        message_id=message_id,
        author=who,
        text=text,
        reply_to_message_id=parent,
        at=at or datetime(2026, 11, 9, 13, message_id % 60),
    )


def render(selected, parents=None, budget=WIDE) -> str:
    return transcript.render(
        selected, parents=parents or {}, budget=budget, today=TODAY, months=MONTHS
    )


def test_many_short_messages_fit_where_a_few_long_ones_do() -> None:
    budget = transcript.Budget(messages=60, chars=2000, message_chars=1000)
    short = [message(index, "ок") for index in range(1, 51)]
    long = [message(index, "слово " * 150) for index in range(1, 51)]

    assert len(transcript.select(chain=(), recent=short, budget=budget)) == 50
    assert len(transcript.select(chain=(), recent=long, budget=budget)) == 2


def test_the_message_count_is_a_ceiling_even_for_one_word_replies() -> None:
    budget = transcript.Budget(messages=5, chars=6000, message_chars=1000)
    chosen = transcript.select(
        chain=(), recent=[message(index, "да") for index in range(1, 21)], budget=budget
    )
    assert [item.message_id for item in chosen] == [16, 17, 18, 19, 20]


def test_the_newest_messages_are_kept_when_the_budget_runs_out() -> None:
    budget = transcript.Budget(messages=60, chars=200, message_chars=1000)
    recent = [message(index, f"сообщение номер {index}") for index in range(1, 30)]
    chosen = transcript.select(chain=(), recent=recent, budget=budget)
    assert chosen[-1].message_id == 29
    assert chosen[0].message_id > 1


def test_the_reply_chain_goes_in_first_but_takes_at_most_half() -> None:
    budget = transcript.Budget(messages=60, chars=400, message_chars=1000)
    chain = [message(index, "старая ветка " * 3) for index in range(1, 11)]
    recent = [message(index, "свежее") for index in range(100, 104)]

    chosen = transcript.select(chain=chain, recent=recent, budget=budget)
    ids = [item.message_id for item in chosen]

    assert 10 in ids
    assert 1 not in ids
    assert {100, 101, 102, 103} <= set(ids)


def test_a_reply_between_two_other_people_is_linked_by_number() -> None:
    selected = [
        message(1, "Кто завтра в офисе?", who="Аня"),
        message(2, "Я и Боря", who="Вера", parent=1),
    ]
    text = render(selected)
    assert "#1 [13:01] Аня: Кто завтра в офисе?" in text
    assert "#2 [13:02] Вера (ответ на #1): Я и Боря" in text


def test_a_parent_outside_the_window_is_quoted_briefly() -> None:
    selected = [message(5, "Согласен", who="Боря", parent=2)]
    parents = {
        2: message(2, "Давайте перенесем обед на пятницу, " + "потому что " * 20, who="Вера")
    }

    text = render(selected, parents)

    assert '(ответ на сообщение, автор Вера: "Давайте перенесем обед' in text
    assert '…")' in text
    assert len(text.split('"')[1]) <= transcript.QUOTE_CHARS


def test_an_older_day_carries_its_date() -> None:
    text = render([message(1, "вчерашнее", at=datetime(2026, 11, 8, 18, 5))])
    assert "[8 ноября 18:05]" in text


def test_a_long_message_is_clipped_not_dropped() -> None:
    budget = transcript.Budget(messages=60, chars=6000, message_chars=40)
    text = render([message(1, "начало " + "болтовня " * 100)], budget=budget)
    assert "начало" in text
    assert "…" in text


def test_the_parents_worth_fetching_are_the_ones_not_already_shown() -> None:
    selected = [message(3, "a", parent=1), message(4, "b", parent=3)]
    assert transcript.missing_parents(selected, 2) == [1, 2]


def test_nothing_to_show_renders_nothing() -> None:
    assert render([]) == ""
