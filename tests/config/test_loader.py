from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

from tabelshchik.config.loader import (
    ConfigError,
    LoadedConfig,
    load,
    load_offices,
    parse_file,
    parse_mapping,
)
from tabelshchik.config.models import AppConfig, OfficeSeed

MINIMAL_APP = {"reminders": {"attendance": {"time": "15:30"}}}


def write(directory: Path, name: str, payload: dict) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return path


def office(**overrides) -> dict:
    base = {
        "id": "ovest",
        "name": "O'Vest",
        "chatId": -100123,
        "employees": [{"id": "anya", "name": "Аня", "telegramUsername": "anya1"}],
        "schedule": {"vacantDesks": {"friday": 2}},
    }
    return {**base, **overrides}


def setup(tmp_path: Path, *offices: dict, app: dict | None = None) -> Path:
    write(tmp_path, "app.yaml", app or MINIMAL_APP)
    for index, payload in enumerate(offices or (office(),)):
        write(tmp_path / "offices", f"{payload.get('id', index)}.yaml", payload)
    return tmp_path


def imported(tmp_path: Path, *offices: dict) -> LoadedConfig:
    """What `tabelshchik import-office` sees.

    Offices are no longer part of `load`: they live in the database. The cross-file rules
    still exist, and still run — in `load_offices`, wherever that is called from.
    """
    setup(tmp_path, *offices)
    return LoadedConfig(app=load(tmp_path).app, offices=load_offices(tmp_path / "offices"))


# ------------------------------------------------------------------------ happy paths


def test_loading_reads_the_app_settings_and_no_offices(tmp_path: Path) -> None:
    """Offices live in the database. Reading them here would mean deciding on every boot
    whether the files or the database win, and the answer has to be the database."""
    config = load(setup(tmp_path))
    assert config.app.timezone == "Asia/Almaty"
    assert config.offices == ()


def test_offices_are_still_parsable_for_an_import(tmp_path: Path) -> None:
    config = imported(tmp_path)
    assert [o.id for o in config.offices] == ["ovest"]
    assert config.office("ovest") is not None
    assert config.office("nope") is None


def test_a_deployment_without_any_office_files_still_loads(tmp_path: Path) -> None:
    """The shipped configuration has no `offices/` directory at all any more."""
    write(tmp_path, "app.yaml", MINIMAL_APP)
    assert load(tmp_path).offices == ()


def test_defaults_fill_in_everything_not_specified(tmp_path: Path) -> None:
    config = load(setup(tmp_path))
    assert config.app.schedule.horizon_weeks == 6
    assert config.app.ai.per_user_daily_limit == 10
    assert config.app.personality.moods["toxic"].weight == 55


# ------------------------------------------------------------------ failing loudly


def test_an_unknown_key_is_a_startup_error_not_a_silent_default(tmp_path: Path) -> None:
    """The old system failed open on typos, so a setting could look configured and do
    nothing for months."""
    with pytest.raises(ConfigError, match="unknown setting"):
        load(setup(tmp_path, app={**MINIMAL_APP, "timezon": "Asia/Almaty"}))


def test_the_error_names_the_file_and_the_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        load(setup(tmp_path, app={**MINIMAL_APP, "schedule": {"horizonWeeks": 0}}))
    assert "app.yaml" in str(caught.value)
    assert "schedule.horizonWeeks" in str(caught.value)


def test_missing_app_file_is_reported_clearly(tmp_path: Path) -> None:
    (tmp_path / "offices").mkdir()
    write(tmp_path / "offices", "ovest.yaml", office())
    with pytest.raises(ConfigError, match="missing configuration file"):
        load(tmp_path)


def test_importing_a_missing_offices_directory_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="offices directory not found"):
        load_offices(tmp_path / "offices")


def test_importing_an_empty_offices_directory_is_reported(tmp_path: Path) -> None:
    (tmp_path / "offices").mkdir()
    with pytest.raises(ConfigError, match="no office configuration"):
        load_offices(tmp_path / "offices")


