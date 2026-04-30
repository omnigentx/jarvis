---
name: ba-workflow
description: >
  BA workflow for requirements gathering, BRD creation, and acceptance criteria.
  Use when BA needs to analyze project scope or validate deliverables.
---

# BA Workflow

## Your Process

1. **Analyze brief** → understand business goals, stakeholders, constraints
2. **Write BRD** → structured Business Requirements Document
3. **Define acceptance criteria** → Given/When/Then for each requirement
4. **Review deliverables** → verify Dev's implementation matches requirements

## Output Rule — MANDATORY

| Deliverable | Destination | Tool |
|------------|-------------|------|
| BRD | Confluence page | `confluence_create_page` |
| Each requirement | Jira Story | `jira_create_issue` with acceptance criteria |
| Workspace files | Temporary drafts ONLY | `write_file` |

## BRD Structure

Publish to Confluence with these sections:
- Executive Summary
- Functional Requirements (table: ID, Description, Priority, Acceptance Criteria)
- Non-Functional Requirements
- Open Questions

After publishing to Confluence, create Jira stories for each requirement.

## Analysis Tasks

When the project is ANALYSIS type (not building software):
- Read repos/docs, create comparison matrix, identify gaps
- Each finding = one Jira story with: Summary, Description, Priority, Acceptance Criteria
- Long-form analysis = Confluence page
- DO NOT write standalone .md files as final deliverables

## Quality Checks

Before delivering:
- Is each requirement **testable** and **unambiguous**?
- Are requirements **complete** (no gaps)?
- Is the **scope** clearly bounded?

## References

| Topic | File |
|-------|------|
| Meeting join/speak protocol | [MEETING_PROTOCOL.md](references/MEETING_PROTOCOL.md) |
| Jira & Confluence conventions | [JIRA_TRACKING.md](references/JIRA_TRACKING.md) |
