"""Secrets and paths, from the environment only.

Deliberately separate from the YAML config: a token in a file that ends up in git is a
different kind of mistake from a mistyped reminder time, and keeping the two apart makes
the config safe to commit.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Secrets:
    telegram_bot_token: str = ""
    openai_api_key: str = ""
    admin_chat_id: int | None = None
    admin_ids: frozenset[int] = field(default_factory=frozenset)

    @property
    def has_telegram(self) -> bool:
        return bool(self.telegram_bot_token)


def load_secrets(environ: dict[str, str] | None = None) -> Secrets:
    source = environ if environ is not None else dict(os.environ)
    return Secrets(
        telegram_bot_token=source.get("TELEGRAM_BOT_TOKEN", "").strip(),
        openai_api_key=source.get("OPENAI_API_KEY", "").strip(),
        admin_chat_id=_optional_int(source.get("ADMIN_CHAT_ID")),
        admin_ids=_id_set(source.get("ADMIN_IDS")),
    )


def config_dir(environ: dict[str, str] | None = None) -> Path:
    source = environ if environ is not None else dict(os.environ)
    return Path(source.get("TABELSHCHIK_CONFIG", "config"))


def database_path(environ: dict[str, str] | None = None) -> Path:
    source = environ if environ is not None else dict(os.environ)
    return Path(source.get("TABELSHCHIK_DB", "data/tabelshchik.db"))


def _optional_int(raw: str | None) -> int | None:
    if not raw or not raw.strip():
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _id_set(raw: str | None) -> frozenset[int]:
    if not raw:
        return frozenset()
    found: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        value = _optional_int(part)
        if value is not None:
            found.add(value)
    return frozenset(found)
