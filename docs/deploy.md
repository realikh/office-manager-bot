# Deploying Табельщик

The bot uses **long polling**, so there is no public URL, no TLS certificate and no
inbound firewall rule to get wrong. Anything that can run Docker and stay on will do.

## Oracle Cloud Always Free

Still genuinely free, with strings that are worth knowing before you rely on it.

### 1. Create the instance

Sign up at [cloud.oracle.com](https://cloud.oracle.com) (a card is needed for identity
verification only). Create a Compute instance:

- **Shape**: `VM.Standard.A1.Flex` with 1 OCPU / 6 GB, or `VM.Standard.E2.1.Micro` if
  Ampere capacity is unavailable in your region. Either is far more than this bot needs.
- **Image**: Ubuntu 24.04
- **SSH key**: add your public key

Oracle halved the Always Free Ampere allocation to 2 OCPU / 12 GB in 2026. It makes no
practical difference here — the bot idles at well under 200 MB.

### 2. Idle reclamation — read this part

Oracle reclaims Always Free compute it classifies as **idle**: 95th-percentile CPU below
10% *and* network below 10% over a continuous 7-day window (plus memory below 10% on A1
shapes). A bot that sleeps between crons fits that description exactly, so the instance is
a reclamation candidate.

Three mitigations, best first:

1. **Upgrade the tenancy to Pay As You Go and stay inside the Always Free limits.** The
   bill stays $0 and idle reclamation stops applying — it is a policy for Always Free
   *accounts*, not for Always Free *resources* in an upgraded account. This is the clean
   fix and it costs nothing.
2. Long polling holds a continuous outbound connection, which helps the network metric,
   but do not rely on it alone.
3. Set `health.pingUrl` (below). If the instance *is* reclaimed you find out in minutes
   instead of through a week of missing reminders.

The honest summary: free hosting has strings, and this is the string.

### 3. Install Docker

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl git
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER" && newgrp docker
sudo systemctl enable --now docker    # this is what survives a reboot
```

The `systemctl enable` matters: without it `restart: unless-stopped` restores the
container after a crash but not after a host reboot.

### 4. Deploy

```bash
git clone <your-repo> tabelshchik && cd tabelshchik
cp .env.example .env && nano .env          # token, admin id, OpenAI key
nano config/offices/ovest.yaml             # fill in chatId for each office
docker compose up -d
docker compose logs -f
```

Add the bot to each office group, make it an administrator if you want it to pin
messages, and get the chat id from [@userinfobot](https://t.me/userinfobot) — group ids
are negative and usually start `-100`.

### 5. Dead-man's switch

Create a free check at [healthchecks.io](https://healthchecks.io), then in
`config/app.yaml`:

```yaml
health:
  pingUrl: "https://hc-ping.com/your-uuid-here"
  pingIntervalMinutes: 15
```

Set the check's period to 20 minutes with a 10-minute grace. If the bot stops pinging you
get an email — this is the only layer that catches a machine that never comes back.

## Verifying a deployment

Do this against a **test bot token and a test group** before pointing it at real chats.

```bash
docker compose exec tabelshchik tabelshchik validate
docker compose exec tabelshchik tabelshchik dry-run
docker compose exec tabelshchik tabelshchik preview --office ovest
```

Then check the three things that actually matter:

1. **A reminder fires.** Set `reminders.attendance.time` a few minutes ahead, restart,
   and wait.
2. **The catch-up sweep works.** `docker compose restart` ten minutes before a reminder
   is due, so the process is down across the fire time. It should go out on startup,
   logged as replayed.
3. **The dead-man's switch works.** `docker compose stop` for longer than the grace
   period and confirm the alert arrives.

## Day-to-day

```bash
docker compose logs -f --tail 100
docker compose pull && docker compose up -d --build     # update
docker compose exec tabelshchik tabelshchik validate     # after editing config
docker compose restart                                   # apply config changes
```

Backups arrive nightly in the admin chat, and `/admin → Резервная копия` produces one on
demand. To restore, stop the bot, drop the file in as `data/tabelshchik.db`, start again.

## Other hosts

Nothing here is Oracle-specific — any always-on Linux box with Docker works: a home
server, a NAS, a cheap VPS. The one requirement is that the machine stays on, because a
bot that is asleep at 15:30 sends nothing. The catch-up sweep covers short outages, not
a machine that is off every night.

Serverless platforms are a poor fit for a different reason: Cloudflare Workers' free
plan caps CPU at 10 ms per invocation, which is not enough to build a styled workbook.
