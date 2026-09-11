# Табельщик

A Telegram bot that decides who comes into the office, tells them the day before, and
does it fairly. Russian-language, multi-office, self-hosted, free to run.

Replaces an earlier bot that ran on GitHub Actions cron and kept its state in git. That
one's reminders were sometimes late, sometimes silently skipped, and its fairness
counters reset on every regeneration. All three are design problems, so this is a rebuild
rather than a repair.

## What it does

- **Fair schedules.** Each office has a weekly template: people fixed to certain
  weekdays, plus `vacantDesks` extra seats to fill. The extras are drafted to make office
  attendance as equal as possible — provably, not heuristically (see below).
- **Attendance reminders** at a configured time, announcing the next working day and
  tagging everyone on it. Mon→Tue, Fri→Mon and Sun→Mon all fall out of one rule.
- **Tempo reminders** weekly and at month end.
- **A colour-coded XLSX** published whenever a schedule changes, with a statistics sheet
  that puts the fairness spread front and centre.
- **Admin mode** inside Telegram: hire and fire, rename, link Telegram handles, weekly
  template, regeneration, previews, job status, backups. The database is authoritative
  after first boot, so this is the only way to change a roster — the YAML seeds are a
  starting point, not a live source.
- **Employee self-service**: everyone manages their own vacations.
- **Chat.** Mention the bot and it answers, grounded in the real schedule and today's
  actual date, following a reply chain back up to ten levels, rate-limited per person per
  day.
- **Moods.** Mostly toxic, occasionally fun, happy, sad or depressive — one per office
  per day.

## Fairness, specifically

The thing being equalised is **surplus** — days attended minus entitlement accrued — not
raw day counts. Raw counts look correct and behave badly: someone back from three weeks
away carries a large deficit and gets drafted every eligible day for a month to catch up.
Surplus means a returner resumes a normal share and a new joiner is fair on day one.

Drafting is a min-cost max-flow with separable convex costs over lexicographically
ordered vector costs: fairness first, then shortfall spreading, then per-week balance,
then a seeded tie-break for variety. Minimising the sum of squared surpluses over a base
polytope yields the *decreasingly minimal* point, which simultaneously minimises the
maximum surplus, maximises the minimum, and minimises the max−min spread and every other
Schur-convex fairness measure. So "why squares?" needs no defending — every strictly
convex objective picks the same integer optimum.

It is verified against a brute-force oracle under `hypothesis`, and against the
counterexample that shows why a per-day greedy pass cannot work:

```
Day 1: draftable {A, B}, one desk
Day 2: draftable {A},    one desk   (B is away)

greedy  → A takes both                  → (2, 0)
optimal → B on day 1, A on day 2        → (1, 1)
```

Flow wins because it can walk back and reassign day 1; greedy has no repair step.

Fairness persists across regenerations through a ledger that is a **rebuildable cache**,
not a mutable counter. A 156-week simulation shows the spread plateauing around 2.5 days
rather than drifting.

## Reliability

The previous bot's defining problem. Four layers:

1. `restart: unless-stopped` — survives crashes and host reboots.
2. **A durable job ledger.** Every occurrence is claimed before it runs and recorded
   after, so a retry, a double trigger or a restart mid-run cannot double-send.
3. **A catch-up sweep on startup.** Anything that should have fired within the grace
   window is replayed once, marked late. A reminder delayed by a reboot still goes out.
