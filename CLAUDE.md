# Working on Табельщик

Read this before changing anything. It is the orientation an agent needs that the code
itself cannot give: what the layers are for, which conventions are load-bearing, and the
specific ways this repo has bitten people before.

**Табельщик** is a Telegram bot that decides who comes into which office on which day,
fairly and provably, and then tells them. All user-facing text is Russian. It must stay
free to run: one small VM, SQLite, long polling, no managed services.

---

## Commands

```bash
uv run pytest                    # everything (~50s, 800 tests)
uv run pytest -m "not slow"      # skip the multi-year fairness simulations
uv run pytest tests/domain -q    # one area
uv run mypy                      # strict, on src *and* tests
uv run ruff check . && uv run ruff format .
uv run lint-imports              # the three layering contracts
```

Run **all four** before calling anything done. CI runs the same set and has caught two
"works on my machine" bugs that local runs could not — both real production faults, not
test artifacts.

```bash
uv run tabelshchik validate      # parse app.yaml + messages.yaml, touch nothing
uv run tabelshchik import-office FILE...   # load office YAML into the database
uv run tabelshchik healthcheck   # read the heartbeat file's age
uv run tabelshchik run           # the bot
```

Migrations:

```bash
TABELSHCHIK_DB=/tmp/x.db uv run alembic upgrade head
TABELSHCHIK_DB=/tmp/x.db uv run alembic revision --autogenerate -m "What changed"
TABELSHCHIK_DB=/tmp/x.db uv run alembic check   # fails if a model has no migration
```

---

## Layers

```
bootstrap → adapters → config → application → domain
```

Enforced by `import-linter` (`pyproject.toml`), so a violation fails CI rather than being
noticed later. Three contracts:

1. **The layering itself.** Imports point one way.
2. **`domain` is pure.** No `aiogram`, `sqlalchemy`, `httpx`, `apscheduler`,
   `xlsxwriter`, `yaml`, `asyncio`, `pathlib`, `os`, `logging`. Every function is
   deterministic given its arguments — which is the only reason the solver can be
   property-tested against a brute-force oracle.
3. **`application` does not know the config format.** No `pydantic`, no `yaml`, no
   `tabelshchik.config`. Policy arrives as plain dataclasses from
   `bootstrap/mapping.py`, so every use case runs in a test with no file on disk.

Telegram handlers depend on the `BotContext` **Protocol** in
`application/context.py`, never on `bootstrap.Services` — an adapter importing the
composition root inverts the layering. `Services` satisfies it structurally; there is
nothing to register.

### Where things live

| Path | What belongs there |
|---|---|
| `domain/` | Entities, the min-cost-flow solver, the fairness ledger, the calendar, `stable_hash`. Pure. |
| `application/` | Use cases (`send_attendance_reminder`, `send_tempo_reminder`, `send_holiday_greeting`, `regenerate_schedule`, `manage_absences`, `manage_roster`, `manage_offices`, `manage_admins`, `manage_limits`, `manage_times`, `chat`, `memory`, `prune_history`), the `ports.py` Protocols, `ids.py`, `voice.py`. |
| `adapters/` | SQLAlchemy repositories, the Telegram routers, the OpenAI client, APScheduler, XLSX, the `holidays` calendar, fakes. |
| `config/` | Pydantic models for `app.yaml` and `messages.yaml`; `OfficeSeed` for the import command. |
| `bootstrap/` | The composition root, job registration, lifespan, `mapping.py`. |

### Adding a capability

1. Extend the **Protocol** in `application/ports.py`.
2. Implement it in the adapter (`adapters/db/repositories.py` for storage).
3. Wire it in `bootstrap/container.py` (field, `__post_init__`, imports).
4. If a handler needs it, add it to `BotContext` in `application/context.py`.
5. New table? Add the model, autogenerate a migration, run `alembic check`.

Skipping step 4 is the usual mistake: everything runs until a handler touches a field the
Protocol does not declare, and mypy will not catch it because `Services` is passed as
`BotContext`.

---

## Conventions that are not negotiable

**All Russian copy lives in `config/messages.yaml`.** Nothing user-facing should be a
string literal in Python — the file exists so the bot's voice can be rewritten without a
deploy and so all five moods can be checked for coverage at startup. There are known
exceptions in the employee router and a few admin strings; do not add more.

**Every config model forbids unknown keys** (`extra="forbid"`) and uses camelCase aliases.
A misspelled setting is a startup error, not a silent default. The previous system failed
open on typos, which is how a setting can look configured and do nothing for months.

**Dates are `datetime.date`, never instants.** A working day is a calendar concept.
Timestamps are only for "when did this actually happen".

