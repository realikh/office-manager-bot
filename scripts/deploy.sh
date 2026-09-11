#!/usr/bin/env bash
#
# Deploy Табельщик. Runs on the VM as the forced command for the deploy SSH key, so
# whatever a client asks to run, sshd runs this and nothing else.
#
# Everything lives inside main() on purpose: this script updates the very repository it
# is stored in, and bash reads a script incrementally. Without the wrapper, git could
# swap the file out from under the interpreter mid-execution.

set -Eeuo pipefail

main() {
    local repo="${TABELSHCHIK_REPO:-$HOME/tabelshchik}"
    local container="tabelshchik"
    local wait_seconds="${DEPLOY_HEALTH_TIMEOUT:-300}"

    cd "$repo"

    # A reset would silently discard a hand-edited config file. Better to stop and make
    # someone look than to throw away a change nobody remembers making.
    if ! git diff --quiet HEAD 2>/dev/null; then
        fail "Рабочая копия на сервере изменена вручную." "$(git status --short | head -20)"
    fi

    local before after
    before="$(git rev-parse HEAD)"
    git fetch --quiet origin main
    git reset --hard --quiet origin/main
    after="$(git rev-parse HEAD)"

    if [[ "$before" == "$after" ]]; then
        echo "already at $(short "$after") — rebuilding anyway to pick up any image changes"
    else
        echo "deploying $(short "$before") -> $(short "$after")"
        git --no-pager log --oneline "$before..$after" | head -10 || true
    fi

    echo "building..."
    if ! docker compose up -d --build; then
        fail "Сборка не удалась." "$(docker compose logs --tail 40 2>&1 | tail -40)"
    fi

    echo "waiting for health (up to ${wait_seconds}s)..."
    if ! await_health "$container" "$wait_seconds"; then
        fail "Контейнер не стал healthy после деплоя $(short "$after")." \
             "$(docker compose logs --tail 40 2>&1 | tail -40)"
    fi

    echo "deployed $(short "$after") — healthy"
}

# Succeeds when the container reports healthy. Fails early on a crash loop: a container
# that is restarting or has exited will never become healthy, and waiting out the full
# timeout just delays the alert.
await_health() {
    local name="$1" deadline=$(( SECONDS + $2 )) status health

    while (( SECONDS < deadline )); do
        status="$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null || echo missing)"
        health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$name" 2>/dev/null || echo none)"

        case "$status:$health" in
            running:healthy) return 0 ;;
            running:none)    return 0 ;;   # no healthcheck defined; running is all we have
            restarting:*|exited:*|dead:*|missing:*)
                echo "container is $status — not waiting for the timeout"
                return 1
                ;;
        esac
        sleep 5
    done

    echo "timed out waiting for health (last status: ${status:-?}/${health:-?})"
    return 1
}

# Sent with curl straight to the Telegram API rather than through the bot, because the
# bot is precisely what has just failed. Never fails the script itself: losing the alert
# must not hide the deploy error underneath it.
alert() {
    local text="$1" token chat env_file="${TABELSHCHIK_REPO:-$HOME/tabelshchik}/.env"

    [[ -r "$env_file" ]] || return 0
    token="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$env_file" | cut -d= -f2- | tr -d '"'"'"' \r')"
    chat="$(grep -E '^ADMIN_CHAT_ID=' "$env_file" | cut -d= -f2- | tr -d '"'"'"' \r')"
    [[ -n "$token" && -n "$chat" ]] || return 0

    curl -sS --max-time 15 -o /dev/null \
        --data-urlencode "chat_id=${chat}" \
        --data-urlencode "text=${text}" \
        --data-urlencode "parse_mode=HTML" \
        "https://api.telegram.org/bot${token}/sendMessage" || true
}

fail() {
    local reason="$1" detail="${2:-}"
    echo "DEPLOY FAILED: $reason" >&2
    [[ -n "$detail" ]] && echo "$detail" >&2

    alert "$(printf '🚨 <b>Деплой не удался</b>\n%s\n\n<pre>%s</pre>' \
        "$reason" "$(printf '%s' "$detail" | tail -25 | escape_html)")"
    exit 1
}

escape_html() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
short() { printf '%.8s' "$1"; }

main "$@"
