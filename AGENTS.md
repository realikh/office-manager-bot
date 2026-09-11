# Agent instructions

The brief for this repository lives in **[CLAUDE.md](CLAUDE.md)** — commands, the layering
contracts, conventions, and the traps that have cost real time here.

It is kept in one file on purpose rather than duplicated per tool: two copies of the same
instructions drift, and the half that is wrong is the half you will read.

Deeper notes, all linked from there:

| Document | When you need it |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Before changing the solver, the ledger or retention |
| [docs/ai-voice.md](docs/ai-voice.md) | Before changing a prompt, a guard or `config/messages.yaml` |
| [docs/configuration.md](docs/configuration.md) | Before adding a setting or editing an office file |
| [docs/testing.md](docs/testing.md) | Before adding tests, or when one you did not expect breaks |
| [docs/deploy.md](docs/deploy.md) | The VM, Docker, and the automated deploy |

Two rules worth repeating outside any file:

- **Run `uv run pytest && uv run mypy && uv run ruff check . && uv run lint-imports`**
  before calling work done. All four. CI runs the same set.
- **Never commit secrets.** The bot token, admin chat id and OpenAI key live in `.env` on
  the server and in GitHub Actions secrets. They are not in this repository and must not
  enter it.
