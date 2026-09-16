# Configuration

Two files in git, one line about what belongs in them, and a recipe for adding a setting
without leaving half of it unwired.

**The line: deployment configuration lives in git; the organisation lives in the
database.** Offices, employees, weekly templates, calendar exceptions, who is an admin and
*when* each message goes out are all edited from the bot, by the people who run it.
Timezones, silent hours and the bot's voice are deploy-time decisions and stay in the
repository.

## The files

| File | Holds | Authority |
|---|---|---|
| `config/app.yaml` | Operational settings: timezone, which days the attendance reminder runs, silent hours, schedule policy, retention, moods, AI, health | **Always live.** Read at boot; there is no copy in the database. |
| `config/messages.yaml` | Every user-facing string, per mood | **Always live.** See [ai-voice.md](ai-voice.md). |

Secrets are never in either: `TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY`, `ADMIN_IDS`,
`ADMIN_CHAT_ID` come from the environment (`.env` on the server), via
`bootstrap/settings.py`.

## What is not here any more

`config/offices/*.yaml` used to describe offices. It was a seed: `seed_offices` inserted
an office the first time it was seen and then skipped it forever, at whole-office
granularity — so editing a file on a running system did nothing at all, while the file sat
in the repository looking authoritative. It also put real names, Telegram handles and the
office group id into git.

Offices are created and edited from `/admin` now, and nothing reads those files at boot.
They survive as an **import format**:

```bash
uv run tabelshchik import-office path/to/office.yaml
```

Explicit, never automatic, and the way back into an empty database after a restore.
There is deliberately no `--replace`: replacing an office hard-deletes it and cascades
away every employee, assignment and ledger entry it ever had.

`config/offices/` is git-ignored. If you keep local copies, they are a snapshot rather
than a record — the database is the record, and the nightly backup is what protects it.

## Delivery times

`/admin` → ⏰ Время рассылок. These were `app.yaml` keys (`reminders.attendance.time`,
`schedule.autoExtend`), which made moving a reminder by ten minutes a commit and a deploy.
A leftover key is now a startup error, not a setting that is quietly ignored.

| Time | Default | What it does |
|---|---|---|
| 📣 Кто завтра в офисе | 15:30 | The attendance reminder. Its *days* are still `reminders.attendance.runOn`. |
| 📝 Tempo | 17:50 | Runs daily; sends only on the last working day of the week and/or month — one message when both. |
| 🏁 Конец рабочего дня | 18:00 | Schedules nothing. If Tempo goes out 1–30 minutes before it, the message tells people to spend exactly those minutes on Tempo; any earlier, no countdown. |
| 🎉 Праздники | 10:00 | Runs daily; greets the office on a public holiday. |
| 🔄 Продление | чт 10:00 | Rolls the horizon forward and publishes the workbook. Typed as `чт 10:00`, or `10:00` to keep the day. |

They are rows in the `setting` table, stored as minutes after midnight. A missing row
means the default in `application/policy.py:ReminderTimes`, so a default changed in a
release still reaches every deployment that has not moved away from it; `-` in the prompt
deletes the row. Reads are uncached (`Services.reminder_times`).

Saving a time calls `OfficeJobs.reschedule()`, which rebuilds every active office's
triggers on the running scheduler. Moving a time on a day it has already fired does not
send twice: the attendance reminder checks its announced fingerprint, the extender's
regeneration is a no-op when nothing changed, and Tempo and holiday posts are recorded in
`bot_post` and skipped if today's is already there. A time moved to earlier than *now*
takes effect from the next occurrence.

## Holidays