def test_broken_yaml_is_reported_as_yaml(tmp_path: Path) -> None:
    (tmp_path / "offices").mkdir()
    (tmp_path / "offices" / "bad.yaml").write_text("id: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_offices(tmp_path / "offices")


def test_a_freeze_window_must_leave_something_to_plan(tmp_path: Path) -> None:
    app = {**MINIMAL_APP, "schedule": {"horizonWeeks": 2, "freezeWeeks": 2}}
    with pytest.raises(ConfigError, match="must be smaller than"):
        load(setup(tmp_path, app=app))


def test_every_mood_weighted_zero_is_rejected(tmp_path: Path) -> None:
    app = {**MINIMAL_APP, "personality": {"moods": {"toxic": {"weight": 0}}}}
    with pytest.raises(ConfigError, match="weight above zero"):
        load(setup(tmp_path, app=app))


# --------------------------------------------------------------- cross-office rules


def test_an_employee_may_only_belong_to_one_office(tmp_path: Path) -> None:
    second = office(id="pine", name="Pine", chatId=-100999)
    with pytest.raises(ConfigError, match="duplicate employee id"):
        imported(tmp_path, office(), second)


def test_telegram_usernames_must_be_unique_across_offices(tmp_path: Path) -> None:
    second = office(
        id="pine",
        name="Pine",
        chatId=-100999,
        employees=[{"id": "borya", "name": "Боря", "telegramUsername": "ANYA1"}],
    )
    with pytest.raises(ConfigError, match="duplicate telegram username"):
        imported(tmp_path, office(), second)


def test_two_offices_may_share_a_chat(tmp_path: Path) -> None:
    """Deliberately allowed: one group for several offices is a real setup. Messages
    into a shared chat carry an office header so the two stay distinguishable."""
    second = office(id="pine", name="Pine", employees=[{"id": "borya", "name": "Боря"}])

    assert imported(tmp_path, office(), second).shared_chat_ids == frozenset({-100123})


def test_offices_with_their_own_chats_are_not_reported_as_shared(tmp_path: Path) -> None:
    second = office(
        id="pine", name="Pine", chatId=-100999, employees=[{"id": "borya", "name": "Боря"}]
    )
    assert imported(tmp_path, office(), second).shared_chat_ids == frozenset()


def test_an_office_without_a_chat_is_not_shared_with_another(tmp_path: Path) -> None:
    """Two nulls are not a collision."""
    first = office(chatId=None)
    second = office(
        id="pine", name="Pine", chatId=None, employees=[{"id": "borya", "name": "Боря"}]
    )
    assert imported(tmp_path, first, second).shared_chat_ids == frozenset()


def test_offices_load_in_a_stable_order(tmp_path: Path) -> None:
    setup(
        tmp_path,
        office(id="zulu", chatId=-1, employees=[{"id": "zoya", "name": "Зоя"}]),
        office(id="alpha", chatId=-2, employees=[{"id": "alla", "name": "Алла"}]),
    )
    assert [o.id for o in load_offices(tmp_path / "offices")] == ["alpha", "zulu"]


# ------------------------------------------------------------------- office schema


def test_fixed_schedule_must_name_real_employees() -> None:
    payload = office(schedule={"fixed": {"monday": ["ghost"]}})
    with pytest.raises(ConfigError, match="unknown employees"):
        parse_mapping(payload, OfficeSeed, source="t.yaml")


def test_fixed_schedule_may_not_list_someone_twice() -> None:
    payload = office(schedule={"fixed": {"monday": ["anya", "anya"]}})
    with pytest.raises(ConfigError, match="lists someone twice"):
        parse_mapping(payload, OfficeSeed, source="t.yaml")


def test_absences_must_name_real_employees() -> None:
    payload = office(
        absences=[{"employeeId": "ghost", "startDate": "2026-01-01", "endDate": "2026-01-02"}]
    )
    with pytest.raises(ConfigError, match="absences name unknown"):
        parse_mapping(payload, OfficeSeed, source="t.yaml")


def test_an_absence_may_not_end_before_it_starts() -> None:
    payload = office(
        absences=[{"employeeId": "anya", "startDate": "2026-01-05", "endDate": "2026-01-01"}]
    )
    with pytest.raises(ConfigError, match="ends before it starts"):
        parse_mapping(payload, OfficeSeed, source="t.yaml")


def test_negative_vacant_desks_are_rejected() -> None:
    with pytest.raises(ConfigError, match="negative"):
        parse_mapping(office(schedule={"vacantDesks": {"friday": -1}}), OfficeSeed, source="t")


def test_employee_ids_must_look_like_slugs() -> None:
    payload = office(employees=[{"id": "Not A Slug", "name": "X"}])
    with pytest.raises(ConfigError, match=r"employees\.0\.id"):
        parse_mapping(payload, OfficeSeed, source="t.yaml")


def test_weekday_names_translate_to_numbers() -> None:
    seed = parse_mapping(
        office(schedule={"fixed": {"monday": ["anya"]}, "vacantDesks": {"friday": 3}}),
        OfficeSeed,
        source="t.yaml",
    )
    assert seed.schedule.fixed_by_weekday == {0: ("anya",)}
    assert seed.schedule.desks_by_weekday == {4: 3}


# ------------------------------------------------------------------- silent windows


@pytest.mark.parametrize(
    ("weekday", "moment", "expected"),
    [
        (1, dt.time(21, 0), True),  # after 20:00 on a weekday
        (1, dt.time(3, 0), True),  # the window crosses midnight
        (1, dt.time(8, 0), False),  # exactly when it lifts
        (1, dt.time(12, 0), False),
        (5, dt.time(12, 0), True),  # Saturday is silent all day
        (6, dt.time(9, 0), True),  # Sunday morning
        (6, dt.time(11, 0), False),  # Sunday after 10:00
    ],
)
def test_silent_hours(weekday: int, moment: dt.time, expected: bool) -> None:
    app = parse_mapping(
        {
            **MINIMAL_APP,
            "silentHours": {
                "default": {"from": "20:00", "to": "08:00"},
                "saturday": "all-day",
                "sunday": {"from": "00:00", "to": "10:00"},
            },
        },
        AppConfig,
        source="app.yaml",
    )
    assert app.silent_hours.is_silent(weekday, moment) is expected


def test_without_configuration_nothing_is_silent() -> None:
    app = parse_mapping(MINIMAL_APP, AppConfig, source="app.yaml")
    assert not app.silent_hours.is_silent(1, dt.time(23, 0))


# --------------------------------------------------------------- the shipped config


def test_the_shipped_configuration_is_valid() -> None:
    """The real config/ directory must always load — it ships with the bot.

    Both files. `messages.yaml` used to be checked only by `build_services`, so a broken
    one passed `validate` — including the `validate` step CI runs — and failed at boot.
    """
    from tabelshchik.config.messages import MessagesConfig

    config = load(Path("config"))
    assert config.app.timezone == "Asia/Almaty"
    assert parse_file(Path("config/messages.yaml"), MessagesConfig).common.weekdays
