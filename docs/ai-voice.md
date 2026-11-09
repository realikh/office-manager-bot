# The bot's voice

Everything the model touches, and everything it is not allowed to touch. Read this before
editing a prompt, a guard, or `config/messages.yaml`.

## The one rule

**The model never receives a fact, and never emits one.**

It does not know the date, the weekday, the office name, or who is on the roster. It
writes a flavour clause. Every date, weekday, office and person in a sent message was
placed there by `application/voice.py`.

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

`bootstrap/jobs.py`, then `application/send_attendance_reminder.py`, then `voice.tail()`,
then `voice.static()`, then the notifier.

`Voice.tail()` makes **one** request returning JSON:

```json
{"tail": "кофемашина уже нервничает"}
```

The model is told nothing about the day, the office or the people, only the persona and
today's icons. The icons are twenty from the allowlist (`SAFE_EMOJI`), picked by
`emoji_choices` with `stable_hash("emoji-choices", office, day)`, so a different set every
day and the same set all day. They are offered as imagery for the phrase and must not
appear in it. Without them the same prompt every morning gets the same answer: the prompt
once named its topics ("про кофе, дорогу, стойкость") and the phrase came back about
coffee every day, the persona lesson below learnt a second time.

The tail runs through `render_tail`. Rejected, it is replaced by the written per-mood tail.

Each roster line is the person and nothing else: the tag, or the name with the username,
or the bare name. The lines used to carry a model-written title and an icon in front of
each name ("кирпичный философ"); they were removed at the owner's request, and with them
the gender check and the per-person icon pools.

The rendered line loop is over `snapshot.roster`. **The phrase is optional; being reminded
is not.** With the model off, broken or out of credit, everyone scheduled still gets a
tagged line. There is a test that asserts exactly this.

## Whole messages: Tempo and holidays

`Voice.announce()` asks for one JSON object, `{"text": "…"}`, given the day's persona and a
brief that the use case writes. The link is added by the code afterwards, and the model is
told so. The result goes through
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

Then `tempo.footer`, the link as tappable text, and nothing else. Nobody is tagged: the
message is pinned after every earlier Tempo pin recorded in `bot_post` for that chat has
been unpinned, whichever office posted it, and the pin notification is what reaches the
whole chat.

Offices sharing one group get one message. Every office's job fires in the same minute,
so "has this chat had one today" checked before the model call would let both through.
Instead each job works out which office speaks for the chat (`chats.speaker`): the first
office bound to it, by id, for which the message is due today by its own calendar. The
others return `shared-chat`. An office whose week ends on a Thursday still gets its
Thursday message, and the speaker's Friday one then takes that pin down.

**Holidays** (`application/send_holiday_greeting.py`) runs every day, asks
`HolidayCalendar.celebrations()` about today, and greets only the first day of a holiday
(Nauryz is three days, one greeting). Transferred days off and the weekday a weekend
holiday was moved onto are not celebrations and are filtered in the adapter. Religious and
memorial holidays (`SOLEMN_WORDS`) get a brief that forbids irony whatever the mood. The
model gets the English name, which it understands for any country; the written fallback
uses the local one. No tags, no pin, and one greeting per chat even when two offices share
it: the same speaker rule, with "due" meaning that office's calendar has a new celebration
today. The greeting covers every office in the chat that celebrates, so a holiday only one
of them keeps is still greeted.

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

### Stem matching, carefully

Russian declines, so `понедельник` has to catch `понедельника`. But dropping the final
character of a short word is a disaster: `мая` → `ма`, which matches *кофемашина*,
*малиновый* and most of the language.

`_prefixes` keeps words under four characters whole and matches only at **word
boundaries**. `name_stems` is the exception — it substring-matches down to two characters,
because a false positive costs one written line while a real name in a group chat does
not.

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
(`ai.triggers`). The orchestration in one paragraph: every question gets a small context
that is always there (now, who is writing, where, and the recent conversation). The
schedule and saved notes are not in it; they are two lookups the model calls through
OpenAI function calling, run locally against the database. A general question is answered
by the first call. A schedule or memory question costs one more. What is worth remembering
comes back inside the final answer, so writing a note never costs a call:

```json
{"reply": "…",
 "notes": [{"about": "me", "topic": "день рождения", "text": "3 марта", "until": null}],
 "forget": [{"about": "office", "topic": "кофемашина"}]}
```

Prose instead of JSON is used as-is and nothing is remembered: a model that ignored the
contract still answered.

