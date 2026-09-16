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

## 2. Check the configuration locally

```bash
uv run tabelshchik validate
```

This parses `app.yaml` and `messages.yaml` and touches nothing else. There are no offices
to check: a new deployment has none at all, and you create them from the bot in step 5.

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

## 3.5. Create the VM

Skip if you already have one with a **public IP** and **Ubuntu 24.04**. Two settings are
easy to get wrong and both mean starting over, so they are called out below.

In the Oracle Cloud console: **☰ → Compute → Instances → Create instance**.

**Name** — anything, e.g. `tabelshchik`.

**Image and shape** → *Edit*:

- **Image** → *Change image* → **Canonical Ubuntu** → **24.04**. The console defaults to
  Oracle Linux; the commands below assume Ubuntu, so change this deliberately.
- **Shape** → *Change shape* → **Ampere** → `VM.Standard.A1.Flex` → 1 OCPU, 6 GB.
  Check it is labelled **Always Free eligible**.
- If that fails later with **"Out of host capacity"** — common for Ampere — go back and
  pick **AMD** → `VM.Standard.E2.1.Micro` instead. It is 1/8 OCPU and 1 GB, which is
  still ample: this bot idles under 200 MB. Try Ampere again another day if you like.

**Networking** — the setting people miss:

- Let it create a new VCN and subnet if you have none.
- **Subnet** must be a **public subnet**.
- **Assign a public IPv4 address** must be **Yes**. Without it there is nothing to SSH
  to, and it cannot be added afterwards if the subnet is private.

**Add SSH keys** → choose **Paste public keys** and paste your own, rather than letting
Oracle generate one. Then `ssh` finds the key by default and there is no extra file to
keep track of:

```bash
cat ~/.ssh/id_ed25519.pub     # if this errors, run: ssh-keygen -t ed25519
```

**Boot volume** — leave the defaults. 50 GB is well inside the free allowance.

Then **Create**. It is `PROVISIONING` for a minute, then `RUNNING`, and the **Public IP
address** appears on the instance page.

### Connect

```bash
ssh ubuntu@<PUBLIC_IP>
```

If it times out rather than being refused, the VCN security list is missing ingress on
TCP 22 — default VCNs have it, custom ones may not.

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

## 6. Create your first office

The database starts empty — there are no offices until you make one, and nothing is read
from a file. All of this happens in Telegram.

The first boot turns the ids in `ADMIN_IDS` into admins, the lowest of them the **owner**.
From then on that variable does nothing: adminship is managed from the bot, which is what
lets you hand it over later and stop being an admin yourself.

1. DM the bot `/start`, then `/admin`. The slash-command menu is published automatically,
   so typing `/` shows what is available — admins see `/admin`, nobody else does. There is
   nothing to set in @BotFather.
2. **Офисы → ➕ Создать офис**, and send the name. You get an id back.
3. In the office's Telegram group, send **`/bind`** and pick the office. This is how it
   learns the chat id — that id is shown nowhere in the Telegram interface and cannot be
   typed from memory. Two offices may share one group; messages then carry an office
   header so they stay distinguishable.
4. **Офисы → your office → 👥 Сотрудники → ➕ Добавить** for each person. A Telegram handle
   is optional — anyone can link themselves later with `/start`.
5. **🪑 Свободные места** for each weekday, and **📌 Фикс. расписание** for anyone who is
   always in on a given day. A schedule is generated as soon as the first person exists.

To give somebody else adminship: **/admin → 👑 Администраторы → ➕ Добавить**. Handing over
ownership is two steps on purpose — grant, then transfer — after which you can remove
yourself.

---

## 6.5. Smoke test, before the group sees anything

Everything here is read-only or goes only to you.

```bash
docker compose exec tabelshchik tabelshchik validate
docker compose exec tabelshchik tabelshchik dry-run
docker compose exec tabelshchik tabelshchik preview --office <your-office-id>
```

Then in Telegram:

- `/me` → your upcoming office days
- **/admin → Офисы → your office → ✉️ Тестовое напоминание** — renders the real reminder
  and sends it **to you only**. This is the one to look at closely: right people, right
  date, everyone tagged.
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
- **/admin → Офисы → … → 🔄 Перегенерировать** — sends you the schedule workbook. Then
  **/admin → 📨 Отправить сообщение**, forward the workbook to the bot and pick the office:
  it lands in the group as the bot's own post, which is a good way to introduce the bot.
