# Wrap-Up Skill

Consolidates post-merge bookkeeping into a single efficient operation, reducing 8+ sequential tool calls per merged PR into one script execution.

## Quick Start

```bash
# Install dependencies with uv
uv sync

# Run tests
uv run pytest -v

# Run with coverage
uv run pytest --cov=scripts --cov-report=html -v

# Execute workflow
uv run python scripts/wrap_up_operations.py RHCLOUD-12345
```

## What It Does

When triage detects a merged PR, this skill executes 8 operations in sequence:

1. **get_task_info** - Retrieve task details from memory server (status, PR info, metadata)
2. **jira_transition_issue** - Move JIRA ticket to "Release Pending" (JIRA Cloud API v3)
3. **jira_add_comment** - Post merge confirmation and stage deploy note to JIRA (ADF format)
4. **archive_task** - Archive task in memory server
5. **slack_notify** - Send notification via memory server
6. **delete_remote_branch** - Delete remote bot branch using GitHub API
7. **delete_local_branch** - Delete local bot branch with `git branch -D`
8. **bot_status_update** - Update bot status to `idle`

All operations use **fail-fast error handling**: if any operation fails, execution stops immediately.

## Usage

### From Claude Code

```bash
/wrap-up RHCLOUD-12345
```

### From Command Line

```bash
# Basic usage
python scripts/wrap_up_operations.py RHCLOUD-12345

# With options
uv run python scripts/wrap_up_operations.py RHCLOUD-12345 \
  --jira-url=https://custom-jira.example.com \
  --skip=slack_notify \
  --dry-run

# JSON output
uv run python scripts/wrap_up_operations.py RHCLOUD-12345 --json
```

### From Python

```python
from scripts.wrap_up_operations import execute_wrap_up_workflow

result = execute_wrap_up_workflow(
    jira_key="RHCLOUD-12345",
    jira_url=None,  # Falls back to WRAP_UP_JIRA_URL env var
    jira_token=None,  # Falls back to WRAP_UP_JIRA_TOKEN env var
    jira_email=None,  # Falls back to WRAP_UP_JIRA_EMAIL env var
    memory_url=None,  # Falls back to BOT_MEMORY_URL env var
    github_token=None,  # Falls back to GITHUB_TOKEN or GH_TOKEN env var
    skip_operations=[],
    dry_run=False,
)

if result.success:
    print("✓ All operations completed successfully")
    for op in result.operations:
        print(f"  {op.operation}: {op.message}")
else:
    print("✗ Workflow failed")
    for op in result.operations:
        if op.status.value == "failed":
            print(f"  {op.operation}: {op.message}")
```

## Configuration

Set these environment variables for API integrations:

```bash
# JIRA Cloud (required)
export WRAP_UP_JIRA_TOKEN=your_api_token_here
export WRAP_UP_JIRA_EMAIL=your.email@redhat.com
export WRAP_UP_JIRA_URL=https://redhat.atlassian.net  # Optional, this is the default

# Memory Server (required)
export BOT_MEMORY_URL=https://memory-server.example.com

# GitHub (required for branch deletion)
export GITHUB_TOKEN=ghp_your_token_here  # Or GH_TOKEN
```

**Note:** Uses JIRA Cloud API v3 with Basic authentication (email + API token).

## Testing

```bash
# Run all tests (30 total)
uv run pytest -v

# Run specific test file
uv run pytest tests/test_operations.py -v  # 20 unit tests
uv run pytest tests/test_integration.py -v  # 10 integration tests

# Run with coverage
uv run pytest --cov=scripts --cov-report=html -v

# View coverage report
open htmlcov/index.html
```

### Test Coverage

- **Unit tests** (`test_operations.py`): 20 tests for individual operations
  - Verifies exact API URLs, headers, JSON payloads
  - Tests error handling and edge cases
  - Validates Memory Server, JIRA, GitHub integrations

- **Integration tests** (`test_integration.py`): 10 tests for full workflow
  - End-to-end scenarios with mocked APIs
  - Tests fail-fast behavior
  - Validates skip operations and dry-run mode

## Architecture

### Design Principles

1. **Fail fast**: Stop on first error to maintain consistency
2. **No LLM reasoning**: All inputs known from triage detection
3. **Sequential execution**: Operations have dependencies (e.g., get task info before everything else)
4. **Idempotent**: Safe to retry on failure
5. **Observable**: Logs all actions to stdout
6. **Testable**: Comprehensive unit and integration tests

### File Structure

