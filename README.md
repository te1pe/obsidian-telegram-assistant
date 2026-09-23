# obsidian-telegram-assistant

A Telegram bot that turns a message into an Obsidian note in a second, and
"/e завтра 15:00 90 Созвон" into a macOS Calendar event.

Written for myself, runs every day. No third-party libraries: just Python and
launchd.

The bot parses Russian input — dates, weekdays and recurrence are written in
Russian, so the commands below are shown as they are actually typed.

## What it does

| You send the bot | You get | Latency |
|---|---|---|
| any text | a note `inbox/YYYY-MM-DD-HHMMSS.md` with frontmatter | 1–2 seconds |
| `/e завтра 15:00 90 Созвон` | an event in Calendar | 1–2 seconds |
| `/r пт 19:00 Позвонить маме` | a reminder in Reminders | 1–2 seconds |
| `/e будни 09:00 15 Планирование` | a recurring event — after confirmation | |
| `/stats` | how many notes have piled up, when the last sync ran | |

Events and reminders can also be created with buttons, without commands.

## Installation

In detail — see [docs/setup.md](docs/setup.md). In short:

```bash
cp src/*.py scripts/*.sh ~/bin/          # scripts
./runner/build.sh                        # launcher + code signing
cp launchd/*.example ~/Library/LaunchAgents/   # schedule, then fill in the token
```

Requires macOS 13+, the system Python 3.9 and the Command Line Tools.
The Obsidian vault can live anywhere — the path is set by the `VAULT` variable.

---

## What it is made of

```
src/tg-inbox.py        the bot: receiving messages, notes, events, reminders
src/shrink-images.py   one-off compression of vault images to WebP
scripts/vault-sync.sh  commits and pushes the vault to git every half hour
scripts/add-event.sh   create an event from the terminal
runner/brain-runner.c  launcher in C: the holder of the macOS permissions
launchd/               example plists
templates/CLAUDE.md    note-parsing rules for Claude Code
docs/                  installation and architecture
```
