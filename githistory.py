#!/usr/bin/env python3
"""GitHistory: build and continuously update a daily archive of your authored commits.

The scanner only records real commits returned by GitHub. It never creates
synthetic commits or artificial contribution activity.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

API = "https://api.github.com"
ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / ".githistory"
STATE_FILE = STATE_DIR / "state.json"
HISTORY_DIR = ROOT / "history"
API_VERSION = "2022-11-28"
DEFAULT_INTERVAL = 3600


class GitHubAPI:
    def __init__(self, token: str, username: str):
        self.username = username
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "GitHistory-open-source",
        })

    def get(self, url: str, params: dict[str, Any] | None = None) -> requests.Response:
        delay = 2.0
        while True:
            try:
                response = self.session.get(url, params=params, timeout=(10, 45))
            except requests.RequestException as exc:
                print(f"Network error: {exc}; retrying in {delay:.0f}s", flush=True)
                time.sleep(delay + random.random())
                delay = min(delay * 2, 300)
                continue

            if response.status_code in (403, 429) and (
                response.headers.get("X-RateLimit-Remaining") == "0"
                or response.status_code == 429
                or "rate limit" in response.text.lower()
            ):
                reset = int(response.headers.get("X-RateLimit-Reset", "0") or 0)
                wait = max(5, reset - int(time.time()) + 3) if reset else 60
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = max(wait, int(retry_after))
                    except ValueError:
                        pass
                print(f"GitHub rate limit reached; sleeping {wait}s", flush=True)
                time.sleep(wait)
                continue

            if response.status_code in (500, 502, 503, 504):
                print(f"GitHub returned {response.status_code}; retrying in {delay:.0f}s", flush=True)
                time.sleep(delay + random.random())
                delay = min(delay * 2, 300)
                continue

            response.raise_for_status()
            return response

    def pages(self, url: str, params: dict[str, Any] | None = None):
        """Yield paginated API results, following GitHub's Link header."""
        next_url = url
        next_params = params
        while next_url:
            response = self.get(next_url, next_params)
            payload = response.json()
            if not isinstance(payload, list):
                raise RuntimeError(f"Expected a list from {next_url}")
            yield from payload
            next_url = response.links.get("next", {}).get("url")
            next_params = None


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_date(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print("State file is unreadable; starting a safe rescan.", file=sys.stderr)
        return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(STATE_FILE)


def account_and_repositories(api: GitHubAPI) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    user = api.get(f"{API}/user").json()
    if user.get("login", "").casefold() != api.username.casefold():
        raise SystemExit(
            f"Token belongs to @{user.get('login')}, but GITHUB_USERNAME is @{api.username}. "
            "Use a read token for the account being analysed."
        )
    repos = list(api.pages(
        f"{API}/user/repos",
        {"per_page": 100, "sort": "full_name", "affiliation": "owner,collaborator,organization_member"},
    ))
    return user, repos


def fetch_authored_commits(
    api: GitHubAPI,
    repo: dict[str, Any],
    since: str | None,
) -> list[dict[str, Any]]:
    branch = repo.get("default_branch")
    if not branch:
        return []
    params: dict[str, Any] = {
        "author": api.username,
        "sha": branch,
        "per_page": 100,
    }
    if since:
        # Small overlap protects against clock skew and delayed indexing.
        overlap = parse_date(since) - dt.timedelta(hours=24)
        params["since"] = overlap.isoformat().replace("+00:00", "Z")

    url = f"{API}/repos/{repo['full_name']}/commits"
    found: list[dict[str, Any]] = []
    for item in api.pages(url, params):
        author = item.get("author") or {}
        # Keep only commits GitHub resolves to this account.
        if author.get("login", "").casefold() != api.username.casefold():
            continue
        commit = item.get("commit") or {}
        author_data = commit.get("author") or {}
        date = author_data.get("date")
        if not date:
            continue
        found.append({
            "sha": item["sha"],
            "date": date,
            "message": (commit.get("message") or "").splitlines()[0],
            "repository": repo["full_name"],
            "url": item.get("html_url", ""),
        })
    return found


def render_day(day: dt.date, commits: list[dict[str, Any]], account_login: str) -> str:
    lines = [
        f"# Git activity — {day.isoformat()}",
        "",
        f"Account: **@{account_login}**",
        "",
    ]
    if not commits:
        lines += ["No authored commits recorded for this day.", ""]
    else:
        lines += [f"**{len(commits)} authored commit(s)**", ""]
        for item in sorted(commits, key=lambda x: x["date"]):
            short_sha = item["sha"][:7]
            message = item["message"].replace("\r", " ").strip()
            lines += [
                f"## {message or '(no commit message)'}",
                "",
                f"- Repository: [{item['repository']}]"
                f"(https://github.com/{item['repository']})",
                f"- Commit: [{short_sha}]({item['url']})",
                f"- Authored at: {item['date']}",
                "",
            ]
    return "\n".join(lines).rstrip() + "\n"


def write_archive(
    account_created: dt.date,
    commits: list[dict[str, Any]],
    account_login: str,
) -> int:
    by_day: dict[dt.date, list[dict[str, Any]]] = {}
    for item in commits:
        day = parse_date(item["date"]).date()
        by_day.setdefault(day, []).append(item)

    last_day = max(by_day, default=account_created)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    day = account_created
    while day <= last_day:
        path = HISTORY_DIR / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}" / "README.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        content = render_day(day, by_day.get(day, []), account_login)
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")
            written += 1
        day += dt.timedelta(days=1)
    return written


