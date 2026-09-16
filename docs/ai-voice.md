# The bot's voice

Everything the model touches, and everything it is not allowed to touch. Read this before
editing a prompt, a guard, or `config/messages.yaml`.

## The one rule

**The model never receives a fact, and never emits one.**

It does not know the date, the weekday, the office name, or who is on the roster. It
writes a flavour clause and some epithets. Every date, weekday, office and person in a
sent message was placed there by `application/voice.py`.

This is a *structural* guarantee, not a filter. A hallucinating model can produce an
awkward phrase; it cannot announce the wrong day, the wrong office, or a colleague who
does not exist, because it was never given one to get wrong.

Two messages bend the rule, and only as far as they must: the Tempo reminder and the
holiday greeting are written whole by the model (see
[Whole messages](#whole-messages-tempo-and-holidays)). They are told today's weekday, and
the greeting today's date and the holiday's name, because "С пятницей!" and "С 8 Марта!"
are the point. The guard then allows exactly those words and numbers and nothing else.

### Why it is built this way

The reminder used to hand the model a `{date}` token and tell it to include one, without
telling it what the token expanded to. `{date}` was `"В понедельник, 14 сентября 2026
года"` — lead-in and date concatenated. The model, reasonably, wrote its own lead-in in
front of it:

> В понедельник **В понедельник, 14 сентября 2026 года**, словно комары на опенспейсе…

The same bug was in the hand-written templates, which had been written assuming `{date}`
was a bare date: `"Никто не просил, но {date} в офис надо"` rendered as *"…но В
понедельник, 14 сентября в офис надо"*.

A guard that detects the repetition would only ever be a filter. Removing the token from
the model's hands removes the failure mode.

## The tokens

| Token | Filled by | Example |
|---|---|---|
| `{when}` | us | `Завтра` / `В понедельник` |
| `{date}` | us | `14 сентября 2026 года` |
| `{office}` | us | `«Pine Office Park»` — the quotes are added by the code |
| `{tail}` | the model, or a written pool | `Кофемашина уже нервничает.` |
| `{minutes}` | us | `10 минут` — accusative, declined by `plural_minutes` from `common.minutes` |
| `{holiday}` | us | `«Наурыз мейрамы»` — the country's own name, quotes included |
| `{url}` | us | only in `tempo.footer`, which is markup and is not escaped |

`{when}` is capitalised and **must be sentence-initial** in every template: at the start
of the string, or immediately after `.` or `!`. `"Ждём {when}, …"` renders as *"Ждём
Завтра, …"*, which is wrong in Russian.

`{tail}` is capitalised and punctuated by `_as_sentence`, because every template places it
after a sentence end.

Substitution is **literal `str.replace`, never `str.format`**, and escaping happens first.
A stray brace in the data can therefore neither raise nor interpolate something
unintended.

## The reminder, end to end

`bootstrap/jobs.py` → `application/send_attendance_reminder.py` → `voice.decorate()` →
`voice.static()` → the notifier.

`Voice.decorate()` makes **one** request returning JSON:

```json
{
  "tail": "кофемашина уже нервничает",
  "epithets": ["Несгибаемый защитник", "Душа компании"],
  "emojis": ["🛡", "🎉"]
}
```

The model is told only how many epithets are needed and what gender each must be
(`1) мужской род, 2) женский род`), plus the emoji it may choose from. It is never sent
the names.

Then:

- The tail runs through `render_tail`. Rejected → the written per-mood tail.
- Each epithet runs through `render_epithet` **independently**. Rejected → the written
  per-mood, per-gender epithet for that slot. A model that titles one person well and
  fumbles the next costs only the second title.
- Each emoji runs through `render_emoji`, which is an **allowlist** (`SAFE_EMOJI`, about
  ninety icons) rather than a pattern check. Rejected → the mood's own pool for that slot,
  shuffled with a stable hash so icons do not repeat inside one message.

  Asking the model for the icon is what makes it mean something: ☕ goes to «Кофейный
  гений» and 🗺 to «Планировщик маршрутов». Before, the icon was dealt from a pool seeded
  on the office and the day, so it had nothing to do with the title beside it — and two
  renders of the same day produced different titles under identical icons, which is what
  made the icons look hardcoded.

  An allowlist rather than "is this an emoji": a work chat is not the place to find out
  what a model considers appropriate. It refuses flags (a political statement in some
  rooms), skin-tone modifiers (not ours to assign to somebody), and anything not on the
  list. Every curated pool icon is on it, so the same icon is never judged two ways —
  there is a test for that.

The rendered line loop is over `snapshot.roster`. **Decoration is optional; being
reminded is not.** With the model off, broken or out of credit, everyone scheduled still
gets a tagged line with a written title. There is a test that asserts exactly this.

## Whole messages: Tempo and holidays

`Voice.announce()` asks for one JSON object, `{"text": "…"}`, given the day's persona and a
brief that the use case writes. The link, the tagged names and the office header are
added by the code afterwards, and the model is told so. The result goes through
`render_announcement`; anything off and the hand-written line is sent instead, with no
sign that anything happened. `Announcement.generated` records which one it was.

**Tempo** (`application/send_tempo_reminder.py`) runs every day at the configured time
and sends on the last working day of the week, the last working day of the month, or both
— as one message, from `tempo.monthEnd` when the month is involved. The brief says:

- what today is, and *why* it ends the week when it is not Friday (`пятница — праздник`),
  because otherwise the model congratulates a Thursday on being Friday;
- whether the month closes today, and not to congratulate on the week when it does not;
- if the reminder lands 1–30 minutes before the end of the workday
  (`ReminderTimes.tempo_gap_minutes`), exactly how many minutes are left and that those
  are the ones to spend on Tempo. That number is the only one the guard allows. With no
  gap it is told not to count anything down, and the written line has no `lastMinutes`
  sentence either.

Then `tempo.footer` (the link as tappable text), then everyone active, tagged. The
message is pinned after every earlier Tempo pin recorded in `bot_post` for that office and
chat has been unpinned — pin notifications are what reach the whole chat.

**Holidays** (`application/send_holiday_greeting.py`) runs every day, asks
`HolidayCalendar.celebrations()` about today, and greets only the first day of a holiday
(Nauryz is three days, one greeting). Transferred days off and the weekday a weekend
holiday was moved onto are not celebrations and are filtered in the adapter. Religious and
memorial holidays (`SOLEMN_WORDS`) get a brief that forbids irony whatever the mood. The
model gets the English name, which it understands for any country; the written fallback
uses the local one. No tags, no pin, and one greeting per chat even when two offices share
it.

## The guards

In `voice.py`, applied to generated text only. Each returns `None` on refusal, and every
caller has a working fallback — so rejecting is cheap and the guards are deliberately
strict.

| Check | Catches |
|---|---|
| Length | A runaway generation |
| Braces, `@`, `<`, `>` | Failed substitution, mentions, markup injection |
| Any digit (except `allow_numbers`) | An invented date, headcount or statistic |
| Weekday / month / "завтра" stems (except `allow_words`) | The duplication this whole design exists to prevent |
| `http:`, `://`, `www.`, `.kz` and friends (whole messages) | A link of its own, next to ours |
| Office name stems | *"офис «X» … в X снова весело"* |
| Roster name stems | A colleague it was never given |
| Banned terms | `personality.bannedTerms` |
| Shouting (>60% caps, 12+ letters) | A model having a moment |
| Gender agreement (epithets) | *"Мужик"* in front of a woman's name |
| Allowlist (emojis) | A flag, a skin tone, or whatever else the model reached for |

### Stem matching, carefully

Russian declines, so `понедельник` has to catch `понедельника`. But dropping the final
character of a short word is a disaster: `мая` → `ма`, which matches *кофемашина*,
*малиновый* and most of the language.

`_prefixes` keeps words under four characters whole and matches only at **word
boundaries**. `name_stems` is the exception — it substring-matches down to two characters,
because a false positive costs one written line while a real name in a group chat does
not.

### Gender agreement

`_agrees` is a heuristic and deliberately a strict one. It looks at the head word's
ending, which is unambiguous for adjectives (`-ый/-ий/-ой` vs `-ая/-яя`), and treats a
lone masculine-looking noun as a mismatch for a female slot. Over-rejecting costs one
written title that reads just as well.

`Employee.gender` comes from the office YAML and defaults to `male`. Setting it is part
of the admin add-employee flow for exactly this reason.

## Moods

Five: `toxic`, `fun`, `happy`, `sad`, `depressive`. Weighted **55 / 25 / 12 / 5 / 3** —
mostly toxic as intended, with `sad` and `depressive` rare on purpose: they read as flat
rather than funny, and one day in five of them makes the bot merely miserable.

Selection is **deterministic, not random**: `stable_hash("mood", office_id, day)`. One
mood per office per day, so a restart mid-afternoon does not change the bot's personality
halfway through. The same mechanism picks which written variant of a line is used.

`safeMode: true` or `personality.enabled: false` forces `fun` everywhere, immediately.

Weights are defined in three places that must agree: `config/app.yaml`,
`domain/mood.py:DEFAULT_WEIGHTS`, and `config/models.py:_DEFAULT_MOODS`.

### Writing persona text

The persona sets **tone only**. It must not list topics.

The toxic persona used to say *"Шутишь едко — но только про понедельники, дорогу до
офиса, опенспейс…"*, and the model dutifully brought up Monday in every single answer, on
every day of the week, including Fridays. Describe the character and let the question
decide the subject. There is a test asserting the word does not appear in the chat system
prompt.

House rules, also stated at the top of `messages.yaml`:

- Toxicity aims at the situation — the commute, the open-plan office, Tempo, the concept
  of work. **Never at a person.**
- Nothing about appearance, competence, personal life, health, ethnicity, gender,
  religion, age or nationality.
- `sad` and `depressive` are deadpan about the working week, not about being alive. A bot
  that reads as genuinely distressed in a work chat is neither funny nor kind to whoever
  feels obliged to respond.

## The chat

`application/chat.py`. Triggered by mention, reply, or private message
(`ai.triggers`). One request per question, returning JSON:

```json
{"reply": "…", "remember": {"scope": "office" | "user" | null, "fact": "…"}}
```

The reply and the decision about what to remember come back together. Two calls would
cost twice as much and could disagree with each other. Prose instead of JSON is used
as-is and nothing is remembered — a model that ignored the contract still answered.

A JSON object that does not parse is almost always a reply the token ceiling cut off.
`_salvage` recovers the text of `"reply"` up to the cut and ends it with `…`; it used to be
sent verbatim, braces and all.

### What it will talk about, and for how long

Anything. The system prompt tells the model to answer any question — code, science,
everyday things — and not to steer back to the office. The persona keeps it in character
and the day's mood colours the tone; the boundaries are the house rules below plus no help
with the plainly harmful. Facts about the office still come only from the prompt.

Length is the model's call, and it is told how to make it: a line or two for a simple
question, a full answer with steps for one that needs it. The prompt used to cap every
answer at three sentences, which is why they all read as terse. The ceiling is
`ai.chat.maxTokens` (2000), separate from `ai.maxTokens` (400) which bounds what the
reminders ask for, and `MAX_ANSWER_LENGTH` is 6000 characters. The router splits anything
over Telegram's 4096 on line breaks, so an escaped entity is never cut in half, and caches
every part so a reply to any of them finds the thread. Output is plain text: Markdown
would arrive as literal asterisks, since everything the model writes is escaped.

### What goes into the prompt

1. **Today's date and weekday.** Without it the model cannot know it is Friday, which is
   how it ends up insisting on Monday to somebody who just said otherwise.
2. The asker's own upcoming office days, and tomorrow's roster.
3. **Memory** — shared facts always, personal facts only for the person they are about.
4. **The reply chain**, oldest first.

Every part has a hard character budget, so the worst-case prompt size is known in advance
instead of discovered on an invoice.

### Reply chains

Telegram populates `reply_to_message` **exactly one level deep**. Anything further back is
reconstructed from the `chat_message` table, which caches every message the bot receives
plus its own replies — the latter matter most, since a follow-up usually hangs off the
bot's own answer.

Depth is `ai.chat.replyDepth` (10). The chain is trimmed from the **oldest** end when it
exceeds `replyChars`, because the nearest messages are the ones the question refers to.

**The honest limit:** with Telegram privacy mode on (the default) the bot only receives
messages that mention it or reply to it, so a chain through ordinary chatter stops at the
first link nobody cached. That is a partial chain, never a wrong one. Setting privacy to
Disabled in @BotFather unlocks the full depth and means the bot receives every group
message. No code changes either way. Raw messages are pruned after
`retention.chatMessagesDays` (7) and used for nothing else.

### Memory

Two scopes in `chat_memory`:

- `office` — shared with everyone in that office's chat.
- `employee` — injected **only** when that exact person is asking. A personal fact from
  an unlinked stranger is discarded rather than filed under a guess.

Both are **ring buffers**: writing a new fact is what evicts the oldest. That is the whole
retention policy — a bounded number of rows means a bounded prompt. `application/memory.py`
holds the pure half (cleaning, truncation on a word boundary, near-duplicate collapse by
word overlap against the *smaller* set, so a restatement merges).

`ai.chat.remember: false` switches the whole thing off, including the JSON field in the
prompt.

## Fallbacks

Four layers, all silent. The written corpus is the floor on quality.

1. **No key / `ai.enabled: false`** → `Voice.model is None`. Reminder uses written text;
   chat answers `ai.disabled`.
2. **Empty persona for the mood** → written text.
3. **Model returned nothing** (network, 4xx, retries exhausted) → written text; chat
   answers an `ai.failed` variant.
4. **Guards rejected it** → written text, per slot for epithets and per slot for emoji.

Plus: over the daily allowance → an `ai.rateLimited` variant — "я с тобой больше не
разговариваю, до завтра", in the day's mood — on every further attempt, a different line
each time (the variant is picked with a nonce hashed from the question), and at no cost.
The office-wide cap answers from `ai.globalLimited` instead, because the person asking
may not have said a word all day. The allowance is per person, and either the default or
one person's own number can be changed from `/admin` without a deploy — see
[configuration.md](configuration.md#ai-limits). Plus: output containing a banned term → an
escaped ellipsis.

## Cost

- `gpt-4.1-nano`, `ai.temperature` 1.0. `ai.maxTokens` 400 for reminders and greetings,
  `ai.chat.maxTokens` 2000 for a chat reply.
- **One call per chat message**, capped at 10 per person per day and 200 globally
  (`globalDailyLimit` is a stop-loss, not a quota). The worst case is the cap times a
  full 2000-token reply — at nano prices, cents a day. A reply is only that long when the
  question needs it.
- **One call per reminder per office**: the attendance reminder daily, Tempo on the
  days it is due, a greeting on holidays. Not rate-limited; it does not need to be at
  that volume.
- The attempt is counted **whatever happened**, including failures. Otherwise a broken
  model is a free, unlimited way to spend money.
- `ai_usage.tokens` records the API's own count and appears on the admin status screen.
  It read zero for months because the client discarded `response.json()["usage"]`.

## The workbook caption

`schedule.caption` carries `{summary}`, which is **already rendered and already
HTML-safe** — escaping it again at the call site shows literal `&lt;b&gt;`.

The summary is the coming week only: tomorrow and the six days after it, windowed by
**date**. It used to take the first seven *scheduled* days, which for an office that
fills desks only on Fridays meant six weeks of identical rosters, comfortably past
Telegram's 1024-character caption limit, and the message arrived cut off mid-name.

`reports/xlsx.schedule_caption` composes the caption to fit rather than truncating it.
If the week will not fit, days that do are kept and a line says the rest is in the
attached file. Never slice a caption to length.

## `config/messages.yaml`

Schema in `config/messages.py`, flattened to dotted catalog keys in
`bootstrap/mapping.py` — that mapping is the authoritative list of keys.

Three shapes:

- `MoodVariants` — a list per mood, one picked stably per day. All five moods required.
- `MoodGendered` — male/female lists per mood (epithets only).
- `MoodText` — one string per mood (`ai.persona` only).

Startup validators, which will refuse to boot:

- every `attendance.intro` line carries `{when}`, `{date}`, `{office}` and `{tail}`;
- every `attendance.empty` line carries `{when}` and `{date}`;
- `attendance.tails` and `attendance.emojis` contain **no braces** — they are substituted
  *into* a template, so a brace would survive into the final text and read as a failed
  substitution;
- `tempo.footer` carries `{url}`, every `tempo.lastMinutes` line carries `{minutes}`, and
  every `holiday.greeting` line carries `{holiday}`.

`tempo.weekly` names no weekday: the last working day of the week is not always a Friday.
`tempo.lastMinutes` lines need a verb that takes the accusative (`потратьте`, `уделите`),
because that is the case `{minutes}` is declined in.

A missing mood is a startup error rather than a silent fall back to a default voice,
which is how a missing mood stays invisible until 15:30 on a Friday.

## After changing any of this

```bash
uv run pytest tests/application/test_voice.py tests/integration/test_attendance_reminder.py \
              tests/integration/test_chat_context.py tests/integration/test_tempo_and_chat.py \
              tests/integration/test_holiday_greeting.py -q
uv run tabelshchik validate
```

Then look at the output with your own eyes. Render every mood, with the model answering
well, answering badly, and switched off entirely. The tests check the contract; only
reading the messages tells you whether they are any good.
