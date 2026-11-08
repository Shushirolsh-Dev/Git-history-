#!/usr/bin/env python3
"""Continuously discover and document public GitHub repositories not owned by this account.

There is no daily repository target or fixed discovery stop. The scanner partitions
GitHub's creation-date search space so the Search API's 1,000-result-per-query
ceiling does not hide older/newer repositories. Seen repository IDs are persisted
and skipped on later runs.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any

import requests

API = "https://api.github.com"
ROOT = Path(__file__).resolve().parent
OUT = ROOT / "research" / "public-repositories"
STATE = ROOT / ".githistory" / "research-state.json"
SKIP_OWNERS = {os.getenv("GITHUB_USERNAME", "").casefold()}


class GitHub:
    def __init__(self, token: str):
        self.s = requests.Session()
        self.s.headers.update({
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "GitHistory-public-repository-research",
        })
        self.last_search_request = 0.0
        self.last_core_request = 0.0

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        is_search = path.startswith("/search/")
        interval = 2.1 if is_search else 0.12
        attr = "last_search_request" if is_search else "last_core_request"
        elapsed = time.monotonic() - getattr(self, attr)
        if elapsed < interval:
            time.sleep(interval - elapsed)
        setattr(self, attr, time.monotonic())
        delay = 3
        while True:
            try:
                response = self.s.get(API + path, params=params, timeout=(10, 45))
            except requests.RequestException as exc:
                print(f"Network issue: {exc}; retry in {delay}s", flush=True)
                time.sleep(delay + random.random())
                delay = min(delay * 2, 180)
                continue
            if response.status_code in (403, 429):
                remaining = response.headers.get("X-RateLimit-Remaining")
                reset = int(response.headers.get("X-RateLimit-Reset", "0") or 0)
                if remaining == "0" or response.status_code == 429 or "rate limit" in response.text.lower():
                    wait = max(60, reset - int(time.time()) + 5) if reset else 60
                    wait = max(wait, int(response.headers.get("Retry-After", "0") or 0))
                    print(f"Rate limit: pausing {wait}s", flush=True)
                    time.sleep(wait)
                    continue
            if response.status_code in (500, 502, 503, 504):
                print(f"GitHub {response.status_code}; retry in {delay}s", flush=True)
                time.sleep(delay)
                delay = min(delay * 2, 180)
                continue
            response.raise_for_status()
            return response.json()


def load_state() -> dict[str, Any]:
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
        if not isinstance(state.get("seen"), list):
            state["seen"] = []
        return state
    except (OSError, json.JSONDecodeError):
        return {"seen": [], "day": "", "daily_count": 0}


def save_state(state: dict[str, Any]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, separators=(",", ":")) + "\n", encoding="utf-8")
    temporary.replace(STATE)


def clean(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_summary(readme: str) -> tuple[str, str]:
    if not readme:
        return "No README is available in the repository's default branch.", ""
    lines = readme.splitlines()
    title = ""
    for line in lines[:20]:
        match = re.match(r"^\s*#\s+(.+)", line)
        if match:
            title = clean(match.group(1))
            break
    paragraphs = []
    in_fence = False
    for line in lines:
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or line.lstrip().startswith(("#", "![", "<", "|", "- [ ]")):
            continue
        value = clean(line)
        if len(value) >= 45:
            paragraphs.append(value)
        if sum(map(len, paragraphs)) >= 1100:
            break
    return title or "Project overview", "\n\n".join(paragraphs)[:1800]


def make_doc(repo: dict[str, Any], upstream_readme: str) -> str:
    owner = repo.get("owner", {}).get("login", "unknown")
    name = repo.get("name", "repository")
    title, excerpt = extract_summary(upstream_readme)
    topics = repo.get("topics") or []
    language = repo.get("language") or "Not specified"
    description = repo.get("description") or "No repository description supplied."
    return f"""# {title}

> Independent public-repository research note generated by GitHistory.

