# Configuration

Three files, one rule about which of them is in charge, and a recipe for adding a setting
without leaving half of it unwired.

## The files

| File | Holds | Authority |
|---|---|---|
| `config/app.yaml` | Operational settings: timezone, reminder times, silent hours, schedule policy, retention, moods, AI, health | **Always live.** Read at boot; there is no copy in the database. |
| `config/messages.yaml` | Every user-facing string, per mood | **Always live.** See [ai-voice.md](ai-voice.md). |
| `config/offices/*.yaml` | Offices, employees, weekly template, calendar exceptions, absences | **Seed only.** See below. |

Secrets are never in any of them: `TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY`, `ADMIN_IDS`,
`ADMIN_CHAT_ID` come from the environment (`.env` on the server), via
`bootstrap/settings.py`.

## Seed versus database

An office file is **bootstrap data, not a live source of truth**. `seed_offices` inserts
an office the first time it is seen and then skips it forever
(`replace_existing=False`).

Two consequences that surprise people:

- **Editing an office file after first boot does nothing.** Adding a person to
  `ovest.yaml` on a running system has no effect. Use the admin UI
  (`/admin` → office → 👥 Сотрудники), which writes to the database through
  `application/manage_roster.py`.
- **The skip is at whole-office granularity.** If `models.Office(id=…)` exists, the entire
  file is skipped — employees, template, calendar, absences and all.

Flipping `replace_existing=True` — or adding a "reload config" button that does — would
**hard-delete employees and cascade away their assignments and ledger history**. The
admin "⚙️ Конфиг" screen deliberately only validates the files on disk; it applies
nothing.

The office files are still worth keeping accurate. They are what a rebuild from an empty
database produces, and the only human-readable record of the starting state.

## Validation

Every model inherits `config/models.py:Base`:

```python
model_config = ConfigDict(
    extra="forbid",              # a typo is a startup error, not a silent default
    alias_generator=to_camel,    # YAML is camelCase, Python is snake_case
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
| `admins` | Telegram **user** ids allowed into admin mode. `ADMIN_IDS` overrides. |
| `reminders.attendance` | `time`, `runOn`, `pin`. Listing `sun` is what covers Sun→Monday; Mon→Tue and Fri→Mon fall out of the one rule. |
| `silentHours` | Per weekday with a `default`. Silent still *sends* — it just does not buzz. A window may cross midnight. |
| `schedule` | `horizonWeeks` generated, `freezeWeeks` immutable (must be smaller, validated), `maxDaysPerWeek`, `absencePolicy`, `surplusClamp`. |
| `retention` | `scheduleMonths` 3, `jobRunsDays`, `auditDays`, `aiUsageDays`, `chatMessagesDays`. |
| `personality` | `moods` weights, `safeMode`, `bannedTerms`. |
| `ai` | Model, sampling, daily limits, triggers, and `chat` context budgets. |
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
