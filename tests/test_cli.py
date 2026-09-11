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
    """Docker runs this every 30 seconds; a typo would report the bot dead forever."""
    match = re.search(r'CMD\s+(\["tabelshchik".*\])', Path("Dockerfile").read_text("utf-8"))
    assert match, "no healthcheck command found"

    _entry, *argv = json.loads(match.group(1))
    assert parse(*argv).command == "healthcheck"


def test_the_healthcheck_is_frequent_enough_to_be_useful_to_a_deploy() -> None:
    """A deploy waits for this verdict. At the old five-minute interval it would have
    given up long before the first check ran."""
    text = Path("Dockerfile").read_text("utf-8")
    interval = re.search(r"--interval=(\d+)s", text)
    assert interval, "healthcheck interval is not in seconds"
    assert int(interval.group(1)) <= 60


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


# ---------------------------------------------------------------- the deploy pipeline


def workflow() -> dict[str, Any]:
    import yaml

    parsed = yaml.safe_load(Path(".github/workflows/ci.yml").read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def test_deploying_requires_the_checks_to_pass() -> None:
    """The whole safety property: a red build must not reach the VM."""
    assert workflow()["jobs"]["deploy"]["needs"] == "check"


def test_only_main_deploys() -> None:
    condition = workflow()["jobs"]["deploy"]["if"]
    assert "refs/heads/main" in condition
    assert "push" in condition


def test_deploys_do_not_run_concurrently() -> None:
    """Two builds racing on one VM would interleave a git reset with a docker build."""
    concurrency = workflow()["jobs"]["deploy"]["concurrency"]
    assert concurrency["group"]
    # Cancelling mid-build would leave the VM holding a half-built image.
    assert concurrency["cancel-in-progress"] is False


def test_the_host_key_is_pinned() -> None:
    """Disabling host key checking would accept whatever answers on that address."""
    steps = workflow()["jobs"]["deploy"]["steps"]
    script = "\n".join(step.get("run", "") for step in steps)
    # Comments stripped: a line explaining why we do not do this is not doing it.
    code = "\n".join(line for line in script.splitlines() if not line.strip().startswith("#"))

    assert "known_hosts" in code
    assert "StrictHostKeyChecking=no" not in code
    assert "StrictHostKeyChecking=accept-new" not in code


def test_secrets_are_passed_by_env_not_interpolated_into_the_script() -> None:
    """Interpolating a key straight into `run` puts it in the shell's command line."""
    step = next(s for s in workflow()["jobs"]["deploy"]["steps"] if "run" in s)
    assert "secrets.DEPLOY_SSH_KEY" in str(step.get("env", {}))
    assert "secrets.DEPLOY_SSH_KEY" not in step["run"]


def test_the_deploy_script_is_executable_and_parses() -> None:
    import os
    import subprocess

    script = Path("scripts/deploy.sh")
    assert os.access(script, os.X_OK), "deploy.sh is not executable"
    subprocess.run(["bash", "-n", str(script)], check=True)


def test_the_deploy_script_survives_being_updated_while_it_runs() -> None:
    """It git-resets the repository it lives in, and bash reads scripts incrementally.
    Wrapping the body in a function forces a full parse before anything executes."""
    source = Path("scripts/deploy.sh").read_text(encoding="utf-8")
    assert "main() {" in source
    assert source.rstrip().endswith('main "$@"')


def test_the_deploy_script_refuses_a_dirty_tree() -> None:
    """A reset would silently discard a hand-edited office config."""
    source = Path("scripts/deploy.sh").read_text(encoding="utf-8")
    assert "git diff --quiet" in source


def test_alerts_do_not_go_through_the_bot() -> None:
    """The bot is what just failed, so the alert cannot depend on it."""
    source = Path("scripts/deploy.sh").read_text(encoding="utf-8")
    assert "api.telegram.org" in source
    assert "curl" in source
