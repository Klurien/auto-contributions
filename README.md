# Auto Contributions (Green Profile Bot)

A small Python tool that keeps your GitHub contribution graph green by making
**real git commits** and pushing them to a repository. It uses the `git` CLI
only — no GitHub REST/GraphQL API calls for the profile.

## Features

- `init` – initialise the local repo, wire up the remote, seed files.
- `daily` – make today's contribution(s) and push (idempotent).
- `backfill` – fill in missing days over the last N days.
- `status` – show last commit and distinct contribution days.
- `schedule` – install a daily cron job.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
cp .env.example .env   # edit values
python3 green.py init
```

## Usage

```bash
python3 green.py daily              # today
python3 green.py daily --force     # add more commits today
python3 green.py backfill --days 90 # fill last 90 days
python3 green.py status
python3 green.py schedule          # daily 09:00 cron
```

## Notes

- Contributions are only counted when the commit author email matches a
  verified email on your GitHub account.
- Back-dating uses `GIT_AUTHOR_DATE`/`GIT_COMMITTER_DATE`.
- Run via cron/systemd for "always green".