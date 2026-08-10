# netresearch/maint

Organization maintenance and automation tasks for the Netresearch GitHub organization.

## Workflows

### Star Notifications

**File:** `.github/workflows/star-notifications.yml`

Monitors all public repositories in the netresearch organization for new stars, forks, watchers, and dependents, and sends notifications to Matrix.

**Schedule:** Every 15 minutes

**Manual trigger:** Yes (via Actions tab → "Run workflow")

**Notifications sent to:** Matrix room via Hookshot webhook

#### Secrets Required

| Secret | Description |
|--------|-------------|
| `MATRIX_WEBHOOK_URL` | Matrix Hookshot webhook URL |

#### How It Works

1. Fetches all public repos in the org
2. Gets current stargazers, forks, watchers, and dependents for each repo
3. Compares with previously known data (stored as artifact)
4. Sends Matrix notification for each new star, fork, watcher, or dependent
5. Updates state for next run

The first run indexes existing data without sending notifications to avoid spam.

#### Notification Types

- **⭐ Stars:** When someone stars a repository
- **🍴 Forks:** When someone forks a repository
- **👀 Watchers:** When someone starts watching a repository
- **📦 Dependents:** When a new repository depends on one of our repositories (includes the dependent's star and fork count)

### Scheduled Failure Notifications

**File:** `.github/workflows/scheduled-failure-notifications.yml`

Watches every non-archived repository in the netresearch organization for failing **scheduled** workflow runs and reports them to Matrix. It exists because `netresearch.github.io`'s nightly `Build & Deploy` was red for 13 days and 14 consecutive runs before anyone noticed: GitHub's own notification for a failing scheduled run goes to whoever last edited the workflow file, which for a shared reusable workflow is frequently nobody who watches that repo. Scheduled runs are the org's early-warning system for dependency drift — they install fresh where PR runs sit on warm caches — so losing that signal is expensive.

**Schedule:** Daily at 07:00 UTC. The runs being watched are overwhelmingly nightly, so polling faster buys no detection speed and just multiplies API calls; 07:00 UTC lands after the usual nightly window and at the start of the CET working day.

**Manual trigger:** Yes, with a `dry_run` input that prints the notifications instead of posting them.

#### What it reports

It reports **transitions**, not states, so a workflow that is still red does not produce a message on every poll:

- **🔴 New failure** — a scheduled workflow went green → red.
- **🔁 Weekly reminder** — one message per week while it stays red, so a long failure cannot fade out silently.
- **🟢 Recovery** — red → green, briefly. A channel that only ever reports bad news gets muted.
- **📋 Baseline** — a single summary line on the first run, or when the state artifact has expired, listing everything already failing instead of re-announcing each as new.

Every failure message carries the repository, the workflow name, the number of consecutive failures, the date the streak started, and a link to the latest run — enough to triage without opening GitHub. Repositories with no scheduled runs are skipped and never reported.

Runs whose conclusion is `cancelled`, `skipped`, `neutral`, `stale` or `action_required` are ignored in both directions: they are verdicts on GitHub's plumbing rather than on the software, so counting them as red would generate noise and counting them as green would silently reset a real failure streak.

#### Retired workflows

A workflow that can no longer run is excluded, however red its last run is:

```
retired  :=  its id has no entry in GET /actions/workflows   (renamed or removed)
         OR  that entry's state is not "active"              (disabled)
```

Both halves are needed, and each catches a case in the organization today. `netresearch/ofelia`'s "Cleanup Container Images" is the first: a `netresearch/.github` template sync on 2026-04-19/20 **renamed** it to "Container Retention" at `.github/workflows/container-retention.yml`, which is alive and active — only the old identity's run history is frozen red. Expect more of these, because template syncs rename workflows across the fleet. `netresearch/claude-code-marketplace-P`'s "Pages" is the second: still listed, but `state: disabled_manually`. A disabled workflow **still appears** in `actions/workflows`, so the first half alone would miss it entirely. Retired states are `disabled_manually`, `disabled_inactivity` (GitHub's automatic pause after 60 days of repo inactivity) and `disabled_fork`.

Without this each would earn a weekly reminder forever about something nobody can action, which is how a notification channel earns a mute — taking the real signals with it.

The rule is keyed on **workflow id, not name**. An id follows the workflow file, so a sync that edits `name:` in place keeps the id and the entry is correctly kept — same workflow, still scheduled — whereas matching on name would retire a workflow that is running perfectly well. A sync that moves the file mints a new id and the old one legitimately disappears, which is the `ofelia` case.

