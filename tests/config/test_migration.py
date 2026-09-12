"""Tests for the one-off converter from the old office-rotation-bot configs."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from migrate_from_old_bot import Report, convert

TODAY = date(2026, 9, 11)

OLD = {
    "id": "ovest",
    "name": "O'Vest",
    "address": "улица Акмешит 1",
    "schedule": {"durationDays": 365, "seed": 20260804},
    "vacantDesks": {"friday": 11},
    "calendarOverrides": {"excludedDates": [date(2026, 12, 31)], "includedDates": []},
    "vacations": [
        {
            "employeeId": "alikhan-khassen",
            "startDate": date(2026, 10, 1),
            "endDate": date(2026, 10, 9),
        },
        {
            "employeeId": "alikhan-khassen",
            "startDate": date(2026, 8, 16),
            "endDate": date(2026, 8, 29),
        },
    ],
    "employeeGroups": [
        {
            "id": "mobile-developers",
            "employees": [
                {
                    "id": "alikhan-khassen",
                    "name": "Alikhan Khassen",
                    "telegramUsername": "realikh",
                    "gender": "male",
                },
                {
                    "id": "islam-sandybayev",
                    "name": "Islam Sandybayev",
                    "telegramUsername": "eislishere",
                    "gender": "male",
                    "lastWorkDate": date(2026, 8, 7),
                },
            ],
        },
        {
            "id": "backend-developers",
            "employees": [
                {
                    "id": "zhaniya-zhakipova",
                    "name": "Zhaniya Zhakipova",
                    "telegramUsername": "Kukasaurs",
                    "gender": "female",
                },
            ],
        },
    ],
}


def converted(raw=None, report=None):
    return convert(raw or OLD, report or Report(), today=TODAY)


def test_identity_and_address_carry_over() -> None:
    seed = converted()
    assert seed["id"] == "ovest"
    assert seed["name"] == "O'Vest"
    assert seed["address"] == "улица Акмешит 1"


def test_chat_id_is_left_blank_to_be_filled_in() -> None:
    """The old bot posted every office into one chat, so there is nothing to carry."""
    assert converted()["chatId"] is None


def test_employee_groups_flatten_but_keep_the_team() -> None:
    seed = converted()
    assert [e["id"] for e in seed["employees"]] == [
        "alikhan-khassen",
        "islam-sandybayev",
        "zhaniya-zhakipova",
    ]
    assert seed["employees"][0]["teamId"] == "mobile-developers"
    assert seed["employees"][2]["teamId"] == "backend-developers"


def test_last_work_date_becomes_ended_on() -> None:
    seed = converted()
    assert seed["employees"][1]["endedOn"] == date(2026, 8, 7)
    assert "endedOn" not in seed["employees"][0]


def test_gender_and_username_survive() -> None:
    person = converted()["employees"][2]
    assert person["gender"] == "female"
    assert person["telegramUsername"] == "Kukasaurs"


def test_past_vacations_are_dropped_and_future_ones_kept() -> None:
    seed = converted()
    assert len(seed["absences"]) == 1
    assert seed["absences"][0]["startDate"] == date(2026, 10, 1)


def test_vacations_for_unknown_people_are_dropped_with_a_warning() -> None:
    report = Report()
    raw = {
        **OLD,
        "vacations": [
            {"employeeId": "ghost", "startDate": date(2026, 10, 1), "endDate": date(2026, 10, 2)}
        ],
    }
    seed = converted(raw, report)
    assert "absences" not in seed
    assert any("unknown employee" in w for w in report.warnings)


def test_excluded_dates_become_closed_days() -> None:
    assert converted()["calendar"]["closed"] == [date(2026, 12, 31)]


def test_included_dates_become_extra_workdays() -> None:
    raw = {**OLD, "calendarOverrides": {"excludedDates": [], "includedDates": [date(2026, 11, 8)]}}
    assert converted(raw)["calendar"]["extraWorkdays"] == [date(2026, 11, 8)]


def test_vacant_desks_transfer_unchanged_when_there_is_no_fixed_schedule() -> None:
    """Without a fixed schedule the old 'total capacity' reading and the new 'extra
    draftees' reading coincide, so the number means the same thing."""
    report = Report()
    seed = converted(report=report)
    assert seed["schedule"]["vacantDesks"] == {"friday": 11}
    assert not report.warnings


def test_vacant_desks_are_reinterpreted_and_flagged_when_a_fixed_schedule_exists() -> None:
    """Where both exist the number changed meaning, so the operator must be told."""
    report = Report()
    raw = {
        **OLD,
        "vacantDesks": {"monday": 5},
        "fixedSchedule": {"monday": ["alikhan-khassen", "zhaniya-zhakipova"]},
    }
    seed = converted(raw, report)

    assert seed["schedule"]["vacantDesks"]["monday"] == 3  # 5 total minus 2 fixed
    assert any("extra draftees" in w for w in report.warnings)


def test_fixed_schedule_entries_for_unknown_people_are_dropped() -> None:
    report = Report()
    raw = {**OLD, "fixedSchedule": {"monday": ["alikhan-khassen", "ghost"]}}
    seed = converted(raw, report)

    assert seed["schedule"]["fixed"]["monday"] == ["alikhan-khassen"]
    assert any("not on the roster" in w for w in report.warnings)


def test_the_seed_nonce_is_derived_from_the_old_seed() -> None:
    assert converted()["seedNonce"] == 20260804 % 100_000


def test_a_config_without_an_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="no 'id'"):
        convert({"name": "Nameless"}, Report(), today=TODAY)


def test_the_result_passes_the_new_schema() -> None:
    """The point of the whole exercise: the output must actually load."""
    from tabelshchik.config.loader import parse_mapping
    from tabelshchik.config.models import OfficeSeed

    seed = parse_mapping(converted(), OfficeSeed, source="migrated.yaml")
    assert seed.id == "ovest"
    assert len(seed.employees) == 3
    assert seed.schedule.desks_by_weekday == {4: 11}


def test_the_converted_output_loads_as_a_directory_of_office_files(tmp_path: Path) -> None:
    """What `tabelshchik import-office` will be handed.

    This used to read `config/offices/` in the repository. Those files are gone — offices
    live in the database now — so the converter is checked against its own output, which
    is the thing being tested anyway.
    """
    import yaml

    from tabelshchik.config.loader import load_offices

    (tmp_path / "offices").mkdir()
    (tmp_path / "offices" / "ovest.yaml").write_text(
        yaml.safe_dump(converted(), allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    offices = load_offices(tmp_path / "offices")
    assert [office.id for office in offices] == ["ovest"]
    assert len(offices[0].employees) == 3