Public holidays come from the [`holidays`](https://pypi.org/project/holidays/) package,
per office (`/admin` → office → ⚙️ → 📅). It tracks the law — it already knows Kazakhstan's
Constitution Day moves to 15 March from 2027 — where the free web API that was checked
still listed the old date and missed Orthodox Christmas and Kurban Ait.

Its data changes only when the package does, so `.github/workflows/holidays.yml` upgrades
that one package every Monday, runs every check CI runs, and if they pass commits
`uv.lock` to `main` and starts the deploy. Nothing needs doing by hand; the ⏰ screen shows
the package version and the next holiday so a stale calendar is visible. If `main` is ever
branch-protected, that workflow has to open a pull request instead.

The greeting job asks the calendar about *today* every time it runs, rather than working
out a list at boot.

## AI limits

The daily AI allowance has always been per person: `ai_usage` is keyed on
`(user_id, day)`, so ten a day means ten each, not ten between everybody. What was fixed
was the *number* — the same for everyone, changeable only by editing `app.yaml` and
deploying.

Three things move it now, all from the bot:

| | Where | Meaning |
|---|---|---|
| The default | `/admin` → 🤖 Лимиты ИИ | What everybody gets unless singled out |
| One person's allowance | their card → 🤖 Лимит ИИ | Overrides the default for them alone |
| The global cap | `/admin` → 🤖 Лимиты ИИ | Cost stop-loss across everybody, per day |

`app.yaml` still carries the defaults, and a row in the `setting` table overrides one. A
missing row means the file wins, so a default changed in a release reaches every
deployment that has not deliberately moved away from it.

An employee's override is `NULL` until somebody sets it, and that is deliberate:
**raising the default lifts everybody who was never singled out.** Storing a copy of the
current default on every employee would have frozen the roster at whatever the number
happened to be, and made changing the default do nothing.

Zero is a real answer — no AI replies for that person. "No limit of their own" is `NULL`,
which the UI spells `-`.

Worth knowing: the global cap bites before the per-person limits do. Raising everybody to
thirty while the cap stays at two hundred means the bot goes quiet for the whole office
once the cap is reached, which reads as a fault rather than as a budget — so the two live
on the same screen.

## Adminship

There is no `admins:` key. Admins live in the `admin` table, exactly one of them an
`OWNER`, and they are managed from `/admin` → 👑 Администраторы.

`ADMIN_IDS` seeds that table **only when it is empty** — the same rule the office files
followed, for the same reason. Once anybody is an admin the environment variable does
nothing, which is what lets an owner hand the bot over and then stop being an admin. If
the table ever ends up empty, the next boot re-seeds from it; that is the way back in.

`ADMIN_CHAT_ID` is optional and falls back to the owner's own Telegram id. Keep it set
anyway: `scripts/deploy.sh` reads it out of `.env` directly to report a failed deploy, and
it cannot reach the database.

## Validation

Every model inherits `config/models.py:Base`:

```python
model_config = ConfigDict(
    extra="forbid",  # a typo is a startup error, not a silent default
    alias_generator=to_camel,  # YAML is camelCase, Python is snake_case
    populate_by_name=True,
    frozen=True,
)
```

`extra="forbid"` is the important one. The system this replaced failed open on typos,
which is how a setting can look configured and do nothing for months. `ConfigError`
messages name the file, the key path and what was wrong, so they can be fixed without
reading the source.

Cross-file rules live in `loader._check_across_offices`: office ids, employee ids and
Telegram usernames must each be globally unique. **Chat ids deliberately need not be** —
two offices may share one Telegram group, and messages into a shared chat carry an office
header so they stay distinguishable. `LoadedConfig.shared_chat_ids` reports which.

Check any change without starting the bot:

```bash
uv run tabelshchik validate
```

## Adding a setting

Four files, and missing any one leaves it half-wired.

1. **`config/models.py`** — add the field to the right section with a default and bounds:

   ```python
   reply_depth: int = Field(default=10, ge=0, le=25)
   ```

   Bounds are not decoration. They are what turns a fat-fingered `1000` into a startup
   error rather than a surprising bill.

2. **`config/app.yaml`** — add it with a comment saying what it is *for*, not what it is.

3. **`application/policy.py`** — add it to the plain dataclass the use cases take. The
   `application` layer may not import `pydantic` or `tabelshchik.config`; that contract is
   enforced by `lint-imports` and is what lets every use case run without a file on disk.

4. **`bootstrap/container.py`** (or `mapping.py`) — map one to the other. Policies built
   in `container.py` are properties like `chat_policy`; pure translations live in
   `mapping.py`.

Then add a test in `tests/config/test_loader.py` if the value has a rule worth pinning.

### Adding copy

`config/messages.yaml` and its schema in `config/messages.py`, plus the dotted key in
`bootstrap/mapping.py:catalog()` — that function is the authoritative list of catalog
keys. All five moods are required. Read [ai-voice.md](ai-voice.md) first; the token
validators will refuse to boot otherwise.

## Reference

### `app.yaml`

| Section | Notes |
|---|---|
| `timezone` | Everything civil. Also passed explicitly to every cron trigger — see CLAUDE.md. |
| `reminders.attendance` | `runOn`, `pin`. Listing `sun` is what covers Sun→Monday; Mon→Tue and Fri→Mon fall out of the one rule. The time is set from `/admin`. |
| `silentHours` | Per weekday with a `default`. Silent still *sends* — it just does not buzz. A window may cross midnight. |
| `schedule` | `horizonWeeks` generated, `freezeWeeks` immutable (must be smaller, validated), `maxDaysPerWeek`, `absencePolicy`, `surplusClamp`. When the extender runs is set from `/admin`. |
| `retention` | `scheduleMonths` 3, `jobRunsDays`, `auditDays`, `aiUsageDays`, `chatMessagesDays`. |
| `personality` | `moods` weights, `safeMode`, `bannedTerms`. |
| `ai` | Model, sampling, daily limits, triggers, and `chat` context budgets. `maxTokens` bounds what reminders and greetings ask for; `chat.maxTokens` bounds one chat reply. The two limits are **defaults** — see above. |
| `health` | `pingUrl` dead-man's switch, heartbeat file and staleness, `catchUpGraceHours`, `nightlyBackup`. |

### `offices/<id>.yaml`

```yaml
id: ovest                      # [a-z0-9][a-z0-9-]{1,62}
name: O'Vest
address: улица Акмешит 1
chatId: -1003760453775         # may be shared with another office
timezone: Asia/Almaty
holidayCalendar: KZ
seedNonce: 60804               # bump to reshuffle deterministic draws
active: true

schedule:
  fixed:                       # people who are always in on that weekday
    monday: [temirlan-iskhazov, bagzhan-artykbayev]
  vacantDesks:                 # drafted *in addition to* the fixed list
    friday: 11

employees:
  - id: alikhan-khassen
    name: Alikhan Khassen
    telegramUsername: realikh  # 5–32 chars, no @
    telegramUserId: 12345      # usually set by /start, not by hand
    gender: male               # drives grammatical agreement in reminders
    teamId: mobile-developers
    startedOn: 2025-01-15
    endedOn: 2026-08-07        # inclusive last working day

calendar:
  closed: [2026-12-31]         # shut for a non-holiday reason
  extraWorkdays: [2026-01-10]  # Kazakhstan transfers holidays onto Saturdays

absences:
  - employeeId: alikhan-khassen
    startDate: 2026-07-01
    endDate: 2026-07-14
    kind: vacation             # vacation | sick | trip | other
```

`vacantDesks` is how many people to draft **in addition to** the fixed list, not the day's
total capacity. `OfficeSeed.references_resolve` rejects duplicate employee ids, a
`schedule.fixed` entry naming someone who does not exist, and an absence for an unknown
employee — all at startup.