**Comments explain *why*, never *what*.** Match the density of the file you are editing.
A comment that restates the line above it is noise; one that records why an obvious
simplification is wrong is the most valuable thing in the file.

**Test names are sentences.** `test_a_removal_is_undoable`, not `test_restore_2`. The
docstring, where there is one, says what breaks in production if the test fails.

---

## Who the bot will talk to

**One gate, `application/audience.py`.** `resolve()` returns a `Grant` naming the audience
and the office it may speak about, or `None` for nothing at all:

| Situation | Audience | Office |
|---|---|---|
| chat id matches a configured office | `OFFICE_CHAT` | that office |
| private, user id in the `admin` table | `ADMIN` | their own, else first active |
| private, linked **and in tenure** | `EMPLOYEE` | that employee's office |
| anything else | `STRANGER` | `None` |

Being in an office's group chat is itself the credential — the reminder posts that
office's full tagged roster there every working day.

It exists because the rule it replaced answered **everybody**: an unknown sender fell
through to `active_offices()[0]`, so any account that found the bot could ask who was in
tomorrow and be told by name, and could get a fact written into that office's shared
memory that every employee's prompt then carried.

Rules that follow from it, each with a test:

- **The gate runs before anything reads, caches or sends.** Not after `_remember`, not
  after the trigger check.
- **Never add a fallback that cannot fail.** `office_id is None` must stay reachable.
- **Tenure is checked at the gate**, not by clearing `telegram_user_id` on termination —
  the link is what lets a reminder tag someone, and `restore` should just work.
- **A closed office answers nobody.** That used to be an accident of three separate scans
  iterating `active_offices()`; now that an office can be closed from a button, the check
  is written out in `audience.resolve` and in employee self-service.
- `/start` decides via `audience.claim()`, which refuses a username match onto a record
  somebody else already holds, and reports "unknown" for a departed employee so the bot is
  not an oracle for testing handles against the roster.
- **Screens that name other people are private-chat only** — the whole admin router, plus
  `me:office` and `/menu`. `_replace` edits in whatever chat the button lives in. The one
  exception is `routers/bind.py`, which runs in groups and lists office *names* only; its
  callback re-checks `is_admin` on the press, because `AdminOnly` (now in
  `adapters/telegram/middlewares.py`) answers with silence, which in a group reads as a
  broken bot rather than a refusal.

## Traps

These have all cost real time. They are ordered by how likely you are to hit them.

### Your edit silently matched nothing

`ruff format` runs after every change and reflows code. A `str.replace` pattern copied
from an earlier read will then match **zero** occurrences and `replace()` returns the
string unchanged — no error, no diff, and you find out much later.

**Always assert before writing:**

```python
assert old in s, "pattern not found"  # or sys.exit with a label
s = s.replace(old, new, 1)
```

When applying several edits in one script, label each one and print on success, so a
failure names which pattern went stale. Re-read a file after any `ruff format`.

### A free-text FSM step must refuse commands

Typing `/cancel` during a rename renamed the employee to `/cancel`. A state handler
matches *any* message in its state, and aiogram dispatches in registration order, so it
won over the cancel handler registered below it.

Both halves are fixed and both are tested: cancel is registered first, and every free-text
step calls `_refused_a_command(message)` before touching the text. Order alone is one
reordering away from breaking. Prompts carry a `✖️ Отмена` button, because that is what
people reach for.

The tests read the state-group names **out of the source**, so a flow added later cannot
exempt itself by not being on a hard-coded list. Add a `StatesGroup` and it is covered.

It was harmless in the other flows only because a username regex and an integer parser
rejected the command — luck, not design.

### One screen, and it is the last message

Two operations, both in `adapters/telegram/ui.py`, and every keyboard goes through one:

- **`redraw`** — a callback edits the screen its button lives on, in place.
- **`rehome`** — anything that has added to the chat since the screen was drawn (the
  answer the user just typed, a workbook, a preview) moves the screen down beneath it.

**A callback must never send a message carrying a keyboard.** Two keyboards means two live
screens, and the older one keeps working: pressing a button on it acts on state that has
moved on. That is what put two identical office lists on screen three minutes apart. A
callback may send *content* — a document, a rendered fortnight — and then rehome the screen
below it. `test_no_callback_handler_hands_out_a_second_keyboard` enforces this over both
routers.

A flow's prompt **replaces** the screen it was launched from rather than stacking under it,
and records where to return, so ✖️ Отмена goes back to that person's card rather than to
the top. `_ask` does all three — set the state, replace the screen, remember the origin —
and attaches `cancel_keyboard()` itself, so a prompt with no way out is not something nine
call sites can forget. `_step` re-prompts, `_finish` ends the flow; both rehome.

