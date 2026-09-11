"""Loading and validating YAML configuration.

Validation errors are raised at the boundary with enough context to fix them without
reading the source — the file, the key path, and what was wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from tabelshchik.config.models import AppConfig, OfficeSeed

APP_FILE = "app.yaml"
OFFICES_DIR = "offices"


class ConfigError(Exception):
    """Configuration is invalid. The message is meant to be shown to a human."""


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    app: AppConfig
    offices: tuple[OfficeSeed, ...]

    def office(self, office_id: str) -> OfficeSeed | None:
        return next((office for office in self.offices if office.id == office_id), None)

    @property
    def shared_chat_ids(self) -> frozenset[int]:
        """Chats that more than one office posts into."""
        seen: dict[int, int] = {}
        for office in self.offices:
            if office.chat_id is not None:
                seen[office.chat_id] = seen.get(office.chat_id, 0) + 1
        return frozenset(chat for chat, count in seen.items() if count > 1)


def load(config_dir: Path) -> LoadedConfig:
    """Load and validate the whole configuration directory."""
    if not config_dir.is_dir():
        raise ConfigError(f"configuration directory not found: {config_dir}")

    app = parse_file(config_dir / APP_FILE, AppConfig)
    offices = load_offices(config_dir / OFFICES_DIR)
    _check_across_offices(offices)

    return LoadedConfig(app=app, offices=offices)


def load_offices(offices_dir: Path) -> tuple[OfficeSeed, ...]:
    if not offices_dir.is_dir():
        raise ConfigError(f"offices directory not found: {offices_dir}")

    paths = sorted(p for p in offices_dir.glob("*.yaml") if not p.name.startswith("."))
    if not paths:
        raise ConfigError(f"no office configuration files in {offices_dir}")

    return tuple(parse_file(path, OfficeSeed) for path in paths)


def parse_file[ModelT: BaseModel](path: Path, model: type[ModelT]) -> ModelT:
    if not path.is_file():
        raise ConfigError(f"missing configuration file: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ConfigError(f"{path.name} is not valid YAML: {error}") from error

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name} must contain a mapping at the top level")

    return parse_mapping(raw, model, source=path.name)


def parse_mapping[ModelT: BaseModel](
    raw: dict[str, Any], model: type[ModelT], *, source: str
) -> ModelT:
    try:
        return model.model_validate(raw)
    except ValidationError as error:
        raise ConfigError(f"{source}:\n{_describe(error)}") from error


def _describe(error: ValidationError) -> str:
    lines = []
    for issue in error.errors():
        location = ".".join(str(part) for part in issue["loc"]) or "(root)"
        message = issue["msg"]
        if issue["type"] == "extra_forbidden":
            # The most common real-world mistake, and the one a permissive loader would
            # swallow: a misspelled key that silently does nothing.
            message = "unknown setting — check the spelling"
        lines.append(f"  {location}: {message}")
    return "\n".join(lines)


def _check_across_offices(offices: tuple[OfficeSeed, ...]) -> None:
    """Rules that only make sense once every office is in view."""
    _require_unique(
        [(office.id, office.id) for office in offices],
        label="office id",
    )
    _require_unique(
        [
            (employee.id, f"{office.id}/{employee.id}")
            for office in offices
            for employee in office.employees
        ],
        label="employee id",
        hint="an employee belongs to exactly one office",
    )
    _require_unique(
        [
            (employee.telegram_username.lower(), f"{office.id}/{employee.id}")
            for office in offices
            for employee in office.employees
            if employee.telegram_username
        ],
        label="telegram username",
    )

    # Chat ids are deliberately NOT required to be unique. Two offices may share one
    # Telegram group; messages into a shared chat carry an office header so they stay
    # distinguishable, and `LoadedConfig.shared_chat_ids` reports which chats those are.


def _require_unique(pairs: list[tuple[Any, str]], *, label: str, hint: str = "") -> None:
    seen: dict[Any, str] = {}
    for key, where in pairs:
        if key in seen:
            suffix = f" ({hint})" if hint else ""
            raise ConfigError(f"duplicate {label} {key!r}: used by {seen[key]} and {where}{suffix}")
        seen[key] = where
