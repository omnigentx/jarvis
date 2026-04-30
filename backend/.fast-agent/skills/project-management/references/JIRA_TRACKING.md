# Jira Issue Tracking — Shared Account Rules

All agents share ONE Jira Cloud account via `mcp-atlassian` tools.

## Naming Convention
- Prefix issue summaries with your role tag: `[PM]`, `[BA]`, `[Dev]`, `[QE]`, `[DSO]`
- In comments, always start with `**Your Name:**` before your message
- Example: `**Minh - Dev:** Fixed the auth bug, PR #42 ready for review`

## Key Tools (mcp-atlassian)

| Action | Tool |
|--------|------|
| Create issue | `jira_create_issue(project_key, summary, issue_type, description)` |
| Search issues | `jira_search(jql)` — use JQL queries |
| Update issue | `jira_update_issue(issue_key, ...)` |
| Add comment | `jira_add_comment(issue_key, body)` |
| Transition issue | `jira_transition_issue(issue_key, transition)` — e.g. "In Progress", "Done" |
| Get issue | `jira_get_issue(issue_key)` |
| List projects | `jira_list_projects()` |
| Create sprint | `jira_create_sprint(board_id, name)` |
| Get board | `jira_get_board(board_id)` |

## Confluence Integration

| Action | Tool |
|--------|------|
| Create page | `confluence_create_page(space_key, title, body)` |
| Update page | `confluence_update_page(page_id, title, body)` |
| Search | `confluence_search(cql)` — use CQL queries |
| Get page | `confluence_get_page(page_id)` |

## Issue Workflow
- PM creates and assigns issues. Use description to note: `Assigned to: <agent_name>`
- When completing work, add comment: `**Your Name:** [DONE] <summary>`
- Transition issues through: `To Do` → `In Progress` → `In Review` → `Done`
- For bugs: use issue_type="Bug" with severity label

## Best Practices
- Check existing issues before creating duplicates (`jira_search`)
- Link related issues when relevant
- Keep comments concise and actionable
- Use Confluence for long-form docs (BRD, Architecture, Test Plans, Analysis Reports)
- **Workspace MD files are ONLY temporary drafts** — final output ALWAYS goes to Jira/Confluence

## Analysis Project — Backlog Creation Pattern

For analysis/research tasks, use Jira as the PRIMARY output:

### Step 1: Create Epic
```
jira_create_issue(project_key="<KEY>", summary="[ANALYSIS] <project name>", issue_type="Epic", description="<scope>")
```

### Step 2: Create Stories under Epic
For each finding/improvement:
```
jira_create_issue(project_key="<KEY>", summary="[IMPROVEMENT] <title>", issue_type="Story", description="Problem: ...\nProposed solution: ...\nExpected impact: ...\nAcceptance Criteria: ...", parent_key="<EPIC-KEY>")
```

### Step 3: Prioritize + Estimate
Update each story with priority (Must/Should/Could/Won't) and estimate (S/M/L/XL).

### Step 4: Create Sprint Plan on Confluence
```
confluence_create_page(space_key="<KEY>", title="Sprint 1 Plan — <project>", body="<sprint plan with rationale>")
```

> **Discovery**: Use `jira_list_projects()` to find the project key, and `confluence_search(cql="type=space")` to find space key.