The asymmetry is Telegram's: a bot may delete its own messages but not somebody else's in
a private chat. The typed answer always stays, which is why the screen follows it down
instead of updating above it — with a keyboard up on a phone, an edit above the fold looks
like nothing happened. The remembered message id lives in FSM state, so it dies on restart;
harmless, since a stale id only means a delete that quietly fails.

### A handler must never redraw by calling another handler

Callback handlers parse `query.data`. A handler that redraws by calling a sibling makes
the sibling parse callback data meant for the *first* one, and it fails — silently,
because the write it follows has already landed. The visible symptom is a UI that does
not respond: the checkbox is right the next time you open the screen and never on the tap.

Build screens from **explicit arguments** instead. In `routers/admin.py` those are
`offices_screen`, `office_screen`, `office_settings_screen`, `chat_screen`,
`calendar_screen`, `desks_screen`, `fixed_screen`, `fixed_day_screen`, `roster_screen`,
`admins_screen`, `admin_card_screen`, `grant_screen` —
each returns `(text, markup)` from its own parameters, and handlers do
`await _replace(query, *some_screen(services, office_id))`.
`tests/integration/test_admin_screens.py::test_no_handler_redraws_by_calling_another_handler`
enforces it; it caught a third instance that had been working only because two callbacks
happened to carry the office id in the same position.

Related: **`editMessageText` refuses an unchanged message.** A screen whose only
difference is a ✅ needs something in the *text* to move too — hence the "отмечено N"
counter, and the counts in the admins, offices and absences headings. And do not wrap the edit in a bare `except Exception`: that turns a crash in the
screen being drawn into a second, stale message, which is indistinguishable from the UI
not responding.

### Scheduling: pass the timezone explicitly

`daily_at()` in `adapters/scheduling/runner.py` takes an explicit `timezone` and every job
in `bootstrap/jobs.py` passes `app.timezone`. Without it, `CronTrigger` inherits the
**host's** zone. Scheduled firing survives that (the scheduler applies its own zone when a
job is added) but the **catch-up sweep evaluates the trigger directly**, so the two zones
diverge and the sweep silently replays nothing. It looks fine locally if your machine
happens to be in `Asia/Almaty`. There is a regression test that walks every registered job
and asserts its trigger carries a zone.

### Enum columns, never `String`

Use `_enum(SomeStrEnum)` from `adapters/db/models.py`. A plain `String` round-trips to
`str`, which breaks every `is` comparison against an enum member — and the code selecting
assignments by source and status is built entirely out of those. When this last happened
it froze the wrong people, ignored manual pins, and kept counting cancelled assignments.

### `stable_hash` is 63 bits, deliberately

These values are persisted and SQLite's `INTEGER` is *signed*. An unsigned 64-bit hash
overflows on write about half the time.

### `remove_employee(ended_on=None)` is a hard delete

The default cascades away every assignment a person ever had and silently rewrites
everyone else's fairness numbers. `application/manage_roster.end_tenure` always passes an
explicit date. Never call the store method directly from a handler.

### Delivery times are database rows too, and moving one must reschedule

The times of day (attendance, Tempo, holiday greeting, extender, end of workday) live in
the `setting` table, read uncached through `Services.reminder_times`, with defaults in
`ReminderTimes`. `office_jobs` reads them every time it builds a job list, and saving one
calls `OfficeJobs.reschedule()` — skip that and the change is stored and ignored until the
next deploy. A time moved after its job fired gives the day a second occurrence key, so
anything that posts must be idempotent per day on its own: the attendance fingerprint,
or a `bot_post` row for Tempo and holidays. `app.yaml` refuses the old `time` keys.

### Pins are tracked in `bot_post`, never in memory

The Tempo reminder unpins every earlier Tempo post recorded as pinned for that office and
chat, *then* pins the new one. The dict on `TelegramNotifier` used to hold this and was
empty after every redeploy, so old pins stayed up. `delete_posts_before` keeps any row
still pinned — it is what next week's reminder needs to find.

### Offices and admins are database rows, not files

Nothing reads `config/offices/*.yaml` at boot; the directory is git-ignored and only
`tabelshchik import-office` applies one, explicitly. Offices are created from `/admin`,
and `seed_offices` survives for that command and for tests. `replace_existing=True` stays
off the command line: it hard-deletes an office and cascades away every assignment and
ledger entry it ever had.

Adminship is the `admin` table, with **exactly one `OWNER`** enforced by a partial unique
index. Three rules follow, each with a test:

- **Demote before you promote.** SQLite checks that index per statement, not at commit, so
  `transfer_ownership` does both in one transaction in that order. The obvious order
  raises an IntegrityError out of a session scope.
