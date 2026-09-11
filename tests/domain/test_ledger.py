from __future__ import annotations

from datetime import timedelta

from tabelshchik.domain.entities import SCALE, LedgerEntry
from tabelshchik.domain.ledger import (
    DayRecord,
    apply_day,
    merge_checkpoint,
    rebuild,
    spread_scaled,
    surplus_days,
)

from .conftest import ANCHOR


def record(offset: int, available: set[str], attended: set[str], desks: int = 2) -> DayRecord:
    day = ANCHOR + timedelta(days=offset)
    return DayRecord(
        day=day,
        weekday=day.weekday(),
        available=frozenset(available),
        entitlement_scaled=desks * SCALE // len(available),
        attended=frozenset(attended),
    )


def test_attending_credits_a_day_and_a_weekday() -> None:
    entries = apply_day({}, record(0, {"a", "b"}, {"a"}))
    assert entries["a"].days == 1
    assert entries["a"].per_weekday[0] == 1
    assert entries["b"].days == 0


def test_being_available_accrues_entitlement_whether_or_not_you_came() -> None:
    entries = apply_day({}, record(0, {"a", "b"}, {"a"}))
    assert entries["a"].entitlement_scaled == entries["b"].entitlement_scaled == SCALE


def test_surplus_is_days_minus_entitlement() -> None:
    entries = apply_day({}, record(0, {"a", "b"}, {"a"}))
    assert entries["a"].surplus_scaled == 0  # attended 1, owed 1
    assert entries["b"].surplus_scaled == -SCALE  # owed 1, attended none


def test_absence_accrues_nothing() -> None:
    """The whole NO_DEBT design: time away builds no obligation to make up."""
    entries: dict[str, LedgerEntry] = {}
    for offset in range(5):
        # "c" is never available, so never accrues.
        entries = apply_day(entries, record(offset, {"a", "b"}, {"a", "b"}))
    assert "c" not in entries
    assert entries["a"].surplus_scaled == entries["b"].surplus_scaled


def test_rebuild_reproduces_incremental_application_exactly() -> None:
    records = [
        record(0, {"a", "b", "c"}, {"a", "b"}),
        record(1, {"a", "b", "c"}, {"b", "c"}),
        record(2, {"a", "c"}, {"a", "c"}),
    ]

    incremental: dict[str, LedgerEntry] = {}
    for item in records:
        incremental = apply_day(incremental, item)

    # Exact equality, not approximate: the arithmetic is fixed-point for this reason.
    assert rebuild(records) == incremental


def test_rebuild_is_independent_of_record_order() -> None:
    records = [
        record(0, {"a", "b"}, {"a"}),
        record(1, {"a", "b"}, {"b"}),
        record(2, {"a", "b"}, {"a"}),
    ]
    assert rebuild(records) == rebuild(list(reversed(records)))


def test_rebuild_resumes_from_a_checkpoint() -> None:
    """What keeps the ledger rebuildable after the retention prune deletes old rows."""
    early = [record(0, {"a", "b"}, {"a"}), record(1, {"a", "b"}, {"b"})]
    late = [record(2, {"a", "b"}, {"a"}), record(3, {"a", "b"}, {"b"})]

    full = rebuild(early + late)
    checkpointed = rebuild(late, checkpoint=rebuild(early))

    assert checkpointed == full


def test_merging_a_checkpoint_sums_every_component() -> None:
    first = {
        "a": LedgerEntry(
            "a", days=3, entitlement_scaled=2 * SCALE, per_weekday=(1, 1, 1, 0, 0, 0, 0)
        )
    }
    second = {
        "a": LedgerEntry("a", days=2, entitlement_scaled=SCALE, per_weekday=(0, 0, 1, 1, 0, 0, 0))
    }

    merged = merge_checkpoint(first, second)["a"]

    assert merged.days == 5
    assert merged.entitlement_scaled == 3 * SCALE
    assert merged.per_weekday == (1, 1, 2, 1, 0, 0, 0)


def test_merging_adds_employees_absent_from_the_checkpoint() -> None:
    merged = merge_checkpoint({}, {"new": LedgerEntry("new", days=1)})
    assert merged["new"].days == 1


def test_spread_reports_the_gap_between_the_luckiest_and_unluckiest() -> None:
    entries = [
        LedgerEntry("a", days=5, entitlement_scaled=3 * SCALE),  # +2
        LedgerEntry("b", days=1, entitlement_scaled=3 * SCALE),  # -2
    ]
    assert spread_scaled(entries) == 4 * SCALE
    assert spread_scaled([]) == 0


def test_surplus_days_is_for_display_only() -> None:
    assert surplus_days(LedgerEntry("a", days=3, entitlement_scaled=SCALE // 2)) == 2.5


def test_entitlement_of_an_unavailable_roster_is_not_divided_by_zero() -> None:
    entries = apply_day({}, DayRecord(ANCHOR, 0, frozenset(), 0, frozenset()))
    assert entries == {}
