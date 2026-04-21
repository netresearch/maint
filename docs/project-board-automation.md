# Project Board Automation

This document describes the setup for automatically adding issues and pull requests to the Netresearch TYPO3 project board.

## Overview

New issues and pull requests across Netresearch repositories are automatically added to the organization's project board for tracking and prioritization.

**Project Board:** https://github.com/orgs/netresearch/projects/4

## Components

### 1. GitHub Project Board

The organization uses GitHub Projects (new) for tracking work across repositories.

**Setup location:** https://github.com/orgs/netresearch/projects

### 2. GitHub App: Netresearch Project Bot

A GitHub App handles authentication for the add-to-project workflow. This is preferred over Personal Access Tokens (PATs) because:

- Tokens are generated on-demand (no expiration issues)
- Organization-owned (survives personnel changes)
- Fine-grained permissions
- Better audit trail

**App settings:** https://github.com/organizations/netresearch/settings/apps/netresearch-project-bot

#### App Configuration

| Setting | Value |
|---------|-------|
| App ID | `2513248` |
| Client ID | `Iv23li6IHvsVDnmsKOZ4` |
| Webhook | Disabled (not needed) |

#### Permissions

| Scope | Permission | Purpose |
|-------|------------|---------|
| Organization → Projects | Read and write | Add items to project board |
| Repository → Metadata | Read-only | Access repository information |
| Repository → Issues | Read-only | Read issue data |
| Repository → Pull requests | Read-only | Read PR data |

#### Installation

The app is installed for **all repositories** in the netresearch organization.

**Manage installation:** https://github.com/organizations/netresearch/settings/installations

### 3. Organization Secrets

The workflow uses organization-level secrets to authenticate:

| Secret | Description |
|--------|-------------|
| `PROJECT_APP_ID` | GitHub App ID (`2513248`) |
| `PROJECT_APP_PRIVATE_KEY` | GitHub App private key (PEM format) |

**Manage secrets:** https://github.com/organizations/netresearch/settings/secrets/actions

### 4. Repository Workflow

Each repository that should add issues/PRs to the project board needs the workflow file:

**File:** `.github/workflows/add-to-project.yml`

```yaml
name: Add issues to Netresearch TYPO3 board

on:
    issues:
        types: [opened]
    pull_request_target:
        types: [opened]

permissions:
    contents: read

jobs:
    add-to-project:
        name: Add issue or PR to project
        runs-on: ubuntu-latest

        steps:
            - name: Generate GitHub App token
              uses: actions/create-github-app-token@v1
              id: app-token
              with:
                  app-id: ${{ secrets.PROJECT_APP_ID }}
                  private-key: ${{ secrets.PROJECT_APP_PRIVATE_KEY }}
                  owner: netresearch

            - name: Add to project
              uses: actions/add-to-project@v1.0.2
              with:
                  project-url: https://github.com/orgs/netresearch/projects/4
                  github-token: ${{ steps.app-token.outputs.token }}
```

## Maintenance

### Regenerating the Private Key

If the private key is compromised or needs rotation:

1. Go to https://github.com/organizations/netresearch/settings/apps/netresearch-project-bot
2. Scroll to **Private keys**
3. Click **Generate a private key**
4. Download the new `.pem` file
5. Delete the old key
6. Update the `PROJECT_APP_PRIVATE_KEY` secret:
   - Go to https://github.com/organizations/netresearch/settings/secrets/actions
   - Edit `PROJECT_APP_PRIVATE_KEY`
   - Paste the entire contents of the new PEM file

### Adding the Workflow to a New Repository

1. Copy the workflow file above to `.github/workflows/add-to-project.yml`
2. Commit and push
3. The workflow will trigger on new issues and PRs

### Troubleshooting

#### "Bad credentials" error

- Check that `PROJECT_APP_ID` and `PROJECT_APP_PRIVATE_KEY` secrets exist
- Verify the private key hasn't been revoked
- Ensure the app is still installed on the repository

#### Issues/PRs not being added

- Verify the workflow file exists in the repository
- Check the Actions tab for workflow runs and errors
- Ensure the app has access to the repository (check installation settings)

#### "Resource not accessible by integration" error

- The app may not have the required permissions
- Go to app settings and verify Projects permission is "Read and write"
- Re-install the app if permissions were changed

## History

- **2025-12-21:** Migrated from PAT (`ADD_TO_PROJECT_PAT`) to GitHub App authentication
  - Created GitHub App: Netresearch Project Bot
  - Added organization secrets: `PROJECT_APP_ID`, `PROJECT_APP_PRIVATE_KEY`
  - Updated workflow in t3x-rte_ckeditor_image (PR #496)
