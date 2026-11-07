# GitHistory

A living, date-by-date archive of commits GitHub attributes to your account.

GitHistory discovers repositories accessible to your authenticated GitHub account, scans their **default branches**, and writes one Markdown page for each calendar day from your account creation date through your latest recorded authored commit. Empty days are documented too. Later scans add newly discovered commits without rebuilding the entire archive.

## What it does

- Reads your GitHub account creation date and accessible repositories.
- Scans historical commits GitHub resolves to your username.
- Creates `history/YYYY/MM/YYYY-MM-DD.md` daily records.
- Includes commit message, repository, authored timestamp, short SHA, and commit link.
- Saves scan state so later runs are incremental.
- Handles pagination, transient failures, primary rate limits, and secondary rate limits with backoff.
- Runs once, continuously as a worker, or hourly through GitHub Actions.

**Integrity rule:** GitHistory records real commits. It does not create fake commits, backdate activity, or manufacture contribution-graph events. Markdown file generation is an archive operation, not a way to increase GitHub contributions.

## GitHub Actions setup

1. Create a **classic personal access token** for the GitHub account you want to analyse. Give it the minimum repository access needed to read the repositories you want included (for private repositories, the appropriate `repo` access is required).
2. In this repository, open **Settings → Secrets and variables → Actions → New repository secret**.
3. Name the secret `GH_READ_TOKEN` and paste the token there. Never put the token in source code or commit it.
4. Under **Settings → Actions → General → Workflow permissions**, enable **Read and write permissions** so the workflow can save archive updates.
5. Open **Actions → GitHistory sync → Run workflow** for the first historical scan. After that, the workflow runs hourly.

The workflow uses `GH_READ_TOKEN` to read your account and repositories. GitHub Actions' own `GITHUB_TOKEN` is used to commit archive updates to this repository.

## Run locally

Requires Python 3.10+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export GITHUB_TOKEN="YOUR_PERSONAL_ACCESS_TOKEN"
export GITHUB_USERNAME="YOUR_GITHUB_LOGIN"
python githistory.py --once
```

Do not commit your token or put it in a public file.

## Run continuously

```bash
python githistory.py --loop
```

The default polling interval is one hour. You can change it:

```bash
python githistory.py --loop --interval 3600
```

The loop enforces a minimum 15-minute interval. When GitHub signals a rate limit, the scanner waits for the reset/retry window instead of hammering the API. Network and server errors use exponential backoff.

## Coverage and accuracy

- The initial version scans each accessible repository's **default branch**. Commits existing only on other branches are not included yet.
- GitHub's `author=<username>` filter only returns commits GitHub can associate with that account. Commits made with unlinked email addresses may not resolve to your username.
- The archive uses commit author timestamps. GitHub's contribution graph has separate eligibility rules, so archive commit totals and contribution totals are not interchangeable.
- Private repositories are included only when the read token has permission to access them.
- The initial full-history scan can take longer for repositories with very large histories. Subsequent scans are incremental.

## Project layout

```text
history/
  YYYY/
    MM/
      YYYY-MM-DD.md
.githistory/
  state.json
githistory.py
requirements.txt
```

## License

MIT
