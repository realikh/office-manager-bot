"""Deterministic hashing.

Python's builtin ``hash()`` is salted per process for ``str``, so it must never reach
anything whose output is persisted or compared across runs. Everything seeded here goes
through blake2b, which is stable across runs, machines and interpreter versions.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

_DIGEST_BYTES = 8
_MASK = (1 << (_DIGEST_BYTES * 8)) - 1


def stable_hash(*parts: object) -> int:
    """A reproducible 64-bit hash of ``parts``, joined by a separator that cannot appear
    in the rendered pieces themselves."""
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    digest = hashlib.blake2b(payload, digest_size=_DIGEST_BYTES).digest()
    return int.from_bytes(digest, "big") & _MASK


def jitter(seed: int, *parts: object, modulo: int) -> int:
    """A stable pseudo-random value in ``[0, modulo)`` for this seed and key.

    Used only as the lowest-priority tie-break in the solver, so it can vary the choice
    among equally-fair schedules without ever trading away fairness.
    """
    if modulo <= 0:
        raise ValueError("modulo must be positive")
    return stable_hash(seed, *parts) % modulo


def shuffled(seed: int, items: Iterable[str]) -> list[str]:
    """A deterministic shuffle, used where a stable-but-varied order is wanted."""
    return sorted(items, key=lambda item: stable_hash(seed, item))