4. **Outward alerting.** Failed jobs post to the admin chat; an optional
   [healthchecks.io](https://healthchecks.io) ping covers the case the process never
   comes back at all.

Plus nightly SQLite backups sent to the admin chat — free off-box copies.

## Architecture

Ports and adapters, enforced in CI by `import-linter`:

```
domain/        pure: entities, calendar, min-cost flow solver, ledger, simulation
application/   use cases and ports; no framework, no config format, no I/O
adapters/      SQLAlchemy, aiogram, OpenAI, XlsxWriter, APScheduler
bootstrap/     the composition root
config/        pydantic models over YAML
```

`domain` imports nothing from the project and is forbidden from importing `asyncio`,
`pathlib`, `os`, `yaml` or any framework. `application` may not import the config
package, so every use case runs in a test with no config file on disk.

Operational settings live in YAML; rosters, offices and weekly templates live in the
database, seeded once from `config/offices/*.yaml` and edited thereafter from the bot.
One source of truth per kind of data.

## Quick start

```bash
uv sync
uv run tabelshchik validate
uv run tabelshchik simulate --weeks 52
uv run tabelshchik preview --office ovest --out out
```

None of those need a Telegram token. To run for real, fill in `.env` and:

```bash
docker compose up -d
```

Pushes to `main` deploy themselves once set up: CI gates the deploy, and a deploy that
comes up unhealthy alerts on Telegram and is left down. See
[docs/first-deploy.md](docs/first-deploy.md#automatic-deploys).

Deploying for the first time: [docs/first-deploy.md](docs/first-deploy.md) — a runbook
with the Telegram prerequisites that fail silently if skipped, and the three checks that
prove reliability rather than merely that it starts. General deployment notes and the
Oracle Always Free caveats are in [docs/deploy.md](docs/deploy.md).

## Documentation

Start at **[CLAUDE.md](CLAUDE.md)** — the orientation for anyone, human or agent, about to
change something: commands, the layering contracts, the conventions that are load-bearing,
and the specific ways this repo has bitten people before.

| Document | Read it before |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Touching the solver, the ledger or retention |
| [docs/ai-voice.md](docs/ai-voice.md) | Touching a prompt, a guard or `config/messages.yaml` |
| [docs/configuration.md](docs/configuration.md) | Adding a setting or editing an office file |
| [docs/testing.md](docs/testing.md) | Adding tests — or when one you did not expect breaks |
| [docs/deploy.md](docs/deploy.md) | Moving it to a different machine |

## Migrating from office-rotation-bot

```bash
uv run python scripts/migrate_from_old_bot.py \
    --src ~/Developer/office-rotation-bot/configs/locations \
    --out config/offices
```

Carries over rosters, team ids, tenure end dates, fixed schedules, future vacations and
calendar overrides. Schedules are not carried over — they are regenerated — and neither
is notification state, which is meaningless under the new job ledger.

One deliberate change: `vacantDesks` used to mean the day's total capacity, and now means
extra draftees **on top of** the fixed list. The script warns wherever the two readings
would differ.

## Commands

| Command | What it does |
|---|---|
| `tabelshchik run` | Run the bot |
| `tabelshchik validate` | Check configuration and exit |
| `tabelshchik regenerate --office X` | Rebuild a schedule and write the workbook |
| `tabelshchik preview --office X` | Render the schedule and reminder, send nothing |
| `tabelshchik simulate --weeks 52` | Long-run fairness simulation |
| `tabelshchik dry-run` | Boot everything, list the jobs, send nothing |

## Telegram commands

Published automatically on startup, scoped by audience — nothing to configure in
@BotFather.

| Command | Who sees it |
|---|---|
| `/start` | Link your account. Everyone, in a private chat |
| `/menu` | Menu |
| `/me` | Your upcoming office days |
| `/vacation` | Manage your own time off |
| `/help` | Also the only command published in groups |
| `/admin` | Admins only |

### Who the bot answers

Three audiences, resolved in one place (`application/audience.py`):

- **Anyone posting in a configured office chat.** That chat receives the full tagged
  roster every working day, so there is nothing to withhold there.
- **A linked employee, in a private chat**, while they are still in tenure.
- **An admin**, in a private chat.

Everyone else gets a short "I don't know you" and no data — no names, no schedule, no
model call. A group the bot was added to that is not an office chat gets silence.

`/start` links by matching your Telegram @username against the roster, which is a claim
rather than a proof, so: a match onto a record somebody else already holds is refused, a
departed employee reads as "unknown" (otherwise the bot is an oracle for testing handles),
and every link — and every refusal — is reported to the admin chat.

Admin mode, `/menu` and the office-week view are private-chat only. They name other
people, and the screens redraw in whatever chat the button was pressed in.

### Telegram privacy mode

Optional, and off by default in Telegram. With privacy **on** (the default) the bot
receives only messages that mention it or reply to it, so a reply chain reconstructs as
far as the messages it was party to and then stops — a partial thread, never a wrong one.
Setting privacy to **Disabled** in @BotFather lets it see every group message, which makes
the full ten levels work; it also means it receives everything people say in that chat.
Raw messages are cached for `retention.chatMessagesDays` (7 by default) and used for
nothing else. Your call; no code changes either way.

## Development

```bash
uv run pytest              # everything
uv run pytest -m "not slow"  # skip the multi-year simulations
uv run ruff check . && uv run mypy && uv run lint-imports
```
