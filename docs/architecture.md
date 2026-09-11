# Architecture notes

Written for whoever changes this next, including a future me. It records the decisions
that are load-bearing and the reasons they are the way they are — the things that look
arbitrary until you know what went wrong without them.

## Layers

```
bootstrap → adapters → config → application → domain
```

Enforced in CI by `import-linter`. Two contracts beyond the layering itself:

- **`domain` is pure.** It may not import `aiogram`, `sqlalchemy`, `httpx`,
  `apscheduler`, `xlsxwriter`, `yaml`, `asyncio`, `pathlib`, `os` or `logging`. Every
  function is deterministic given its arguments, which is why the solver can be
  property-tested against a brute-force oracle at all.
- **`application` does not know the config format.** Policy arrives as plain dataclasses,
  mapped from pydantic in `bootstrap/mapping.py`. Every use case therefore runs in a test
  with no config file on disk.

The Telegram handlers take a `BotContext` protocol rather than the container, because an
adapter importing the composition root inverts the layering. `Services` satisfies it
structurally.

## Decisions worth knowing

**Surplus, not raw day counts.** See the README. The short version: raw counts punish
people for having been on holiday.

**Lexicographic vector costs, not one packed integer.** Packing tiers into a single
scalar means deriving separation constants and re-deriving them every time a tier is
added. Min-cost-flow theory holds over any ordered abelian group, and lexicographically
ordered ℤ⁴ is one, so tier priority is literally the component order.

**Jitter goes on the employee→day arcs, never the convex source arcs.** Perturbing those
breaks the monotone marginals; the telescoping identity stops holding and the optimality
guarantee evaporates silently. There is an assertion for this that runs in production.

**A constant shift on the source arcs.** Base surplus is signed, so marginals can be
negative, and Dijkstra needs non-negative arcs. Every unit of flow crosses exactly one
source arc and all max flows have the same value, so a constant shift cannot change the
argmin. Do not "simplify" it away.

**The ledger is a cache.** `rebuild()` must reproduce it exactly by replaying day records
forward from a checkpoint. Counters that can only be mutated, never recomputed, are how
the previous system's fairness drifted unnoticed.

**`schedule_day` stores who was available, not just how many.** Entitlement accrues per
available head and absences can be edited later, so the count alone cannot rebuild the
ledger — and exact rebuildability is what makes the retention prune safe.

**Retention folds before it deletes.** The checkpoint is one row per employee, bounded by
headcount rather than time, and never pruned.

**The schema is migrated on boot, not created.** `upgrade_schema` runs Alembic to head
every start: on a fresh database that creates everything, on an existing one it applies
whatever is outstanding, and it is a no-op when there is nothing to do. `create_all`
survives for tests and in-memory databases only. CI runs `alembic check`, which fails if
a model was edited without a migration — otherwise that only shows up as a crash against
a real database.

**Synchronous database access.** A local SQLite file serving a few dozen people: queries
are sub-millisecond, and an async driver would buy latency nobody can perceive in exchange
for a dependency and a class of bugs. Slow work goes to `asyncio.to_thread` at the call
site.

**Enum columns, not `String`.** A plain `String` round-trips to `str`, which silently
breaks every `is` comparison against an enum member — and the code selecting assignments
by source and status is built entirely out of those.

**`stable_hash` is 63 bits.** These values are persisted and SQLite's `INTEGER` is
*signed*, so an unsigned 64-bit hash overflows on write about half the time.

**Per-office jobs, not one job that fans out.** A failure in one office is isolated, the
occurrence key is unambiguous, and the catch-up sweep can replay exactly what was missed.

**The model is never told a fact it could get wrong.** It emits `{date}` and `{office}`
tokens that are substituted after escaping, and output naming an employee, containing
stray braces, shouting or hitting a banned term is discarded in favour of a hand-written
line. Rejecting is cheap, so the guards are strict.

## Things deliberately not built

- **Team cohesion as an objective.** It rewards co-occurrence, which is a convex *reward*
  on group counts — a clustering problem, NP-hard in general, and anything bolted onto
  the flow would be quietly wrong. Give a team an anchor day in the fixed schedule
  instead; that is what people actually mean.
- **Exact weekday balance.** Weeks and weekdays are crossing partitions of the day set,
  so a single-commodity flow cannot carry both exactly; chaining them is broken because
  flow conservation does not preserve provenance. The week layer won because week-to-week
  clumping is what people notice. Weekday counts are tracked in the ledger for reporting.
- **Attendance confirmation.** The ledger counts the announced plan. Buttons would make
  it count reality, at the cost of only working if people press them.
