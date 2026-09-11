"""What the bot writes down, and — mostly — what it does not.

All pure, so the rules about what is worth keeping and what is the same thing said twice
can be checked without a database or a model.
"""

from __future__ import annotations

from tabelshchik.application import memory


def distil(payload, chars: int = 120):
    return memory.distil(payload, max_chars=chars)


# ------------------------------------------------------------------------ distillation


def test_an_office_fact_is_kept_as_shared() -> None:
    result = distil({"scope": "office", "fact": "Стендап переехал на 10:30"})
    assert result is not None
    assert result.scope == memory.OFFICE
    assert result.fact == "Стендап переехал на 10:30"


def test_a_user_fact_is_kept_as_personal() -> None:
    result = distil({"scope": "user", "fact": "Работает из Алматы"})
    assert result is not None
    assert result.scope == memory.EMPLOYEE


def test_a_null_scope_means_nothing_is_worth_keeping() -> None:
    """Which is the common case: most messages in a work chat are not facts."""
    assert distil({"scope": None, "fact": "ага"}) is None
    assert distil({"scope": "", "fact": "ага"}) is None


def test_an_unrecognised_scope_is_discarded_rather_than_guessed() -> None:
    assert distil({"scope": "everyone", "fact": "что-то"}) is None


def test_an_empty_fact_is_discarded_whatever_the_scope_says() -> None:
    assert distil({"scope": "office", "fact": "   "}) is None


def test_a_non_object_payload_is_discarded() -> None:
    assert distil(None) is None
    assert distil("office") is None
    assert distil(["office", "fact"]) is None


def test_a_long_fact_is_truncated_on_a_word_boundary() -> None:
    result = distil({"scope": "office", "fact": "слово " * 60}, chars=40)
    assert result is not None
    assert len(result.fact) <= 41  # the ellipsis
    assert result.fact.endswith("…")
    assert "слов…" not in result.fact, "should cut between words, not through one"


def test_markup_and_line_breaks_are_flattened() -> None:
    """Tags go whole. Stripping only the angle brackets leaves the tag names behind in a
    fact that is read straight back into a later prompt."""
    result = distil({"scope": "office", "fact": "первая\nвторая  <b>третья</b>"})
    assert result is not None
    assert result.fact == "первая вторая третья"


# --------------------------------------------------------------------------- dedupe


def test_the_same_fact_in_the_same_words_is_redundant() -> None:
    assert memory.is_redundant("Стендап в 10:30", ["Стендап в 10:30"])


def test_the_same_fact_in_different_words_is_redundant() -> None:
    assert memory.is_redundant("Алик сидит возле окна", ["Алик сидит у окна и любит тишину"])


def test_a_different_fact_about_the_same_person_is_kept() -> None:
    assert not memory.is_redundant("Алик не ест мясо", ["Алик сидит у окна"])


def test_an_empty_fact_is_always_redundant() -> None:
    assert memory.is_redundant("   ", [])


def test_nothing_known_yet_means_nothing_is_redundant() -> None:
    assert not memory.is_redundant("Стендап в 10:30", [])


# --------------------------------------------------------------------------- render


def test_both_scopes_are_labelled_separately() -> None:
    rendered = memory.render(["Стендап в 10:30"], ["Работает из Алматы"])
    assert "Что ты знаешь об этом чате:" in rendered
    assert "Что ты знаешь о собеседнике:" in rendered
    assert "- Стендап в 10:30" in rendered


def test_a_missing_scope_leaves_no_empty_heading() -> None:
    assert "собеседнике" not in memory.render(["Стендап в 10:30"], [])
    assert "этом чате" not in memory.render([], ["Работает из Алматы"])


def test_no_facts_at_all_renders_nothing() -> None:
    assert memory.render([], []) == ""
