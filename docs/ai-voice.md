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

## The four tokens

| Token | Filled by | Example |
|---|---|---|
| `{when}` | us | `Завтра` / `В понедельник` |
| `{date}` | us | `14 сентября 2026 года` |
| `{office}` | us | `«Pine Office Park»` — the quotes are added by the code |
| `{tail}` | the model, or a written pool | `Кофемашина уже нервничает.` |

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
{"tail": "кофемашина уже нервничает", "epithets": ["Несгибаемый защитник", "Душа компании"]}
```

The model is told only how many epithets are needed and what gender each must be
(`1) мужской род, 2) женский род`). It is never sent the names.

Then:

- The tail runs through `render_tail`. Rejected → the written per-mood tail.
- Each epithet runs through `render_epithet` **independently**. Rejected → the written
  per-mood, per-gender epithet for that slot. A model that titles one person well and
  fumbles the next costs only the second title.
- Emoji are ours, never the model's, picked by shuffling the pool with a stable hash so
  they do not repeat inside one message.

The rendered line loop is over `snapshot.roster`. **Decoration is optional; being
reminded is not.** With the model off, broken or out of credit, everyone scheduled still
gets a tagged line with a written title. There is a test that asserts exactly this.

## The guards

In `voice.py`, applied to generated text only. Each returns `None` on refusal, and every
caller has a working fallback — so rejecting is cheap and the guards are deliberately
strict.

| Check | Catches |
|---|---|
| Length | A runaway generation |
| Braces, `@`, `<`, `>` | Failed substitution, mentions, markup injection |
| Any digit | An invented date, headcount or statistic |
| Weekday / month / "завтра" stems | The duplication this whole design exists to prevent |
| Office name stems | *"офис «X» … в X снова весело"* |
| Roster name stems | A colleague it was never given |
| Banned terms | `personality.bannedTerms` |
| Shouting (>60% caps, 12+ letters) | A model having a moment |
| Gender agreement (epithets) | *"Мужик"* in front of a woman's name |

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
4. **Guards rejected it** → written text, per slot for epithets.

Plus: over the daily allowance → an `ai.rateLimited` variant; output containing a banned
term → an escaped ellipsis.

## Cost

- `gpt-4.1-nano`, `ai.maxTokens` 400, `ai.temperature` 1.0.
- **One call per chat message**, capped at 10 per person per day and 200 globally
  (`globalDailyLimit` is a stop-loss, not a quota).
- **One call per reminder per office** — two a day. Not rate-limited; it does not need to
  be at that volume.
- The attempt is counted **whatever happened**, including failures. Otherwise a broken
  model is a free, unlimited way to spend money.
- `ai_usage.tokens` records the API's own count and appears on the admin status screen.
  It read zero for months because the client discarded `response.json()["usage"]`.

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
  substitution.

A missing mood is a startup error rather than a silent fall back to a default voice,
which is how a missing mood stays invisible until 15:30 on a Friday.

## After changing any of this

```bash
uv run pytest tests/application/test_voice.py tests/integration/test_attendance_reminder.py \
              tests/integration/test_chat_context.py -q
uv run tabelshchik validate
```

Then look at the output with your own eyes. Render every mood, with the model answering
well, answering badly, and switched off entirely. The tests check the contract; only
reading the messages tells you whether they are any good.
