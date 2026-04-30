# Agile Team — Edge Cases & Communication Tool Matrix

Tài liệu phân tích các edge case thực tế khi team Agile agents hoạt động,
và tool nào giải quyết từng case.

## Communication Channels

| Channel | Purpose | When to Use |
|---------|---------|-------------|
| `meeting_room` (sync) | Structured multi-turn discussion | Code reviews, Q&A, brainstorming — both parties active |
| `post_message` / `read_messages` (async) | Fire-and-forget messaging | Agent busy, not spawned, broadcast, quick notifications |
| `delegate_task` / `wait_for_agent` (lifecycle) | Task assignment + completion tracking | PM assigns work, monitors progress |

## Edge Case Matrix

### Inter-Agent Dependencies

| # | Scenario | Tool Flow |
|---|----------|-----------|
| 1 | **Dev depends on SA** (can't code until arch is ready) | **PM-mediated:** PM `wait_for_spawned_agent("SA")` → `spawn_team_members("dev")` |
| | | **Peer-to-peer:** SA `post_message(to="Dev", "Arch ready")` → Dev `read_messages(from="SA", wait=True)` |
| | | **Auto-delivery:** Team status report is auto-delivered to PM inbox when ALL members finish |
| 2 | **Multi-dependency** (Dev needs BA + SA) | PM spawns BA+SA in parallel → waits for both → spawns Dev |
| 3 | **Parallel sync point** (BA + Designer both needed) | Both `post_message` to Dev on completion → Dev `read_messages(wait=True)` |

### Blocker Handling

| # | Scenario | Tool Flow |
|---|----------|-----------|
| 4 | **Blocker escalation** (Dev blocked on unclear req) | Dev `post_message(to="PM", type="blocker", "unclear lunar date format")` → PM reads → `create_meeting(pm, dev, ba)` |
| 5 | **Scope change mid-sprint** | PM `post_message(to="all", "SCOPE CHANGE: ...")` → broadcast to all agents |

### Review Cycles

| # | Scenario | Tool Flow |
|---|----------|-----------|
| 6 | **QE finds bug → Dev fixes → QE re-tests** | QE `post_message(to="Dev", "Bug: missing validation")` → Dev reads, fixes → `post_message(to="QE", "Fixed")` → QE re-tests |
| 7 | **Structured code review** | `create_meeting(participants="dev,qe", agenda="Code review")` → VERDICT: PASS/FAIL |
| 8 | **Revision after completion** | PM `resume_spawn(dev_run_id, "Fix bugs from QE report")` → Dev re-opens with context |

### Handoff & Status

| # | Scenario | Tool Flow |
|---|----------|-----------|
| 9 | **Sequential handoff** (BA→SA→Dev→QE) | Each writes to `workspace/`, then `post_message` to next role |
| 10 | **PM progress check** | `get_team_status(session_id)` — team status auto-delivered when members complete |

## Scenarios Where meeting_room Fails Without Async

| Scenario | Why meeting_room alone fails |
|----------|------------------------------|
| Agent busy writing code | Not calling `wait_for_my_turn` → meeting hangs |
| Agent in another meeting | Single-threaded LLM can't join 2 meetings |
| Agent not yet spawned | No process to join meeting |
| Agent completed/exited | Process gone, can't join |
| Quick notification | 4 tool calls vs 1 `post_message` |
| PM monitors multiple agents | Can only be in 1 meeting at a time |

## Key Design Decisions

1. **meeting_room = primary for discussions** — real-time, turn-based, with transcript
2. **post_message / read_messages = primary for async** — queued, auto-wake, non-blocking
3. **lifecycle tools = PM only** — delegate_task, wait_for, resume stay in agent_spawner
4. **auto-delivery = peer visibility** — team status reports auto-delivered to orchestrator inbox when all members finish
