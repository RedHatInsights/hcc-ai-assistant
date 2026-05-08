# Claim Ticket Skill

Consolidates the entire JIRA ticket claiming sequence into a single efficient operation, reducing ~10 tool calls per new-work cycle into one script execution.

## Usage

```bash
/claim-ticket RHCLOUD-12345
```

## What It Does

When the bot picks up new work, this skill executes 8 operations in sequence:

1. **get_bot_account_id** - Retrieve bot's JIRA account ID (cached after first call)
2. **get_transitions** - Get available transitions and find "In Progress" ID
3. **assign_ticket** - Assign ticket to bot user
4. **transition_to_in_progress** - Move ticket to "In Progress" status
5. **resolve_board** - Determine correct board (platform-experience-ui label → 9297, else → 8070)
6. **get_active_sprint** - Get active sprint from the board
7. **add_to_sprint** - Add ticket to active sprint
8. **task_add** - Track ticket in memory server

All operations use **fail-fast error handling**: if any operation fails, execution stops immediately.

## Configuration

Set these environment variables:

```bash
# JIRA Cloud (required)
export CLAIM_TICKET_JIRA_TOKEN=your_api_token_here
export CLAIM_TICKET_JIRA_EMAIL=your.email@redhat.com
export CLAIM_TICKET_JIRA_URL=https://redhat.atlassian.net  # Optional, default

# Memory Server (required)
export BOT_MEMORY_URL=https://memory-server.example.com

# Board Configuration (optional, has defaults)
export PLATFORM_UI_BOARD_ID=9297
export DEFAULT_BOARD_ID=8070
```

## Operations

### 1. Get Bot Account ID (`get_bot_account_id`)

Retrieves the bot's JIRA account ID using the email address. Result is cached for performance.

**API**: `GET {JIRA_URL}/rest/api/3/user/search?query={email}`

### 2. Get Transitions (`get_transitions`)

Gets available transitions for the ticket and finds "In Progress" transition ID.

**API**: `GET {JIRA_URL}/rest/api/3/issue/{key}/transitions`

### 3. Assign Ticket (`assign_ticket`)

Assigns the ticket to the bot user.

**API**: `PUT {JIRA_URL}/rest/api/3/issue/{key}/assignee`

### 4. Transition to In Progress (`transition_to_in_progress`)

Moves the ticket to "In Progress" status using the transition ID from step 2.

**API**: `POST {JIRA_URL}/rest/api/3/issue/{key}/transitions`

### 5. Resolve Board (`resolve_board`)

Determines the correct board based on ticket labels:
- If ticket has `platform-experience-ui` label → Board 9297
- Otherwise → Board 8070 (default)

**API**: `GET {JIRA_URL}/rest/api/3/issue/{key}?fields=labels`

### 6. Get Active Sprint (`get_active_sprint`)

Gets the active sprint from the determined board.

**API**: `GET {JIRA_URL}/rest/agile/1.0/board/{boardId}/sprint?state=active`

### 7. Add to Sprint (`add_to_sprint`)

Adds the ticket to the active sprint.

**API**: `POST {JIRA_URL}/rest/agile/1.0/sprint/{sprintId}/issue`

### 8. Task Add (`task_add`)

Tracks the ticket in the memory server.

**API**: `POST {BOT_MEMORY_URL}/tasks`

## Error Handling

All operations follow fail-fast behavior:
- If any operation fails, execution stops immediately
- Error messages include operation name and details
- No partial state changes (all-or-nothing per operation)

## Testing

The skill includes comprehensive tests:
- **Unit tests**: Tests for individual operations
- **Integration tests**: Tests for full workflow
- **Total**: 30+ tests

Run tests:

```bash
cd .claude/skills/claim-ticket
uv run pytest -v
```

## Implementation

The skill is implemented in Python 3.12+ with:
- **httpx** for HTTP API calls
- **fail-fast error handling** for reliability
- **account ID caching** for performance
- **comprehensive logging** for observability
- **dry-run mode** for safe testing
- **type hints** for code clarity

## Board Resolution Logic

The skill uses label-based board detection:
- Checks if ticket has `platform-experience-ui` label
- If yes: use Board 9297 (Platform Experience UI board)
- If no: use Board 8070 (default board)

This eliminates the trial-and-error approach that previously required retries.

## Related

- JIRA: RHCLOUD-47263
- See also: `/post-pr` skill for post-PR-creation bookkeeping
- See also: `/wrap-up` skill for post-merge bookkeeping
