"""Command-line parsing.

The Dockerfile's CMD is a runtime argument list that nothing else type-checks, so it is
asserted here against the real parser. Getting it wrong crash-loops the container on
startup, which is a slow and confusing way to find a typo.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tabelshchik.__main__ import build_parser


def parse(*argv: str):
    return build_parser().parse_args(list(argv))


def dockerfile_command() -> list[str]:
    """ENTRYPOINT plus CMD, exactly as Docker would combine them."""
    text = Path("Dockerfile").read_text(encoding="utf-8")

    def json_array(directive: str) -> list[str]:
        match = re.search(rf"^{directive}\s+(\[.*\])\s*$", text, re.M)
        assert match, f"no {directive} found in the Dockerfile"
        return list(json.loads(match.group(1)))

    return json_array("ENTRYPOINT") + json_array("CMD")


def test_the_dockerfile_command_actually_parses() -> None:
    entry, *argv = dockerfile_command()

    assert entry == "tabelshchik"
    args = parse(*argv)

    assert args.command == "run"
    assert args.json_logs is True


def test_the_healthcheck_command_parses() -> None:
    text = Path("Dockerfile").read_text(encoding="utf-8")
    match = re.search(r"CMD\s+(\[\"tabelshchik\".*\])", text)
    assert match, "no healthcheck command found"
    _entry, *argv = json.loads(match.group(1))
    assert parse(*argv).command == "validate"


# ------------------------------------------------------- flags on either side


@pytest.mark.parametrize("argv", [("run", "--json-logs"), ("--json-logs", "run")])
def test_json_logs_works_before_or_after_the_subcommand(argv: tuple[str, ...]) -> None:
    """Both orders are natural to type, and a flag that parses but is ignored is worse
    than one that is refused."""
    assert parse(*argv).json_logs is True


@pytest.mark.parametrize("argv", [("run", "--log-level", "DEBUG"), ("--log-level", "DEBUG", "run")])
def test_log_level_works_before_or_after_the_subcommand(argv: tuple[str, ...]) -> None:
    assert parse(*argv).log_level == "DEBUG"


def test_paths_work_before_or_after_the_subcommand() -> None:
    assert parse("--db", "/data/x.db", "run").db == Path("/data/x.db")
    assert parse("run", "--db", "/data/x.db").db == Path("/data/x.db")


def test_defaults_apply_when_nothing_is_passed() -> None:
    args = parse("run")
    assert args.json_logs is False
    assert args.log_level == "INFO"
    assert args.config is None and args.db is None


# ------------------------------------------------------------------ commands


@pytest.mark.parametrize(
    "argv",
    [
        ("run",),
        ("validate",),
        ("dry-run",),
        ("simulate",),
        ("regenerate", "--office", "ovest"),
        ("preview", "--office", "ovest"),
    ],
)
def test_every_command_parses(argv: tuple[str, ...]) -> None:
    assert parse(*argv).command == argv[0]


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit):
        parse()


def test_an_unknown_command_is_refused() -> None:
    with pytest.raises(SystemExit):
        parse("deploy")


def test_regenerate_requires_an_office() -> None:
    with pytest.raises(SystemExit):
        parse("regenerate")


# --------------------------------------------------------------- deployment wiring


def compose() -> dict[str, Any]:
    import yaml

    parsed = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def test_the_database_lives_on_a_named_volume_not_a_bind_mount() -> None:
    """A bind mount cannot work: the image runs as an unprivileged uid that will not
    match the host directory's owner, and the first boot fails with "unable to open
    database file". Docker owns a named volume, so the uid always lines up."""
    mounts = compose()["services"]["tabelshchik"]["volumes"]
    data_mount = next(m for m in mounts if m.split(":")[1] == "/data")
    source = data_mount.split(":")[0]

    assert not source.startswith((".", "/")), f"/data is bind-mounted from {source!r}"
    assert source in compose()["volumes"]


def test_the_config_mount_is_read_only() -> None:
    """Read-only means host ownership never matters, and config cannot drift from git."""
    mounts = compose()["services"]["tabelshchik"]["volumes"]
    config_mount = next(m for m in mounts if "/app/config" in m)
    assert config_mount.endswith(":ro")


def test_the_container_restarts_itself() -> None:
    assert compose()["services"]["tabelshchik"]["restart"] == "unless-stopped"


def test_the_database_path_is_inside_the_volume() -> None:
    """The env default and the mount point have to agree, or the volume holds nothing."""
    text = Path("Dockerfile").read_text(encoding="utf-8")
    assert "TABELSHCHIK_DB=/data/" in text

    mounts = compose()["services"]["tabelshchik"]["volumes"]
    assert any(m.split(":")[1] == "/data" for m in mounts)