A JSON object that does not parse is almost always a reply the token ceiling cut off.
`_salvage` recovers the text of `"reply"` up to the cut and ends it with `…`; it used to be
sent verbatim, braces and all.

### Which language

The language of the message it is answering, whatever that is; Russian only when the
message is Russian or its language cannot be told. That is the first rule of the system
prompt, with an English gloss beside it, because everything else in the prompt is Russian
and a rule buried in the middle ("пиши на русском, если собеседник не пишет на другом
языке") lost to all of it: English questions came back in Russian. The canned lines
(rate limit, failure, chat switched off) are copy in `messages.yaml` and stay Russian.

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

### What is always in the prompt

1. **Today's date, weekday and time.** Without it the model cannot know it is Friday,
   which is how it ends up insisting on Monday to somebody who just said otherwise.
2. **Who is writing**: their roster name and office, or "not on any roster". A woman on
   the roster gets a hint to be addressed in the feminine; male is the default and often
   simply unset, so it is never passed and the model goes by the name.
3. **Where**: a private chat, an office's group, or a group shared by several offices.
4. **The conversation** (`application/transcript.py`), described below.

The asker's schedule, tomorrow's roster and every saved fact used to be in every prompt.
They are lookups now.

### The conversation

Built from the `chat_message` cache, which keeps every message the bot sees in a chat it
answers in, plus its own replies. Two sources, in this order:

- the reply chain above the message being answered, followed through the cache whoever
  wrote each link, up to `ai.chat.history.replyDepth` (10), and never more than half the
  budget, so a deep chain cannot crowd out what was just said;
- the latest messages of the chat, newest first, until the budget runs out.

The budget is characters, `history.chars` (6000), with each message clipped to
`history.messageChars` (1000) and `history.messages` (60) only as a ceiling. Chat lines of
a hundred or two hundred characters give thirty to sixty messages; a run of long ones gives
about six. Messages are printed oldest first, numbered, with their time and author, and a
reply says which number it answers: `#4 [13:41] Вера (ответ на #2): ...`. That is what lets
the model follow two other people talking to each other, not only replies to itself. A
parent outside the window is quoted in one line; with `replyDepth: 0` nothing is quoted.

The message being answered is never in the transcript: it is the question, and quoting it
back would pay for it twice. Anything with a higher message id is left out too, so a
message that arrived while the bot was working cannot leak into the answer to an earlier
one. The router stores Telegram's own time for each message; the parent of a reply used to
get the child's arrival time.

**What the bot can see.** The bot pins, so in the office groups it is an admin, and
Telegram delivers every group message to admin bots whatever the privacy setting. In a
group where it is not an admin, privacy mode limits it to mentions and replies, and the
transcript is partial, never wrong; Disabled in @BotFather lifts that. Captions are cached
too, so a photo with a caption is part of the conversation; only text is ever answered.
Raw messages are pruned after `retention.chatMessagesDays` (7).

**What leaves the building.** Every answer in a group carries up to 6000 characters of
that group's recent conversation to OpenAI. That was asked for; it is still worth knowing.

### Lookups

`application/chat_tools.py`. Two tools, read-only, plain Russian text out, each result
clipped to `ai.chat.lookupChars` (8000):

- **`schedule(office?, from?, to?)`**: one line per day with the people on it, the asker's
  own days first, where the schedule ends, ISO dates, at most 62 days at a time. Which
  offices it may show comes from the gate: `Grant.offices`. In an office group that is
  every office bound to the group, which is already where both rosters and the workbook are
  posted. Privately, an employee's own office, or every open office for an admin.
- **`recall(about: "me" | "office" | "both")`**: the asker's own notes and their office's,
  newest first, each with the date it was written and, if set, the date it stops being
  true. Marks what it returned as touched.

The loop calls the model; if it asks for lookups, runs them (three per round at most,
every call id answered, the extras with "skipped", because the API refuses a conversation
with an unanswered call), and calls again. After `ai.chat.toolRounds` (2) rounds the last
call goes with `tool_choice: "none"` and has to answer; a model that still asks for a
lookup fails the question rather than loops. Bad arguments come back to the model as an
error line. One question counts once against the allowances, and every call's tokens are
added to its usage.

### Notes

`application/memory.py` is the pure half, `SqlNoteStore` the stored one, in `memory_note`.

- **`me` notes** belong to the asker's Telegram account and are only ever recalled for
  that person. The model is told not to bring them up unasked in a group.
- **`office` notes** belong to the office, and only a linked employee in tenure can write
  them, into their own office. Anyone in a group passes the gate; a guest or an admin with
  no roster record can keep personal notes but cannot plant office ones.
- **The topic is the key.** Writing the same topic again updates the note, which is how
  "кофемашина сломана" becomes "кофемашина работает" instead of both. Office topics name
  who or what they are about ("день рождения Бори"), so two birthdays never share one.
  Nothing is ever replaced across topics; a text nearly identical to a note already there
  (word overlap over the union of 0.9 or more, same numbers) is skipped. Overlap against
  the smaller set used to collapse "У Бори день рождения 5 мая" into "У Пети ...".
- Notes are written in Russian whatever the conversation language, cleaned of markup,
  links and handles, at most `memory.noteChars` (200) each and `memory.notesPerReply` (3)
  per answer. `until`, when the model sets one, must be a real date within a year.
- **Limits**: `memory.personalNotes` (30) per person, `memory.officeNotes` (60) per office.
  Over the limit, the note least recently written or looked up goes. A note nobody wrote
  or recalled for `retention.memoryIdleDays` (365) is pruned, and so is one past its
  `until`. Deleting an office deletes its notes.

`ai.chat.memory.enabled: false` removes the recall lookup and the notes from the contract.

## Fallbacks

Four layers, all silent. The written corpus is the floor on quality.

1. **No key / `ai.enabled: false`** → `Voice.model is None`. Reminder uses written text;
   chat answers `ai.disabled`.
2. **Empty persona for the mood** → written text.
3. **Model returned nothing** (network, 4xx, retries exhausted) → written text; chat
   answers an `ai.failed` variant.
4. **Guards rejected it** → written text.

Plus: over the daily allowance → an `ai.rateLimited` variant — "я с тобой больше не
разговариваю, до завтра", in the day's mood — on every further attempt, a different line
each time (the variant is picked with a nonce hashed from the question), and at no cost.
The office-wide cap answers from `ai.globalLimited` instead, because the person asking
may not have said a word all day. The allowance is per person, and either the default or
one person's own number can be changed from `/admin` without a deploy — see
[configuration.md](configuration.md#ai-limits). Plus: output containing a banned term → an
escaped ellipsis.

## Cost

- `gpt-6-luna` with `ai.reasoningEffort: none`, `ai.temperature` 1.0. `ai.maxTokens` 400 for
  reminders and greetings, `ai.chat.maxTokens` 2000 for a chat reply. Reasoning tokens count
  against `max_completion_tokens`, so at the API's default effort a 400-token reminder can
  spend the whole budget thinking and come back empty. Set `reasoningEffort: null` for a
  model without reasoning, which rejects the parameter.
- `gpt-4.1-nano` was the model until OpenAI shut it down on 2026-10-23. Every request was
  then refused with a 4xx, and every caller fell back to the written lines without a word.
  A shutdown looks exactly like that, so check
  [the deprecations page](https://developers.openai.com/api/docs/deprecations) first.
- **One call per chat message, two or three when it looks something up**, capped at 10
  questions per person per day and 200 globally (`globalDailyLimit` is a stop-loss, not a
  quota). Every round resends the whole prompt, and the conversation window is up to 6000
  characters, so a question costs several times what it did with only a reply chain. At
  the caps that is still cents a day.
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

Two shapes:

- `MoodVariants` — a list per mood, one picked stably per day. All five moods required.
- `MoodText` — one string per mood (`ai.persona` only).

Startup validators, which will refuse to boot:

- every `attendance.intro` line carries `{when}`, `{date}`, `{office}` and `{tail}`;
- every `attendance.empty` line carries `{when}` and `{date}`;
- `attendance.tails` contain **no braces** — they are substituted
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
uv run pytest tests/application/test_voice.py tests/application/test_memory.py \
              tests/application/test_transcript.py tests/integration/test_attendance_reminder.py \
              tests/integration/test_chat_context.py tests/integration/test_tempo_and_chat.py \
              tests/integration/test_holiday_greeting.py -q
uv run tabelshchik validate
```

Then look at the output with your own eyes. Render every mood, with the model answering
well, answering badly, and switched off entirely. The tests check the contract; only
reading the messages tells you whether they are any good.

For the chat, `tabelshchik ask --user-id N [--chat-id N] "..."` puts one question to the
real model against the configured database and prints which lookups it made. It writes
usage and notes like the bot does, so point it at a copy of the database.
