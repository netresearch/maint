# netresearch/maint

Organization maintenance and automation tasks for the Netresearch GitHub organization.

## Workflows

### Star Notifications

**File:** `.github/workflows/star-notifications.yml`

Monitors all public repositories in the netresearch organization for new stars and sends notifications to Matrix.

**Schedule:** Every 15 minutes

**Manual trigger:** Yes (via Actions tab → "Run workflow")

**Notifications sent to:** Matrix room via Hookshot webhook

#### Secrets Required

| Secret | Description |
|--------|-------------|
| `MATRIX_WEBHOOK_URL` | Matrix Hookshot webhook URL |

#### How It Works

1. Fetches all public repos in the org
2. Gets current stargazers for each repo
3. Compares with previously known stargazers (stored as artifact)
4. Sends Matrix notification for each new star
5. Updates state for next run

The first run indexes existing stars without sending notifications to avoid spam.

## Adding New Automation Tasks

1. Create workflow in `.github/workflows/`
2. Add scripts to `scripts/` if needed
3. Document in this README