- **The owner cannot be revoked, only transferred** — an empty table has nobody left who
  can grant adminship back. An admin may always remove *themselves*, which is what lets an
  ex-owner finish leaving.
- **`ADMIN_IDS` seeds the table only when it is empty.** A variable that re-granted
  adminship every boot would make leaving impossible. Emptying the table by hand is the
  way back in.

`is_admin` hits the database on every update and is deliberately uncached: a promotion has
to work on that person's next message, not at the next deploy.

### Closing an office has to stop the things that speak

Per-office jobs are registered at boot and outlive the office, so `send_attendance_reminder`,
`send_tempo_reminder`, `send_holiday_greeting` and the horizon extender each check
`office.active` themselves, **before `planning_context`** — which raises `LookupError` once the office is gone, and the
runner turns that into a Telegram failure alert every Thursday forever. `JobRunner.add_live`
and `drop` keep a running scheduler in step; `add` alone only appends to the list `start`
read once.

### Telegram limits

- **Callback data: 64 bytes total.** `adm:empv:<employeeId>` fits; `adm:x:<officeId>:<employeeId>`
  can not. Derive the office from the employee instead of carrying both —
  `OfficeStore.office_of` is one indexed lookup. `adm:fixtog:` was the exception until an
  office id could be typed by an admin, at which point it stopped fitting.
  `test_every_callback_a_screen_emits_fits_telegram_s_limit` builds every screen with
  maximum-length ids and checks. **Every callback prefix ends in a colon**: dispatch is
  `startswith` in registration order, so without it `adm:emp` swallows `adm:empadd:…` —
  `test_no_callback_prefix_shadows_another` enforces that.
- **Message: 4096 characters.** An over-long message is simply not delivered. Use
  `voice.render_days`, which chunks.
- **Document caption: 1024 characters.** Build one with
  `reports/xlsx.schedule_caption`, which composes to fit. Never slice a caption: it cuts
  mid-name, and now that captions carry markup it can cut mid-tag and break the message.
- **`reply_to_message` is populated exactly one level deep.** Deeper threads are
  reconstructed from the `chat_message` cache — see [docs/ai-voice.md](docs/ai-voice.md).
- **Privacy mode** (on by default) means the bot only receives messages mentioning it or
  replying to it. Everything degrades gracefully; nothing should assume a full view.

### FSM state is in-memory and is dropped on restart

`create_dispatcher` builds `Dispatcher()` with no `storage=`, and polling starts with
`drop_pending_updates=True`. A half-finished wizard dies on redeploy — acceptable, but do
not build a flow that matters across a restart. Each router needs its **own**
state-filtered `/cancel`; an unfiltered one in another router will answer with the wrong
menu.

### Stem matching and short words

Guards that reject a word "and its inflections" take a prefix. Blindly dropping the last
character breaks on short words: `мая` → `ма`, which matches *кофемашина*, *малиновый*
and most of the language. `voice._prefixes` keeps words shorter than four characters
whole and matches at word boundaries. Names are the exception — `name_stems` is
deliberately aggressive because a false positive there costs one hand-written line.

### Tests pin exact wording

Several integration tests load the **real** `config/messages.yaml` and assert on rendered
strings. Changing copy will break them, and that is the point — it forces you to look at
what the message now says. See [docs/testing.md](docs/testing.md).

---

## Subsystem docs

- [docs/architecture.md](docs/architecture.md) — the load-bearing decisions and why the
  obvious simplifications are wrong. Read before touching the solver or the ledger.
- [docs/ai-voice.md](docs/ai-voice.md) — the model contract, the guards, moods, chat
  memory and reply chains, and the cost model. Read before touching prompts or
  `messages.yaml`.
- [docs/configuration.md](docs/configuration.md) — the three config files, how a setting
  gets from YAML to a use case, and the seed-versus-database rule.
- [docs/testing.md](docs/testing.md) — how the suite is organised and which tests are
  load-bearing.
- [docs/deploy.md](docs/deploy.md) / [docs/first-deploy.md](docs/first-deploy.md) — the
  VM, Docker, and the automated deploy.

---

## Deployment, briefly

Push to `main` → GitHub Actions runs the four checks → on green, SSHes to the VM under a
**forced command** (`scripts/deploy.sh`) which git-resets, rebuilds, and polls
`docker inspect` for health. A failed deploy alerts Telegram via `curl` directly — not
through the bot, because the bot is the thing that just failed — and is left down. No
automatic rollback: it can mask a migration that has already run.

The schema is migrated on boot by `upgrade_schema`, so a merged migration applies itself.

**Never commit secrets.** The token, admin chat id and OpenAI key live in `.env` on the
VM and in GitHub Actions secrets. They are not in this repo and must not enter it, or a
transcript.
