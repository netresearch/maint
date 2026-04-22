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

- **Patterns** auto-include every non-archived public repo whose name matches
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