There is deliberately **no age threshold**. A threshold gets it wrong in both directions: a monthly cron whose last run is 45 days old is perfectly alive and must still be reported, while a workflow retired yesterday is already dead. GitHub's own answer is the ground truth, and it is also what distinguishes a *retired* workflow from a merely *dormant* one.

Because an absent id means "retired", a workflow list that is wrong in the **short** direction would retire live workflows and silence their real failures — strictly worse than the noise being removed. So anything less than a confidently complete list is treated as "cannot determine" and every entry is kept: the request failing, the pages not adding up to `total_count` (`netresearch/.github` already has 63 workflows against a 100-item page, so the list is read to the end), or an empty list for a repo that demonstrably has scheduled runs.

The exclusion is never silent. It is listed in the run log on every cycle, named in the baseline message, and a workflow that was being actively reported as failing when it retires gets one closing `🗄` message rather than just ceasing to appear. If the workflow is ever re-added or re-enabled it runs again, becomes `active`, and is picked up by the next poll. The trade-off accepted: a repo GitHub auto-paused for inactivity stops being reported, which is correct in the narrow sense — it genuinely will not run again until someone touches the repo — but it does mean a dormant repo's broken nightly goes quiet.

#### State

Previous statuses live in a workflow **artifact** (`scheduled-failures-state`), downloaded at the start of the job and re-uploaded at the end — the same mechanism `star-notifications.yml` uses. A committed state file was rejected because it would put a commit on `main` on every run; the Actions cache was rejected because entries are evicted after 7 days without a read, which is shorter than the reminder interval this has to measure. The accepted cost of an artifact is that it can expire or be deleted, after which there is no history — handled by the baseline summary above rather than by re-announcing everything.

#### Secrets Required

| Secret | Description |
|--------|-------------|
| `SCHEDULED_FAILURES_PAT` | Fine-grained PAT on the `netresearch` org with `Metadata: read` and `Actions: read` on all repositories. The job's own `GITHUB_TOKEN` is scoped to this repository and cannot read another repository's workflow runs. |
| `MATRIX_WEBHOOK_URL` | Matrix Hookshot webhook URL (shared with Star Notifications) |

#### One-time setup

1. Create a fine-grained PAT at <https://github.com/settings/personal-access-tokens> for the `netresearch` organization, **All repositories**, with `Metadata: read` and `Actions: read`.
2. Add it as `SCHEDULED_FAILURES_PAT` under Settings → Secrets → Actions in this repo.
3. Run the workflow once manually with `dry_run` enabled to see what it would post, then once without to establish the baseline.

## Organization-Wide Automation

### Project Board Automation

Automatically adds new issues and pull requests to the Netresearch TYPO3 project board.

**Project Board:** https://github.com/orgs/netresearch/projects/4

**Documentation:** [docs/project-board-automation.md](docs/project-board-automation.md)

#### Quick Reference

