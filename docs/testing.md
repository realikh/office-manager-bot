# Testing

About 800 tests, under a minute. They are the reason the fairness claims are claims and
not hopes, so it is worth knowing what each group is actually for.

```bash
uv run pytest                     # everything
uv run pytest -m "not slow"       # skip the multi-year simulations
uv run pytest tests/domain -q     # one area
uv run pytest -k fairness -q      # one idea
```

`asyncio_mode = "auto"` — async tests need no decorator. `filterwarnings = ["error"]` — a
`DeprecationWarning` from a dependency fails the suite, on purpose.

## The layout

| Directory | What it proves | Needs |
|---|---|---|
| `tests/domain/` | The solver is optimal, the ledger rebuilds exactly, the calendar is right | Nothing. Pure functions. |
| `tests/application/` | Rendering, guards, memory rules, report assembly | Nothing. Fakes only. |
| `tests/config/` | YAML validates, typos are refused, migration from the old bot preserves data | The real `config/` |
| `tests/integration/` | Use cases against a real SQLite database and fake I/O | In-memory SQLite |
| `tests/test_cli.py` | Argument parsing, and that the Dockerfile healthcheck matches the parser | — |

## Load-bearing tests

Some exist to catch a specific production failure. If one starts failing, read what it
says before changing it.

**`tests/domain/test_solver.py`** — property tests with `hypothesis` against an
independent brute-force oracle (`test_matches_brute_force_optimum`). The solver's
optimality guarantee is not "it looked right"; it is this file. A companion assertion
lives in `domain/solver.py` itself and runs in production: jitter must never land on the
convex source arcs, because perturbing those breaks the monotone marginals and the
guarantee evaporates *silently*.

**`tests/domain/test_simulation.py`** (`@pytest.mark.slow`) — multi-year runs proving the
fairness spread **plateaus** rather than drifting. Slow because that is the only way to
know.

**`tests/domain/test_ledger.py`** — the ledger rebuilds byte-identically from day records,
including across a retention prune. Counters that can only be mutated, never recomputed,
are how the previous system's fairness drifted unnoticed.

**`tests/integration/test_scheduling.py`** — includes
`test_every_scheduled_job_pins_its_timezone`, which walks the **real** registered jobs.
Without an explicit timezone a `CronTrigger` inherits the host's, the catch-up sweep then
looks in a different zone, and a missed reminder is silently never replayed. It passed
locally for weeks because the laptop happened to be in the right zone; CI in UTC caught
it.

**`tests/integration/test_attendance_reminder.py`** — `test_everyone_is_named_even_when_the_model_says_nothing`
is the guarantee that decoration is optional and being reminded is not.
`test_the_date_is_never_said_twice` is the regression for "В понедельник В понедельник".

**`tests/integration/test_wiring.py`** — that the composition root actually composes.
Cheap, and it catches a store added to the container but not to `BotContext`.
`test_the_jobs_use_the_times_stored_in_the_database` and
`test_moving_a_time_reschedules_the_running_bot` are what stop a time set from `/admin`
being stored and then ignored.

**`tests/integration/test_tempo_and_chat.py`** —
`test_the_old_pin_comes_down_first_even_after_a_restart` is the regression for a pin
tracked in memory, which a redeploy forgot; `test_a_reminder_goes_out_once_a_day_whatever_the_scheduler_does`
covers a time moved after the job fired;
`test_an_answer_cut_off_by_the_token_ceiling_is_recovered_not_sent_as_json` covers a chat
reply that ran out of tokens mid-JSON.

**`tests/integration/test_holiday_greeting.py`** — one greeting per holiday, per chat, per
day, and none on a transferred day off. The last few tests read the real `holidays`
package, so a calendar upgrade that changes past data shows up here first.

## Tests that pin exact wording

`test_attendance_reminder.py`, `test_tempo_and_chat.py` and `test_holiday_greeting.py`
load the **real**
`config/messages.yaml` and `config/app.yaml`, so a schema violation fails at collection
time, and several assertions check rendered strings.

**This is deliberate.** Changing copy should make you look at what the message now says.
When one breaks, read the new rendering before updating the expectation — twice now that
has surfaced a real defect rather than a stale string.

`tests/config/test_loader.py` similarly pins values from `app.yaml` itself, such as the
toxic mood weight.

## Fakes

`src/tabelshchik/adapters/fakes.py`, shipped in `src` rather than `tests` so the
integration tests and the CLI can share them.

- **`RecordingNotifier`** — captures messages and documents; `last_text` is the usual
  assertion target. `fail=True` makes every send fail, for the "a failed send must not
  mark the day announced" path. `copies` records what was copied where; `uncopyable`
  names ids Telegram would silently skip, for the partial-delivery path.
- **`StubChatModel`** — returns `reply` verbatim, including when JSON was asked for, so a
  test can hand back prose where the caller wanted an object and check the fallback holds.
  Records `prompts` and `json_requested`, and can report `prompt_tokens` /
  `completion_tokens` for the usage accounting.
- **`FixedClock`** (`adapters/clock.py`) — takes a `datetime`, not a `date`. Every use
  case takes a `Clock`; nothing calls `datetime.now()`.

## Conventions

**Name the behaviour, not the function.** `test_a_removal_is_undoable`, not
`test_restore_2`. A failure list should read as a list of broken promises.

**The docstring says what breaks in production.** Not what the test does — the code says
that.

```python
def test_the_person_and_their_history_survive_the_removal(office) -> None:
    """Soft on purpose. A hard delete cascades away every assignment they ever had and
    silently rewrites everyone else's fairness numbers."""
```

**Assert the outcome, not the mechanism.** `assert scheduled_days(office, "borya")` says
more than checking that `regenerate` was called.

**Fixtures come from `conftest.py`.** `tests/integration/conftest.py` gives you
`sessions` (in-memory SQLite, schema created) and `seed()` with `office_seed()` for a
four-person office, `big_office_seed()` for twelve people against eleven Friday desks —
the dense case, where `shortfall == 0` is worth asserting — or `fixed_office_seed()` for
a fixed-schedule-only office the solver short-circuits on. `ANCHOR` is a Monday; several
tests rely on that.

There is no `real_config` fixture any more: offices live in the database and are no longer
read from the repository, so the tests that needed a realistic roster build one of the
same *shape* instead.

**Strict mypy runs on tests too**, with the ceremony relaxed: untyped defs are allowed,
`union-attr` is off (tests assert on values they just wrote). The checks that catch real
mistakes stay on.

## Adding tests for new work

A use case that changes data wants at least:

1. The happy path, asserting the **consequence** — not that a function ran, but that the
   schedule, ledger or roster now says what it should.
2. What it refuses, and that nothing was written when it refused.
3. The undo, if there is one.
4. The interaction with whatever else touches that data — a removal frees days, so
   something must check the days get refilled.

Anything driven by an AI response wants the model answering well, answering badly, and
switched off entirely. The third is the one that matters: it is what happens on a day the
API is down.
