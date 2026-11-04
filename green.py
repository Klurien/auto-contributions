#!/usr/bin/env python3
"""Automated GitHub contribution bot.

Keeps a GitHub profile "green" by making real git commits + pushes to a
repository. It uses the git CLI only (no GitHub REST/GraphQL API) so
contributions are recorded the same way normal commits are.

Modes:
  init       Prepare the local repository and (optionally) create the remote.
  daily      Make today's contribution(s) and push.
  backfill   Fill in missing days over the last N days and push.
  status     Print repository / last-contribution status.
  schedule   Install a cron job that runs `daily` every day.

Environment / .env variables:
  REPO_DIR        Local clone path (default: this script's directory)
  REMOTE_URL      Git remote URL (default: auto from gh / origin)
  BRANCH          Branch to commit on (default: main)
  AUTHOR_NAME     Commit author name (default: git config user.name)
  AUTHOR_EMAIL    Commit author email (default: git config user.email)
  COMMITS_PER_DAY Number of commits per day (default: 1)
  MAX_BACKFILL    Default backfill window in days (default: 30)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def run(cmd, cwd=None, env=None, check=True, capture=True):
    """Run a command, return (rc, stdout, stderr)."""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    res = subprocess.run(
        cmd,
        cwd=cwd,
        env=full_env,
        capture_output=capture,
        text=True,
    )
    if check and res.returncode != 0:
        raise RuntimeError(
            f"Command failed ({res.returncode}): {' '.join(cmd)}\n"
            f"stdout: {res.stdout}\nstderr: {res.stderr}"
        )
    return res.returncode, res.stdout, res.stderr


def git_config(key, cwd=None):
    rc, out, _ = run(["git", "config", "--get", key], cwd=cwd, check=False)
    return out.strip() if rc == 0 else ""


def today_utc() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def commit_date_str(d: dt.date, hour: int = 12, minute: int = 0) -> str:
    return f"{d.isoformat()}T{hour:02d}:{minute:02d}:00+00:00"


def has_commit_on_date(repo_dir: Path, d: dt.date, author_email: str) -> bool:
    """Check whether any commit already exists on the given UTC date."""
    since = f"{d.isoformat()}T00:00:00+00:00"
    until = f"{d.isoformat()}T23:59:59+00:00"
    rc, out, _ = run(
        [
            "git",
            "log",
            "--all",
            f"--author={author_email}",
            f"--since={since}",
            f"--until={until}",
            "--format=%H",
        ],
        cwd=repo_dir,
        check=False,
    )
    return bool(out.strip())


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------


class GreenBot:
    def __init__(
        self,
        repo_dir: Path,
        branch: str,
        remote_url: str,
        author_name: str,
        author_email: str,
        commits_per_day: int,
    ):
        self.repo_dir = repo_dir
        self.branch = branch
        self.remote_url = remote_url
        self.author_name = author_name
        self.author_email = author_email
        self.commits_per_day = max(1, commits_per_day)

    # -- setup --------------------------------------------------------------
    def ensure_repo(self, create_remote: bool = False):
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        if not (self.repo_dir / ".git").exists():
            run(["git", "init", "-b", self.branch], cwd=self.repo_dir)
        # identity
        if self.author_name:
            run(["git", "config", "user.name", self.author_name], cwd=self.repo_dir)
        if self.author_email:
            run(["git", "config", "user.email", self.author_email], cwd=self.repo_dir)
        # remote
        rc, _, _ = run(
            ["git", "remote", "get-url", "origin"], cwd=self.repo_dir, check=False
        )
        if rc != 0:
            if not self.remote_url:
                if create_remote:
                    self._create_remote()
                else:
                    raise RuntimeError(
                        "No remote configured. Set REMOTE_URL or use --create."
                    )
            else:
                run(
                    ["git", "remote", "add", "origin", self.remote_url],
                    cwd=self.repo_dir,
                )
        if self.remote_url:
            run(
                ["git", "remote", "set-url", "origin", self.remote_url],
                cwd=self.repo_dir,
                check=False,
            )

        # seed files
        readme = self.repo_dir / "README.md"
        if not readme.exists():
            readme.write_text(
                "# Auto Contributions\n\n"
                "Automated daily contributions keeping this profile green.\n"
                "Generated by green.py.\n"
            )
        gitignore = self.repo_dir / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(".lock\n*.pyc\n__pycache__/\n")
        activity = self.repo_dir / "activity.log"
        if not activity.exists():
            activity.write_text("")

    def _create_remote(self):
        if not shutil.which("gh"):
            raise RuntimeError("gh CLI not found; cannot create remote.")
        name = self.repo_dir.name
        rc, out, err = run(
            [
                "gh",
                "repo",
                "create",
                name,
                "--public",
                "--source=.",
                "--remote=origin",
                "--push",
            ],
            cwd=self.repo_dir,
            check=False,
        )
        if rc != 0:
            # maybe already exists; try adding
            url = f"https://github.com/{self._gh_user()}/{name}.git"
            run(["git", "remote", "add", "origin", url], cwd=self.repo_dir, check=False)
            self.remote_url = url
        else:
            self.remote_url = f"https://github.com/{self._gh_user()}/{name}.git"

    def _gh_user(self) -> str:
        rc, out, _ = run(["gh", "api", "user", "-q", ".login"], check=False)
        return out.strip() if rc == 0 else "me"

    # -- commit helpers -----------------------------------------------------
    def _make_commit(self, d: dt.date, nonce: str = ""):
        activity = self.repo_dir / "activity.log"
        stamp = dt.datetime.now(dt.timezone.utc).isoformat()
        with activity.open("a") as f:
            f.write(f"{stamp} {d.isoformat()} {nonce or random.token_hex(4)}\n")
        run(["git", "add", "activity.log"], cwd=self.repo_dir)
        # ensure something staged
        rc, out, _ = run(
            ["git", "diff", "--cached", "--quiet"], cwd=self.repo_dir, check=False
        )
        if rc == 0:
            # nothing staged -> amend with empty commit
            pass
        date_str = commit_date_str(d)
        env = {
            "GIT_AUTHOR_NAME": self.author_name,
            "GIT_AUTHOR_EMAIL": self.author_email,
            "GIT_COMMITTER_NAME": self.author_name,
            "GIT_COMMITTER_EMAIL": self.author_email,
            "GIT_AUTHOR_DATE": date_str,
            "GIT_COMMITTER_DATE": date_str,
        }
        msg = f"chore: daily contribution {d.isoformat()}"
        # allow empty commit if nothing changed
        run(
            ["git", "commit", "--allow-empty", "-m", msg],
            cwd=self.repo_dir,
            env=env,
        )

    def daily(self, force: bool = False, push: bool = True) -> int:
        d = today_utc()
        made = 0
        if (not force) and has_commit_on_date(self.repo_dir, d, self.author_email):
            print(
                f"[daily] Already contributed on {d}; skipping. (use --force to add more)"
            )
        else:
            for i in range(self.commits_per_day):
                self._make_commit(d, nonce=f"daily-{i}")
                made += 1
            print(f"[daily] Made {made} commit(s) for {d}.")
        if push and made > 0:
            self.push()
        return made

    def backfill(self, days: int, per_day: int = None, push: bool = True) -> int:
        if per_day is None:
            per_day = self.commits_per_day
        per_day = max(1, per_day)
        end = today_utc()
        start = end - dt.timedelta(days=days)
        made = 0
        cur = start
        while cur <= end:
            if has_commit_on_date(self.repo_dir, cur, self.author_email):
                cur += dt.timedelta(days=1)
                continue
            for i in range(per_day):
                self._make_commit(cur, nonce=f"backfill-{i}")
                made += 1
            cur += dt.timedelta(days=1)
        print(f"[backfill] Made {made} commit(s) over {days} days.")
        if push and made > 0:
            self.push()
        return made

    def push(self):
        # ensure branch tracking
        run(["git", "branch", "-M", self.branch], cwd=self.repo_dir, check=False)
        rc, _, _ = run(
            ["git", "push", "-u", "origin", self.branch],
            cwd=self.repo_dir,
            check=False,
        )
        if rc != 0:
            # retry with force? no, try plain push
            rc2, out, err = run(
                ["git", "push", "origin", self.branch],
                cwd=self.repo_dir,
                check=False,
            )
            if rc2 != 0:
                raise RuntimeError(f"Push failed: {err or out}")
        print("[push] Pushed to origin.")

    def status(self):
        rc, out, _ = run(
            ["git", "log", "-1", "--format=%H %ad", "--date=iso"],
            cwd=self.repo_dir,
            check=False,
        )
        print("Repo:", self.repo_dir)
        print("Branch:", self.branch)
        print(
            "Remote:", self.remote_url or git_config("remote.origin.url", self.repo_dir)
        )
        print("Last commit:", out.strip() or "(none)")
        # count commits last 30 days
        since = (today_utc() - dt.timedelta(days=30)).isoformat()
        rc, out, _ = run(
            [
                "git",
                "log",
                "--all",
                f"--author={self.author_email}",
                f"--since={since}",
                "--format=%ad",
                "--date=short",
            ],
            cwd=self.repo_dir,
            check=False,
        )
        days = len(set(out.split()))
        print(f"Distinct contribution days (last 30): {days}")


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


def install_schedule(repo_dir: Path, python: str, script: Path):
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    service = unit_dir / "green-contributions.service"
    timer = unit_dir / "green-contributions.timer"
    service.write_text(
        "[Unit]\n"
        "Description=Daily GitHub contribution\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"WorkingDirectory={repo_dir}\n"
        f"ExecStart={python} {script} daily\n"
    )
    timer.write_text(
        "[Unit]\n"
        "Description=Run daily GitHub contribution\n"
        "\n"
        "[Timer]\n"
        "OnCalendar=*-*-* 09:00:00\n"
        "Persistent=true\n"
        "Unit=green-contributions.service\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    run(["systemctl", "--user", "daemon-reload"], check=False)
    run(
        ["systemctl", "--user", "enable", "--now", "green-contributions.timer"],
        check=False,
    )
    print("[schedule] Installed systemd user timer (daily 09:00).")
    print(f"  service: {service}")
    print(f"  timer:   {timer}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_bot(args) -> GreenBot:
    load_dotenv(Path(args.repo or ".").resolve() / ".env")
    load_dotenv(Path.cwd() / ".env")
    repo_dir = (
        Path(
            args.repo
            or os.environ.get("REPO_DIR", str(Path(__file__).resolve().parent))
        )
        .expanduser()
        .resolve()
    )
    branch = args.branch or os.environ.get("BRANCH", "main")
    remote_url = args.remote or os.environ.get("REMOTE_URL", "")
    author_name = os.environ.get("AUTHOR_NAME") or git_config("user.name")
    author_email = os.environ.get("AUTHOR_EMAIL") or git_config("user.email")
    commits_per_day = int(os.environ.get("COMMITS_PER_DAY", "1"))
    if args.commits_per_day:
        commits_per_day = args.commits_per_day
    return GreenBot(
        repo_dir, branch, remote_url, author_name, author_email, commits_per_day
    )


def main():
    parser = argparse.ArgumentParser(
        description="Automated GitHub contributor (git-based)."
    )
    parser.add_argument("--repo", help="Local repository path")
    parser.add_argument("--branch", default=None)
    parser.add_argument("--remote", default=None)
    parser.add_argument("--commits-per-day", type=int, default=None)

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Initialize repo + remote")
    p_init.add_argument(
        "--create", action="store_true", help="Create GitHub repo via gh"
    )

    p_daily = sub.add_parser("daily", help="Commit + push for today")
    p_daily.add_argument("--force", action="store_true")
    p_daily.add_argument("--no-push", action="store_true")

    p_back = sub.add_parser("backfill", help="Fill missing days")
    p_back.add_argument(
        "--days", type=int, default=int(os.environ.get("MAX_BACKFILL", "30"))
    )
    p_back.add_argument("--per-day", type=int, default=None)
    p_back.add_argument("--no-push", action="store_true")

    sub.add_parser("status", help="Show status")

    p_sched = sub.add_parser("schedule", help="Install cron job")
    p_sched.add_argument("--python", default=sys.executable)

    args = parser.parse_args()

    # re-parse daily flags (argparse quirk with shared parser) - handle manually
    bot = build_bot(args)

    if args.cmd == "init":
        bot.ensure_repo(create_remote=args.create)
        # initial commit if empty
        rc, out, _ = run(["git", "rev-parse", "HEAD"], cwd=bot.repo_dir, check=False)
        if rc != 0:
            bot._make_commit(today_utc(), nonce="init")
            bot.push()
        print("[init] Ready.")
    elif args.cmd == "daily":
        force = getattr(args, "force", False)
        push = not getattr(args, "no_push", False)
        bot.ensure_repo()
        bot.daily(force=force, push=push)
    elif args.cmd == "backfill":
        push = not getattr(args, "no_push", False)
        bot.ensure_repo()
        bot.backfill(days=args.days, per_day=args.per_day, push=push)
    elif args.cmd == "status":
        bot.status()
    elif args.cmd == "schedule":
        install_schedule(bot.repo_dir, args.python, Path(__file__).resolve())


if __name__ == "__main__":
    main()
