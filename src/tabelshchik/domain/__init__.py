"""Pure domain: entities, calendar, fairness solver, ledger.

Nothing in this package performs I/O, imports a framework, or touches the clock.
Every function is deterministic given its arguments. This is enforced in CI by the
import-linter contracts in pyproject.toml.
"""