- Tell the team they can DM the bot `/vacation` to manage their own time off. Absences
  landing on an already-announced day are handled: the person is removed, a replacement
  is drafted, and the group gets a correction.

### Expect this: O'Vest's rotation does nothing yet

O'Vest has 11 eligible people and 11 Friday desks, so everyone goes every Friday and the
fairness spread is 0. That is correct — there is nothing to choose. The algorithm starts
mattering when desks are fewer than people. Pine is fixed-schedule only and never
rotates. Change either in **/admin → 🪑 Свободные места**.

---

## Automatic deploys

Once set up, a push to `main` deploys itself — but only if CI is green, and a deploy that
comes up broken sends a Telegram alert and is left down rather than quietly failing.

### One-time setup

**1. A dedicated key**, not your personal one, so its blast radius is a single command on
a single machine:

```bash
ssh-keygen -t ed25519 -N "" -C "tabelshchik-deploy" -f ~/.ssh/tabelshchik-deploy
```

**2. Install the public half on the VM**, locked to the deploy script:

```bash
printf 'command="%s",no-pty,no-agent-forwarding,no-port-forwarding,no-X11-forwarding %s\n' \
  "/home/ubuntu/tabelshchik/scripts/deploy.sh" "$(cat ~/.ssh/tabelshchik-deploy.pub)" \
  | ssh tabelshchik 'cat >> ~/.ssh/authorized_keys'
```

The forced command is the point: whatever a client asks to run, sshd runs the deploy
script instead. A leaked key can trigger a deploy and nothing else — no shell, no file
access, no port forwarding.

**3. Four repository secrets** at *Settings → Secrets and variables → Actions*:

| Secret | Value |
|---|---|
| `DEPLOY_SSH_KEY` | the **private** half — `pbcopy < ~/.ssh/tabelshchik-deploy` |
| `DEPLOY_HOST` | the VM's public IP |
| `DEPLOY_USER` | `ubuntu` |
| `DEPLOY_KNOWN_HOSTS` | `ssh-keyscan <IP>` output |

`DEPLOY_KNOWN_HOSTS` pins the host key. Without it the deploy would have to accept
whatever machine answers on that address.

### What happens on a push

`check` runs lint, types, the architecture contracts, migration drift and the tests. Only
if it passes does `deploy` connect, and then the VM:

1. refuses to continue if its working tree was edited by hand — a reset would silently
   discard it;
2. resets to `origin/main` and rebuilds;
3. waits for the container to report **healthy**, giving up early if it is restarting or
   has exited, because a crash loop will never become healthy;
4. on any failure, sends the last log lines to the admin chat and exits non-zero, turning
   the Actions run red.

That alert goes to Telegram **by curl, not through the bot** — the bot is precisely what
has just failed.

### What "healthy" means

The bot touches `/data/heartbeat` from its own event loop every 30 seconds, and the
container healthcheck fails if that file goes stale. So healthy means the loop is
actually turning — not merely that the process started, which is all the old check
(running `validate` in a second process) ever proved.

```bash
ssh tabelshchik 'docker inspect -f "{{.State.Health.Status}}" tabelshchik'
```

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

The database lives in a Docker named volume rather than a directory you can `ls`, because
the container runs as an unprivileged uid that would not match a host directory's owner.
To restore a backup:

```bash
docker compose stop
docker compose cp tabelshchik.db tabelshchik:/data/tabelshchik.db
docker compose start
```

**`docker compose down -v` deletes that volume**, and with it the schedule, the fairness
ledger and every absence people have entered. Plain `down`, `restart` and
`up -d --build` all leave it alone.

## If something goes wrong

| Symptom | Likely cause |
|---|---|
| No failure alerts, no backups | You never DM'd the bot, so it cannot message you |
| `/admin` does nothing | Your id is not in `ADMIN_IDS` — a non-admin gets silence by design |
| Reminders never arrive | Office `chatId` empty or wrong; `validate` will say |
| Bot ignores @mentions | Privacy mode is fine; check it is actually *in* the group |
| Pins fail in the log | Bot is not a group admin, or lacks "Pin messages" |
| "database is locked" | Two containers on one `data/` directory — run only one |
