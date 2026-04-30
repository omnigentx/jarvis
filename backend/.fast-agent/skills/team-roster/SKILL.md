---
name: team-roster
description: >
  Team member roster, expertise, tools, and coordination guide.
  Read this skill to know who to ask for help and what they can do.
---

# TEAM ROSTER

## Team Members & Capabilities

| Agent | Role | Key Tools | Can Do |
|-------|------|-----------|--------|
| Linh - PM | Project Manager | email, agent_spawner, github, meeting_room | Spawn/manage team members, track progress, search GitHub issues/PRs, create meetings |
| Hoa - BA | Business Analyst | email, github, scrapling, figma-ui-mcp, meeting_room | Research requirements, browse web for references, read GitHub code, review Figma designs |
| Khang - SA | Solution Architect | email, github, scrapling, figma-ui-mcp, meeting_room | Design architecture, read/search GitHub repos, research tech via web, review Figma layouts |
| Minh - Dev | Developer | email, **git**, github, scrapling, figma-ui-mcp, meeting_room | **Clone/pull/push code via git**, implement features, read GitHub PRs, browse API docs |
| Trang - Designer | UI/UX Designer | email, **figma-ui-mcp**, meeting_room | **Create Figma designs** (pages, frames, components), design UI/UX flows, export assets |
| Tuan - QE | QA Engineer | email, github, scrapling, figma-ui-mcp, meeting_room | Write/run tests, read GitHub code, verify against Figma designs, browse test references |
| Duc - DSO | DevSecOps | email, **git**, github, scrapling, meeting_room | **Clone/pull/push code**, manage CI/CD pipelines, deploy, security review |

<important_capabilities>
## Who can do what — delegate accordingly!

- **Need code in workspace?** → Ask **Minh - Dev** or **Duc - DSO** to `git clone` the repo
- **Need a Figma design?** → Ask **Trang - Designer** to create it with `figma-ui-mcp`
- **Need to read code on GitHub?** → **Linh - PM**, **Hoa - BA**, **Khang - SA**, **Minh - Dev**, **Tuan - QE**, **Duc - DSO** all have `github` tool
- **Need web research?** → **Hoa - BA**, **Khang - SA**, **Minh - Dev**, **Tuan - QE**, **Duc - DSO** have `scrapling` tool
- **Need to deploy/CI?** → Ask **Duc - DSO**
</important_capabilities>

<communication_rules>
1. Use `send_email`: `send_email(to="Minh - Dev", body="...", subject="...")`
2. Emails from teammates are **auto-delivered** — no need to poll
3. Wait for dependency: `send_email(to="Agent", body="Send me [deliverable] when ready", subject="[WAITING] ...")`
4. Check status: `check_teammate_status(agent_name="Hoa - BA")`
5. DO NOT guess — ASK teammates when uncertain
6. DO NOT resend a message you already sent
7. ALWAYS use agent display name — not role key (use "Hoa - BA" not "ba")
</communication_rules>

<escalation_rules>
Escalate to PM when:
- Blocked more than 2 times by the same issue
- Scope has changed from the original plan
- Need a team member not yet in the team
- Discovered a risk affecting the timeline
</escalation_rules>

## Standard Workflow

```
Linh - PM assigns → Hoa - BA writes BRD → Khang - SA reviews & designs
→ Minh - Dev implements → Tuan - QE tests → Duc - DSO deploys
→ Linh - PM wraps up
```

> **Note**: Not every project needs all 7 members.
> PM decides which members participate based on scope.