## Repository
- **Project:** [{owner}/{name}]({repo.get('html_url', '')})
- **Owner:** [{owner}](https://github.com/{owner})
- **Primary language:** {language}
- **Stars:** {repo.get('stargazers_count', 0):,}
- **Forks:** {repo.get('forks_count', 0):,}
- **Open issues:** {repo.get('open_issues_count', 0):,}
- **Created:** {repo.get('created_at') or 'Not reported'}
- **Last pushed:** {repo.get('pushed_at') or 'Not reported'}
- **Topics:** {', '.join(topics) if topics else 'No topics listed'}
- **License:** {(repo.get('license') or {}).get('spdx_id', 'Not specified')}

## What the project says it does

{description}

## README overview

{excerpt or 'The upstream README did not contain enough prose for an overview. Inspect the source repository for details.'}

## Research notes

This entry is based on publicly available repository metadata and the upstream README. It is an initial discovery summary, not a code audit, security review, endorsement, or claim that the project is maintained. Verify the upstream repository for current details.

**Source:** {repo.get('html_url', '')}
"""


def date_ranges(start: dt.date, end: dt.date):
    """Yield inclusive date ranges, splitting crowded ranges until each is searchable."""
    pending = [(start, end)]
    while pending:
        first, last = pending.pop()
        query = f"is:public archived:false fork:false created:{first.isoformat()}..{last.isoformat()}"
        first_page = api.get("/search/repositories", {
            "q": query, "sort": "created", "order": "asc", "per_page": 100, "page": 1,
        })
        total = int(first_page.get("total_count", 0))
        if total > 1000 and first < last:
            midpoint = first + (last - first) // 2
            pending.append((midpoint + dt.timedelta(days=1), last))
            pending.append((first, midpoint))
            print(f"Partitioning {first}..{last} ({total:,} matches)", flush=True)
            continue
        if total > 1000:
            print(f"Warning: {first} still has {total:,} matches; GitHub's per-query cap may omit some same-day results.", flush=True)
        yield query, first_page, min(10, (total + 99) // 100)


def main() -> None:
    global api
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_READ_TOKEN")
    if not token:
        raise SystemExit("Set GH_READ_TOKEN as a secret.")
    username = os.getenv("GITHUB_USERNAME", "").casefold()
    if not username:
        raise SystemExit("GITHUB_USERNAME must identify the account whose repositories are excluded.")
    api = GitHub(token)
    state = load_state()
    today = dt.datetime.now(dt.timezone.utc).date()
    if state.get("day") != today.isoformat():
        state["day"], state["daily_count"] = today.isoformat(), 0
    seen = set(state.get("seen", []))
    batch_dir = OUT / f"{today:%Y}" / f"{today:%m}" / f"{today:%d}"
    batch_dir.mkdir(parents=True, exist_ok=True)
    index_path = batch_dir / "README.md"
    index_lines = [
        f"# Public GitHub repository research — {today.isoformat()}", "",
        "**Discovery mode:** Uncapped by a daily target. Public, non-fork, non-archived repositories owned by other accounts.", "",
        "Entries use public repository metadata and upstream README text; they are not code audits or endorsements.", "",
    ]
    generated = 0
    examined = 0
    for query, first_page, pages in date_ranges(dt.date(2008, 1, 1), today):
        for page in range(1, pages + 1):
            payload = first_page if page == 1 else api.get("/search/repositories", {
                "q": query, "sort": "created", "order": "asc", "per_page": 100, "page": page,
            })
            for repo in payload.get("items", []):
                examined += 1
                owner = (repo.get("owner") or {}).get("login", "")
                repo_id = repo.get("id")
                if not repo_id or owner.casefold() == username or owner.casefold() in SKIP_OWNERS or repo_id in seen:
                    continue
                full_name = repo["full_name"]
                try:
                    readme_data = api.get(f"/repos/{full_name}/readme")
                    encoded = readme_data.get("content", "")
                    upstream = base64.b64decode(encoded).decode("utf-8", errors="replace") if encoded else ""
                except requests.HTTPError as exc:
                    print(f"README unavailable for {full_name}: {exc}", flush=True)
                    upstream = ""
                slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{owner}--{repo['name']}")[:180]
                repo_dir = batch_dir / slug
                repo_dir.mkdir(parents=True, exist_ok=True)
                (repo_dir / "README.md").write_text(make_doc(repo, upstream), encoding="utf-8")
                seen.add(repo_id)
                generated += 1
                index_lines.append(f"- [{full_name}]({slug}/README.md) — {repo.get('description') or 'No description supplied.'}")
                if generated % 50 == 0:
                    state["seen"] = sorted(seen)
                    state["daily_count"] = int(state.get("daily_count", 0)) + 50
                    save_state(state)
                    index_path.write_text("\n".join(index_lines) + "\n", encoding="utf-8")
                    print(f"Documented {generated:,} new repositories; examined {examined:,} search results.", flush=True)
    state["seen"] = sorted(seen)
    state["daily_count"] = int(state.get("daily_count", 0)) + (generated % 50)
    save_state(state)
    index_path.write_text("\n".join(index_lines) + "\n", encoding="utf-8")
    print(f"Complete: documented {generated:,} new repositories; examined {examined:,} public repository search results.", flush=True)


if __name__ == "__main__":
    main()