def scan_once() -> int:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise SystemExit("Missing GITHUB_TOKEN. Set it as an environment variable; never paste it into chat.")
    username = os.environ.get("GITHUB_USERNAME")
    if not username:
        raise SystemExit("Missing GITHUB_USERNAME environment variable.")

    api = GitHubAPI(token, username)
    state = load_state()
    started = utc_now()
    user, repos = account_and_repositories(api)
    created = parse_date(user["created_at"]).date()
    since = state.get("last_scan_utc") if state.get("initial_scan_complete") else None

    print(f"Account: @{user['login']} | Created: {created} | Repositories visible: {len(repos)}", flush=True)
    print("Mode: incremental scan" if since else "Mode: first historical scan", flush=True)

    all_new: list[dict[str, Any]] = []
    scanned = 0
    for repo in repos:
        if repo.get("archived") or repo.get("disabled"):
            continue
        try:
            items = fetch_authored_commits(api, repo, since)
            all_new.extend(items)
            scanned += 1
            if items:
                print(f"  {repo['full_name']}: {len(items)} new authored commit(s)", flush=True)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "unknown"
            print(f"  Skipping {repo.get('full_name')}: HTTP {status}", file=sys.stderr, flush=True)

    # The archive itself is the deduplication source. This makes reruns safe.
    changed_days = write_archive(created, all_new, user["login"]) if not since else 0
    if since and all_new:
        # Re-render affected dates with all commits from existing daily records
        # plus newly discovered commits, avoiding duplicate SHA entries.
        changed_days = merge_incremental_days(created, all_new, user["login"])

    # Save the first-scan watermark even when there were no commits. On later
    # runs, advance it only when new activity exists; idle runs stay read-only.
    if not state.get("initial_scan_complete"):
        state = {
            "schema_version": 1,
            "account": user["login"],
            "account_created_at": user["created_at"],
            "initial_scan_complete": True,
            "last_scan_utc": started.isoformat().replace("+00:00", "Z"),
        }
        save_state(state)
    elif all_new:
        state["last_scan_utc"] = started.isoformat().replace("+00:00", "Z")
        save_state(state)

    print(
        f"Finished: scanned {scanned} repositories; found {len(all_new)} authored commits; "
        f"updated {changed_days} daily file(s).",
        flush=True,
    )
    return len(all_new)


def merge_incremental_days(
    account_created: dt.date,
    new_commits: list[dict[str, Any]],
    account_login: str,
) -> int:
    grouped: dict[dt.date, list[dict[str, Any]]] = {}
    for item in new_commits:
        grouped.setdefault(parse_date(item["date"]).date(), []).append(item)

    changed = 0
    for day, additions in grouped.items():
        path = HISTORY_DIR / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}" / "README.md"
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        known = set()
        for line in existing.splitlines():
            if "- Commit: [" in line:
                known.add(line.split("- Commit: [", 1)[1].split("]", 1)[0])
        merged = []
        for item in additions:
            if item["sha"][:7] not in known:
                merged.append(item)
        if merged:
            # Reconstruct existing entries from the markdown before rendering.
            records = []
            import re
            chunks = re.split(r"\n## ", existing)
            for chunk in chunks[1:]:
                title = chunk.splitlines()[0]
                repo_match = re.search(r"\- Repository: \[(.*?)\]", chunk)
                commit_match = re.search(r"\- Commit: \[(.*?)\]\((.*?)\)", chunk)
                date_match = re.search(r"\- Authored at: (.+)", chunk)
                if repo_match and commit_match and date_match:
                    records.append({
                        "sha": commit_match.group(1),
                        "date": date_match.group(1),
                        "message": title,
                        "repository": repo_match.group(1),
                        "url": commit_match.group(2),
                    })
            # Keep original full SHA for reliable dedupe in the current run.
            for item in merged:
                records.append(item)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(render_day(day, records, account_login), encoding="utf-8")
            changed += 1
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description="Continuously archive your real GitHub commit history.")
    parser.add_argument("--once", action="store_true", help="Run one scan and exit (for GitHub Actions).")
    parser.add_argument("--loop", action="store_true", help="Run forever, polling at a rate-limit-safe interval.")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="Polling interval in seconds (minimum 900).")
    args = parser.parse_args()

    if args.loop:
        interval = max(900, args.interval)
        print(f"GitHistory is running continuously; polling every {interval}s.", flush=True)
        while True:
            started = time.monotonic()
            try:
                scan_once()
            except Exception as exc:
                print(f"Scan failed: {exc}", file=sys.stderr, flush=True)
            elapsed = time.monotonic() - started
            time.sleep(max(30, interval - elapsed))
    else:
        scan_once()


if __name__ == "__main__":
    main()