```
.claude/skills/wrap-up/
├── SKILL.md                     # Skill documentation (Claude Code entrypoint)
├── README.md                    # This file
├── pyproject.toml               # Dependencies and tool config
├── uv.lock                      # Locked dependencies
├── scripts/
│   ├── __init__.py
│   └── wrap_up_operations.py   # Main implementation (~700 lines)
└── tests/
    ├── __init__.py
    ├── test_operations.py       # Unit tests (20 tests)
    └── test_integration.py      # Integration tests (10 tests)
```

### Dependencies

- **Python 3.12+**: Modern type hints and language features
- **httpx >= 0.27.0**: HTTP client for Memory Server, JIRA, and GitHub APIs
- **pytest >= 8.0.0**: Testing framework (dev dependency)
- **pytest-cov >= 4.1.0**: Coverage reporting (dev dependency)

Managed with **uv** for fast, reliable dependency resolution.

## API Integrations

### Memory Server API

- **Get task info**: `GET {BOT_MEMORY_URL}/tasks/{jira_key}`
- **Archive task**: `POST {BOT_MEMORY_URL}/tasks/{jira_key}/archive`
- **Send Slack notification**: `POST {BOT_MEMORY_URL}/notifications/slack`
- **Update bot status**: `POST {BOT_MEMORY_URL}/bot/status`

### JIRA Cloud API v3

- **Authentication**: Basic (email:token)
- **Transitions**: `GET/POST /rest/api/3/issue/{key}/transitions`
- **Comments**: Atlassian Document Format (ADF)
- **Endpoint**: `https://redhat.atlassian.net` (default)
- **Note**: Requires API v3 (v2 returns empty transitions array)

### GitHub REST API

- **Delete branch**: `DELETE /repos/{owner}/{repo}/git/refs/heads/{branch}`
- **Authentication**: Bearer token (GITHUB_TOKEN or GH_TOKEN)
- **Endpoint**: `https://api.github.com`

## Troubleshooting

### Common Issues

**Error: "JIRA token not configured"**
- Set `WRAP_UP_JIRA_TOKEN` and `WRAP_UP_JIRA_EMAIL` environment variables
- Or pass `--jira-token` and `--jira-email` parameters
- Or run with `--skip=jira_transition_issue,jira_add_comment`

**Error: "Memory server URL not configured"**
- Set `BOT_MEMORY_URL` environment variable
- Or pass `--memory-url` parameter

**Error: "GitHub token not configured"**
- Set `GITHUB_TOKEN` or `GH_TOKEN` environment variable
- Or pass `--github-token` parameter
- Or run with `--skip=delete_remote_branch`

**JIRA transitions return empty array**
- Ensure using JIRA Cloud API v3 (not v2)
- Check `WRAP_UP_JIRA_URL` is set to `https://redhat.atlassian.net`
- Verify email and token are correct for Basic auth

**Workflow stops partway through**
- This is expected behavior (fail-fast)
- Check error message to identify which operation failed
- Fix the issue and re-run the workflow

### Dry Run Mode

Use `--dry-run` to preview what would happen without executing:

```bash
uv run python scripts/wrap_up_operations.py RHCLOUD-12345 --dry-run
```

This logs all actions but doesn't make API calls, delete branches, or archive tasks.

## Contributing

### Code Style

- **Line length**: 120 characters (black + ruff)
- **Type hints**: Required for all functions
- **Docstrings**: Google style for all public functions
- **Tests**: Required for all new operations

### Adding New Operations

1. Add method to `WrapUpOperations` class
2. Update `execute_wrap_up_workflow` to call the new operation
3. Add unit tests in `tests/test_operations.py`
4. Add integration tests in `tests/test_integration.py`
5. Update SKILL.md documentation

### Running Tests Before Commit

```bash
# Lint and auto-fix
uv run ruff check --fix scripts/

# Run tests
uv run pytest -v

# Check coverage
uv run pytest --cov=scripts --cov-report=term-missing -v
```

**Note**: Line length is configured to 120 characters in pyproject.toml.

## Differences from /post-pr Skill

| Aspect | /post-pr | /wrap-up |
|--------|----------|----------|
| **Trigger** | After PR creation | After PR merge |
| **JIRA Transition** | "Code Review" | "Release Pending" |
| **GitHub Operation** | Update PR, add labels | Delete branch |
| **Git Operation** | None | Delete local branch |
| **Data Source** | CLI arguments | Memory server |
| **Storage** | Save learnings | Archive task |
| **Total Operations** | 6 | 8 |

## License

Same as parent project (hcc-ai-assistant).

## Related

- **Parent JIRA**: RHCLOUD-46589
- **Related Skill**: `/post-pr` - Post-PR-creation bookkeeping