| Component | Location |
|-----------|----------|
| GitHub App | [Netresearch Project Bot](https://github.com/organizations/netresearch/settings/apps/netresearch-project-bot) |
| Secrets | [Organization Secrets](https://github.com/organizations/netresearch/settings/secrets/actions) |
| Workflow | `.github/workflows/add-to-project.yml` (per repository) |

#### Secrets Required (Organization-level)

| Secret | Description |
|--------|-------------|
| `PROJECT_APP_ID` | GitHub App ID |
| `PROJECT_APP_PRIVATE_KEY` | GitHub App private key (PEM) |

### Impact Dashboard

**File:** `.github/workflows/impact-dashboard.yml`

Collects community-impact metrics for repositories listed in
[`config/dashboard-repos.yaml`](config/dashboard-repos.yaml), renders a static
dashboard, and publishes it to the `gh-pages` branch.

The config combines two mechanisms:

- **Patterns** auto-include every public repo (archived included) whose name matches
  a prefix/suffix or whose primary language matches a value (today: `t3x-*`,
  `*-skill`, language `Go`).
- **`include`** explicitly adds individual repos by name and assigns them to a
  category. This is how the Commerce, Ansible, and Tools categories are
  populated. Add or remove entries here in a PR — the workflow picks them up
  on the next run.

**Schedule:** Daily at 03:00 UTC

**Manual trigger:** Yes (via Actions tab → "Run workflow")

**URL:** `https://netresearch.github.io/maint/` (once GitHub Pages is enabled on the `gh-pages` branch).

#### What gets collected

Per repo — lifetime and last 30 days where meaningful:

- Metadata: language, license, topics, homepage, created/updated timestamps
- Stars, forks, watchers, network count
- Issues (open / closed, opened in 30d)
- Pull requests (open / merged / closed-unmerged, opened and merged in 30d)
- Releases (count, latest release, total asset downloads)
- Contributors (total, external = not in public org members, top 10)
- Commits (lifetime on default branch, last 30 days)
- Packagist downloads (total / monthly / daily) for PHP repos with a `composer.json`
- GHCR container pulls (lifetime total + last 30 days, exact numbers) — scraped from the package page (`<h3 title="N">` and the 30-day sparkline's `data-merge-count` bars)
- Dependents count ("Used by N repositories / N packages") — scraped from `/network/dependents`
- Traffic (clones, views, top referrers, top paths) — **last 14 days only**, requires PAT

Aggregate totals and 90 days of daily snapshots feed the time-series charts.

#### Scraped (no stable API)

Two metrics rely on HTML scraping of `github.com` — they work today but may break if GitHub changes the DOM:

- **GHCR pulls** — `github.com/<org>/<repo>/pkgs/container/<pkg>` exposes the exact count in a `title` attribute and the 30-day bars as `data-merge-count`. Package name is discovered via `GET /orgs/<org>/packages` (requires `read:packages`) or falls back to assuming `package == repo` name.
- **Dependents** — `github.com/<org>/<repo>/network/dependents` shows `"N Repositories"` / `"N Packages"` under toggles filtered by `dependent_type`.

Not collected:

- **TER downloads (extensions.typo3.org)** — no stable public API; Packagist stats are a reasonable proxy for TYPO3 extensions installed via composer.

#### Secrets

| Secret | Required | Description |
|--------|----------|-------------|
| `GITHUB_TOKEN` | Automatic | Provided by Actions; covers all core metrics. |
| `IMPACT_DASHBOARD_PAT` | Optional | Fine-grained PAT on the `netresearch` org with `Administration: Read` (for traffic) and optionally `Packages: Read` (to enumerate GHCR packages exactly instead of falling back to name-based probing). Or a classic PAT with `repo` + `read:packages` scopes. Without this secret, traffic is omitted; GHCR still works for repos whose container package name matches the repo name. |

#### One-time setup

1. **Add the PAT secret** (optional but recommended for traffic):
   - Create a PAT at <https://github.com/settings/tokens> with `repo` scope, or a fine-grained token on the `netresearch` org with `Administration: Read` on the relevant repos.
   - Add as `IMPACT_DASHBOARD_PAT` under Settings → Secrets → Actions in this repo.
2. **Run the workflow once manually** (Actions tab → Impact Dashboard → Run workflow). This creates the `gh-pages` branch and the first snapshot.
3. **Enable GitHub Pages**: Settings → Pages → Source = "Deploy from a branch", Branch = `gh-pages`, Path = `/ (root)`.
4. **Lifetime traffic**: GitHub's traffic API only returns the trailing 14 days. A lifetime total is therefore only as old as the first successful run — the pipeline accumulates it via daily snapshots going forward.

#### How the "blast radius" score is computed

A coarse single-number community-participation indicator, per repo:

```
blast_radius = external_contributors × 3
             + total_issues
             + prs_merged
             + forks × 2
             + dependents_repos × 2
```

Weighted toward outside-the-org involvement. Compare repos relative to each other; absolute values are not meaningful on their own.

## Adding New Automation Tasks

1. Create workflow in `.github/workflows/`
2. Add scripts to `scripts/` if needed
3. Document in this README
4. For detailed setup guides, add to `docs/`

### Shared GitHub client

Anything in `scripts/` that talks to the GitHub API should go through
[`scripts/github_api.py`](scripts/github_api.py) rather than rolling its own
`requests.get`. It carries the rate-limit policy — a 60-second floor for
secondary limits, `Retry-After` and `x-ratelimit-reset` handling, and a
600-second per-run sleep budget — that was paid for in broken scheduled runs. A
second, friendlier-looking implementation would simply re-earn those failures.

Scripts are run as `python scripts/<name>.py`, which puts `scripts/` on
`sys.path`; that is what makes `import github_api` work.

### Tests

`tests/` runs under pytest (`python -m pytest tests/`) and each file is also
executable on its own (`python tests/test_github_api.py`) for a quick check
without pytest installed.

> Note: nothing in CI currently runs these tests.
