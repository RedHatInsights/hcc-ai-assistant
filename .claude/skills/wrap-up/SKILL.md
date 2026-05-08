# Wrap-Up Skill

Consolidates post-merge bookkeeping into a single efficient operation, reducing 8+ sequential tool calls per merged PR into one script execution.

## Usage

```bash
/wrap-up RHCLOUD-12345
```

## What It Does

When triage detects a merged PR, this skill executes 8 operations in sequence:

1. **get_task_info** - Retrieve task details from memory server (status, PR info, metadata)
2. **jira_transition_issue** - Move JIRA ticket to "Release Pending"
3. **jira_add_comment** - Post merge confirmation and stage deploy note to JIRA
4. **archive_task** - Archive task in memory server
5. **slack_notify** - Send notification via memory server
6. **delete_remote_branch** - Delete remote bot branch using GitHub/GitLab API
7. **delete_local_branch** - Delete local bot branch with `git branch -D`
8. **bot_status_update** - Update bot status to `idle`

All operations use **fail-fast error handling**: if any operation fails, execution stops immediately.

## Configuration

Set these environment variables:

```bash
# JIRA Cloud (required)
export WRAP_UP_JIRA_TOKEN=your_api_token_here
export WRAP_UP_JIRA_EMAIL=your.email@redhat.com
export WRAP_UP_JIRA_URL=https://redhat.atlassian.net  # Optional, this is the default

# Memory Server (required)
export BOT_MEMORY_URL=https://memory-server.example.com

# GitHub (required for branch deletion)
export GITHUB_TOKEN=ghp_your_token_here  # Or GH_TOKEN

# Project configuration (optional)
export PROJECT_REPOS_PATH=/path/to/project-repos.json  # Default: ./project-repos.json
```

## Operations

### 1. Get Task Info (`get_task_info`)

Retrieves task details from the memory server:
- Task status
- PR information (URL, number, repository)
- Branch name
- Metadata

**API**: `GET {BOT_MEMORY_URL}/tasks/{jira_key}`

### 2. JIRA Transition (`jira_transition_issue`)

Moves the JIRA ticket to "Release Pending" status using JIRA Cloud API v3 with Basic authentication.

**API**: `POST {JIRA_URL}/rest/api/3/issue/{jira_key}/transitions`

### 3. JIRA Comment (`jira_add_comment`)

Posts a merge confirmation comment with stage deploy note using Atlassian Document Format (ADF).

**API**: `POST {JIRA_URL}/rest/api/3/issue/{jira_key}/comment`

### 4. Archive Task (`archive_task`)

Archives the task in the memory server.

**API**: `POST {BOT_MEMORY_URL}/tasks/{jira_key}/archive`

### 5. Slack Notification (`slack_notify`)

Sends a notification via the memory server's Slack integration.

**API**: `POST {BOT_MEMORY_URL}/notifications/slack`

### 6. Delete Remote Branch (`delete_remote_branch`)

Deletes the remote bot branch using GitHub API.

**API**: `DELETE {GITHUB_API}/repos/{owner}/{repo}/git/refs/heads/{branch}`

### 7. Delete Local Branch (`delete_local_branch`)

Deletes the local bot branch using `git branch -D`.

**Command**: `git branch -D {branch_name}`

### 8. Bot Status Update (`bot_status_update`)

Updates the bot status to `idle` in the memory server.

**API**: `POST {BOT_MEMORY_URL}/bot/status`

## Error Handling

All operations follow fail-fast behavior:
- If any operation fails, execution stops immediately
- Error messages include operation name and details
- No partial state changes (all-or-nothing per operation)

## Testing

The skill includes comprehensive tests:
- **Unit tests**: 24 tests for individual operations
- **Integration tests**: 12 tests for full workflow
- **Total**: 36 tests

Run tests:

```bash
cd .claude/skills/wrap-up
uv run pytest -v
```

## Implementation

The skill is implemented in Python 3.12+ with:
- **httpx** for HTTP API calls
- **fail-fast error handling** for reliability
- **comprehensive logging** for observability
- **dry-run mode** for safe testing
- **type hints** for code clarity

## Related

- Parent: RHCLOUD-46589
- See also: `/post-pr` skill for post-PR-creation bookkeeping
