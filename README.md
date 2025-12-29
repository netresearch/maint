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

## Adding New Automation Tasks

1. Create workflow in `.github/workflows/`
2. Add scripts to `scripts/` if needed
3. Document in this README
4. For detailed setup guides, add to `docs/`
