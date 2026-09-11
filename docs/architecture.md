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

**The database is a named Docker volume.** The image runs as uid 10001 and a bind mount
would carry the host directory's ownership, which fails as "unable to open database
file" on first boot and again on any new host. Docker owns a named volume, so the uid
always lines up. The cost is that `down -v` destroys it; the nightly backup to the admin
chat is the mitigation.

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

**The model is never told a fact, and never emits one.** It writes a flavour clause and
some epithets; every date, weekday, office name and person is placed by `voice.py` after
escaping. This started as a guard and became a structure: when the model held a `{date}`
token whose contents it could not see, it wrote its own lead-in in front of it and
produced "В понедельник В понедельник, 14 сентября". A filter can only detect that.
Taking the token out of its hands removes the failure. Generated text that names a
weekday, a month, a digit, the office or an employee — or shouts, or disagrees in gender —
is discarded in favour of a written line. Rejecting is cheap, so the guards are strict.
Details in [ai-voice.md](ai-voice.md).

**The reminder's roster loop never depends on the model.** Decoration is optional; being
reminded is not. Epithets fall back per slot, so a model that titles one person well and
fumbles the next costs only the second title, and with the AI switched off entirely
everyone scheduled still gets a tagged line.

**Removal ends a tenure; it does not delete a person.** The store's `remove_employee`
defaults to a hard delete that cascades away every assignment somebody ever had and
silently rewrites everyone else's surplus. An end date stops them being scheduled, keeps
the history the ledger is rebuilt from, and undoes in one button.

**The office YAML is a seed, not a live source.** Seeding skips an office that already
exists, at whole-office granularity, so after first boot the database is authoritative and
the admin UI is the only way to change a roster. The alternative — re-applying the files
on boot — would delete employees and cascade away their history every time someone edited
a comment.

**Chat memory is a ring buffer, not a time series.** Writing a new fact is what evicts the
oldest. A bounded number of rows means a bounded prompt, which means a bill that cannot
creep; retention by age would let a busy week silently double the cost of every message.
The raw message cache is the opposite — it exists only to rebuild a reply chain, which
nobody follows back more than a few days, so that one is pruned by age.

**One model call, not two.** The chat's reply and its decision about what to remember come
back in a single JSON response; so do the reminder's clause and its epithets. Two calls
would cost twice as much and could disagree with each other.

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
