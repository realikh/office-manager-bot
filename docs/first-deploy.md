# First deploy — runbook

For the setup you have now: an Oracle VM, one bot token, one group chat, and two offices
sharing that group.

Work through it in order. Steps 1–3 are on your laptop, 4–6 on the VM, 7 is the part
that decides whether you trust it.

---

## 0. What you'll need in front of you

| Value | Where it goes | Notes |
|---|---|---|
| Bot API token | `TELEGRAM_BOT_TOKEN` | From @BotFather |
| Your Telegram user id | `ADMIN_CHAT_ID` **and** `ADMIN_IDS` | Same number for both — a private chat's id *is* the user id |
| Group chat id | `chatId:` in both office YAML files | Negative, usually starts `-100` |
| OpenAI key | `OPENAI_API_KEY` | Optional; without it the bot uses its hand-written lines |

Never commit these. `.env` is git-ignored; the office YAML files are not, so the group id
does end up in git — that is fine, a chat id is not a secret.

---

## 1. Telegram prerequisites

Three things that will silently not work if you skip them:

1. **Open a DM with the bot and press Start.** Telegram forbids a bot from messaging a
   user who has never messaged it. Skip this and failure alerts and nightly backups go
   nowhere, with no error you would notice.
2. **Add the bot to the group.**
3. **Make it an administrator, with "Pin messages".** Tempo reminders pin by default. A
   missing permission is handled gracefully — the message still sends, the pin is logged
   as failed — but you would rather have the pin.

Leave **privacy mode ON** (the @BotFather default). It means the bot sees only commands,
messages that @-mention it, and replies to its own messages — which is exactly the three
triggers the chat feature uses. Turning it off would give the bot every message in the
group for no benefit.

---

## 2. Point both offices at the group

Edit two files. Same id in both — they share the chat, and messages will carry an office
header so the two stay distinguishable.

```bash
# config/offices/ovest.yaml  and  config/offices/pine-office-park.yaml
chatId: -1001234567890
```

Then check it locally:

```bash
uv run tabelshchik validate
```

You want to see both offices listed with a chat id, and a note saying the chat is shared.

---

## 3. Get the code onto GitHub

Create an **empty private repo** at github.com/new — no README, no .gitignore, no
licence. Then, from the project directory:

```bash
git remote add origin git@github.com:<you>/office-manager-bot.git
git push -u origin main
```

(Use the HTTPS URL instead if you don't have SSH keys on GitHub.)

---

## 4. Prepare the VM

SSH in, then:

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl git
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER" && newgrp docker
sudo systemctl enable --now docker
```

The `systemctl enable` matters. Without it `restart: unless-stopped` brings the container
back after a crash but **not** after a host reboot, which is the more likely outage.

### While you're here: idle reclamation

Oracle reclaims Always Free instances whose CPU **and** network both sit under 10% for a
continuous 7 days. This bot fits that description precisely.

The fix is to **upgrade the tenancy to Pay As You Go and stay inside the Always Free
limits**. Your bill stays $0 — reclamation is a policy for Always Free *accounts*, not
for Always Free *resources* inside an upgraded one. Do it now rather than after the
instance disappears.

---

## 5. Deploy

```bash
git clone git@github.com:<you>/office-manager-bot.git tabelshchik
cd tabelshchik
cp .env.example .env
nano .env          # token, ADMIN_CHAT_ID, ADMIN_IDS, OpenAI key
docker compose up -d --build
docker compose logs -f
```

On first boot you should see, in order: the schema migrated to head, both offices seeded,
a first schedule generated for each, and the job list. That first-schedule step matters —
without it the bot would sit silent until Thursday's regeneration job.

---

## 6. Smoke test, before the group sees anything

Everything here is read-only or goes only to you.

```bash
docker compose exec tabelshchik tabelshchik validate
docker compose exec tabelshchik tabelshchik dry-run
docker compose exec tabelshchik tabelshchik preview --office ovest
```

Then in Telegram:

- DM the bot `/start` → it should greet you by name from the roster
- `/me` → your upcoming office days
- `/admin` → the admin menu
- **/admin → Офисы → O'Vest → ✉️ Тестовое напоминание** — renders the real reminder and
  sends it **to you only**. This is the one to look at closely: right people, right date,
  everyone tagged.
- @-mention the bot in the group → it should answer

---

## 7. The three checks that actually matter

Everything above proves it runs. These prove it is *reliable*, which is the entire point
of the rebuild. Do them before you rely on it.

### 7a. A reminder fires on time

Set the time a few minutes ahead, restart, wait:

```bash
sed -i 's/time: "15:30"/time: "14:05"/' config/app.yaml   # pick ~3 min from now
docker compose restart && docker compose logs -f
```

Put it back to `15:30` afterwards and restart again.

### 7b. A missed run is recovered

This is the failure the old bot had. Take the process down **across** a reminder time, so
the run is genuinely missed:

```bash
# with the reminder due at, say, 14:20
docker compose stop
# wait until 14:22
docker compose start && docker compose logs -f
```

You should see `replayed N missed job(s) on startup`, and the reminder arrives late
rather than never. The job ledger also means it will not double-send if it partly ran.

### 7c. The dead-man's switch alerts

Create a free check at [healthchecks.io](https://healthchecks.io), set period 20 minutes
and grace 10, then:

```yaml
# config/app.yaml
health:
  pingUrl: "https://hc-ping.com/your-uuid"
  pingIntervalMinutes: 15
```

```bash
docker compose restart
docker compose stop      # leave it down ~30 minutes
```

You should get an email. Start it again. This is the only layer that catches a VM that
never comes back — including one Oracle reclaimed.

---

## 8. Going live

Once 7a–7c pass, there is nothing else to switch on: the bot is already pointed at the
group. Worth doing on the first day:

- **/admin → ⚖️ Справедливость** — the surplus per person and the spread. On day one
  everything is zero; it becomes the number to watch.
- **/admin → Офисы → … → 🔄 Перегенерировать** — publishes the schedule workbook to the
  group, which is a good way to introduce the bot to people.
- Tell the team they can DM the bot `/vacation` to manage their own time off. Absences
  landing on an already-announced day are handled: the person is removed, a replacement
  is drafted, and the group gets a correction.

### Expect this: O'Vest's rotation does nothing yet

O'Vest has 11 eligible people and 11 Friday desks, so everyone goes every Friday and the
fairness spread is 0. That is correct — there is nothing to choose. The algorithm starts
mattering when desks are fewer than people. Pine is fixed-schedule only and never
rotates. Change either in **/admin → 🪑 Свободные места**.

---

## Day-to-day

```bash
docker compose logs -f --tail 100
git pull && docker compose up -d --build          # update
docker compose exec tabelshchik tabelshchik validate   # after editing config
docker compose restart                            # apply config changes
```

Config changes need a restart; roster changes made through `/admin` take effect
immediately.

Backups arrive nightly in your DM, and `/admin → 💾 Резервная копия` makes one on demand.
To restore: `docker compose stop`, drop the file in as `data/tabelshchik.db`, start again.

## If something goes wrong

| Symptom | Likely cause |
|---|---|
| No failure alerts, no backups | You never DM'd the bot, so it cannot message you |
| `/admin` does nothing | Your id is not in `ADMIN_IDS` — a non-admin gets silence by design |
| Reminders never arrive | Office `chatId` empty or wrong; `validate` will say |
| Bot ignores @mentions | Privacy mode is fine; check it is actually *in* the group |
| Pins fail in the log | Bot is not a group admin, or lacks "Pin messages" |
| "database is locked" | Two containers on one `data/` directory — run only one |
