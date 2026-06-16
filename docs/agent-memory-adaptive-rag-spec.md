# Agent Memory and Adaptive Agentic RAG

## 1. Status

Proposed implementation specification.

This document defines the next memory architecture for Jarvis. It builds on the
existing context compaction system but does not require or introduce a graph
database in the current implementation scope.

GraphRAG is explicitly deferred. The architecture must preserve a provider
boundary so a graph database can be evaluated and added later without changing
the memory domain model, agent tool contracts, or retrieval evidence format.

## 2. Objective

Give every Jarvis agent durable, private, inspectable memory that helps it:

- continue long-running work with bounded context;
- recall past conversations and decisions;
- remember user and project preferences;
- retrieve relevant facts using BM25 and semantic similarity;
- learn reusable workflows and propose Skills or MCP improvements;
- explain which memories and sources influenced an answer;
- minimize additional LLM calls, latency, and injected tokens.

The experience must remain natural. Most chat turns must not trigger a
retrieval-planning LLM call. Memory processing and indexing must not block the
agent's response unless the current task explicitly requires retrieved
evidence.

## 3. Product Decisions

### 3.1 Memory ownership

Every durable memory belongs to exactly one agent.

There is no team-wide memory store. Agents exchange knowledge through existing
communication and artifact mechanisms such as email, meetings, prompt
injection, Jira, Confluence, files, reports, and Skills.

An agent may retrieve a communication record only when it is an authorized
participant. If it decides that received information is worth remembering, it
creates its own private memory with provenance pointing to that communication.

Agent identity is the normalized agent name (instance suffixes such as `[1]`
are stripped, matching `normalize_agent_name()` in
`backend/services/sse_progress.py`). There is no separate agent UUID; memory
tables key on `owner_agent_name`, consistent with existing tables such as
`agent_activities` and `agent_context_snapshots`.

Identity prerequisites (blocking — must land before memory ownership ships):

- Role labels (for example `SA`, `PM`) are display metadata, not identity. Any
  code path that currently uses a role string where an agent name is required
  must be standardized to the agent name. Known offender:
  `spawn_and_run_isolated` accepts only `role` and that role string becomes
  the agent identity (`isolated_runner.py`: `agent_name = TEAM_MY_NAME or
  role`); its signature changes to accept `agent_name`, with `role` demoted
  to display-only metadata.
- Unique normalized agent names are enforced at the creation entry points
  (`spawn_agent`, `spawn_and_run_isolated`, `spawn_team_members`) through one
  shared check (`ensure_unique_agent_name()`), never copy-pasted per caller.
  A collision with a live agent or a different existing definition is
  rejected with an explicit error, never silently suffixed or overwritten. An
  audit test asserts every creation entry point calls the shared check.
- Resume, restart, and auto-wake paths take a `run_id` and load the agent
  name from the existing spawn record — they have no name input, so no
  uniqueness check applies there by construction.
- Renaming an agent is an explicit migration that rewrites
  `owner_agent_name` across memory tables; it never happens implicitly.

### 3.2 Memory is not context compaction

Context compaction preserves continuity of the current task. Durable memory
helps an agent learn across tasks and sessions.

The existing compactor may produce memory candidates from information it has
already analyzed, but it must not write memory directly.

### 3.3 LLMs do not own persistence

LLMs may propose:

- memory candidates;
- conflict resolutions;
- procedural candidates;
- Skill or MCP proposals.

Only deterministic backend services may validate, version, authorize, and
persist memory.

### 3.4 Adaptive retrieval

Retrieval is available on every turn, but expensive agentic retrieval is not.

The default hierarchy is:

```text
Level 0: no retrieval
Level 1: deterministic fast retrieval
Level 2: bounded agentic retrieval
```

### 3.5 Source of truth

SQLite remains the source of truth for memory records, versions, provenance,
jobs, and audit data.

Qdrant is a rebuildable search index. Losing Qdrant must not lose memory.

### 3.6 Current technology scope

Use:

- SQLite for canonical domain data and transactional outbox;
- Qdrant for BM25 sparse search, dense vector search, filtering, and RRF;
- `BAAI/bge-m3` for multilingual dense embeddings;
- `BAAI/bge-reranker-v2-m3` for conditional reranking only;
- existing FastAPI, fast-agent hooks, background jobs, approvals, and SSE;
- GitNexus only as development tooling, not a required runtime dependency.

Defer:

- graph database selection;
- GraphRAG traversal and community summaries;
- global/team memory;
- mandatory cloud services;
- autonomous Skill or MCP publication.

## 4. Memory Model

Jarvis uses five conceptual layers.

### 4.1 Working context

Purpose: information required to complete the current task.

Storage/runtime:

- `agent.message_history`;
- raw snapshots;
- compacted working context;
- current evidence ledger.

Behavior:

- always bounded;
- automatically compacted;
- not indexed as a separate durable memory type;
- may contain retrieved evidence references.

Existing integration:

- `backend/services/context_compaction.py`
- `backend/services/context_persistence.py`
- `backend/services/session_service.py`

### 4.2 Pinned memory

Purpose: small, high-value instructions or preferences that should usually be
available without search.

Examples:

- preferred language;
- implementation approval rules;
- project-specific preferences;
- durable communication style;
- explicit "remember this" instructions.

Rules:

- owned by one agent;
- hard token budget per agent;
- loaded at session start or when the memory revision changes, by appending a
  clearly delimited pinned-memory block through the existing session load path
  (`backend/services/session_service.py`); pinned memory must not duplicate
  content already present in the agent instruction or attached Skills;
- only user messages and related existing memories are needed for most
  preference extraction;
- sensitive data is never auto-pinned.

### 4.3 Episodic memory

Purpose: searchable history of what the agent experienced.

Examples:

- previous conversations;
- decisions made in a session;
- incidents and recoveries;
- emails sent or received;
- meetings attended;
- task outcomes.

Rules:

- indexing does not require an LLM;
- `episodic_documents.content` is an immutable, hash-verified, self-contained
  copy; it is the durable record served to retrieval;
- live session history files are mutable (compaction rewrites them) and must
  never be used as a stable address;
- progressive disclosure fetches the full `episodic_documents` row for a chunk
  (one table, one write path — no cross-store anchor that can drift); snapshot
  or session references inside `metadata_json` are debug metadata only, never
  a required lookup path;
- retrieval returns excerpts with source references.

### 4.4 Procedural memory

Purpose: reusable methods for completing classes of work.

Examples:

- a reliable debugging sequence;
- a deployment verification workflow;
- a recurring tool sequence;
- a proposal for a new Skill or MCP integration.

Rules:

- generated only after task completion or repeated evidence;
- uses a reduced task trace rather than unbounded raw context;
- remains a candidate until approved;
- approved procedures are published as versioned Skills, runbooks, or MCP
  proposals, not silently injected as hidden instructions.

### 4.5 Semantic memory

Purpose: stable facts, decisions, and relationships learned by one agent.

Examples:

- a project architecture decision;
- an operational fact verified through a tool;
- a user correction;
- an external-system fact with a source.

Rules:

- every fact requires provenance;
- assistant statements alone are not sufficient authority;
- facts may be superseded, expire, or conflict;
- GraphRAG projection is deferred, but records must be graph-ready.

## 5. High-Level Architecture

```text
                         READ PATH

User request
    |
    v
Task Agent
    |
    v
Deterministic Retrieval Router
    |---------------------------|
    |                           |
    v                           v
Level 0: no retrieval      Level 1: fast retrieval
                                |
                                v
                     Qdrant BM25 + dense vector
                                |
                                v
                         RRF + policy scoring
                                |
                     sufficient? ---- no ----> Level 2
                                |               bounded planner/
                               yes              corrective retrieval
                                |
                                v
                        Evidence Builder
                                |
                                v
                       Agent working context


                         WRITE PATH

Conversation/lifecycle events
    |
    v
Memory Router
    |-----------------------------------------------|
    |                 |               |             |
user preference   completed turn   compaction   task completion
    |                 |               |             |
candidate         episodic index   candidates   task trace
    |                 |               |             |
    |-----------------v---------------v-------------|
                      |
                      v
              Memory Candidate Service
                      |
          validate / deduplicate / authorize
                      |
             simple? -------- no ------> Curator LLM
                      |                     |
                     yes                    v
                      |               structured decision
                      |                     |
                      v                     v
                  Memory Service
                      |
             SQLite transaction + outbox
                      |
                      v
              Background Index Worker
                      |
                      v
                    Qdrant
```

## 6. Existing Jarvis Integration Points

The implementation must extend existing services rather than create parallel
lifecycle, persistence, or realtime systems.

| Concern | Existing integration point |
|---|---|
| Live working context | `backend/fast-agent/src/fast_agent/agents/llm_decorator.py` |
| Compaction and candidate extraction | `backend/services/context_compaction.py` |
| Raw/working snapshots | `backend/services/context_persistence.py` |
| Chat session load/save | `backend/services/session_service.py` |
| Resumable spawned agents | `backend/services/inject_resume.py` |
| Tool/LLM lifecycle hooks | fast-agent `ToolRunnerHooks` (attach pattern: `backend/services/context_compaction.py`) |
| Request-scoped progress SSE | `backend/services/sse_progress.py` |
| Canonical message stream | `backend/services/agent_message_stream.py` |
| Global realtime events | `backend/services/activity_stream.py` |
| Background task patterns | `backend/services/background_jobs.py` |
| Approval workflow | `backend/services/approval_service.py` |
| Skill persistence/publication | `backend/services/skill_service.py` |
| Canonical SQLite schema | `backend/core/database.py` |
| Authenticated REST client | `frontend/src/api.js` |
| Agent detail/version UX | `frontend/src/views/AgentDetail.vue` |
| Settings pattern | `frontend/src/views/SettingsView.vue` |

Integration rules:

- add memory hooks through `ToolRunnerHooks`; do not monkey-patch fast-agent;
- do not create a second message event format;
- use `ActivityStreamManager` for global memory lifecycle events;
- use the existing progress stream only for request-specific chat feedback;
- store runtime-created agent configuration through the existing agent
  definition services;
- reuse approval and Skill services rather than duplicating their state
  machines;
- keep memory indexing outside the synchronous chat and compaction critical
  paths;
- within `before_llm_call`, the compaction check runs first and the retrieval
  hook runs after it, so fresh evidence is never summarized away in the same
  turn; evidence blocks carry lightweight markers for identification and
  deduplication only — if a later compaction drops them, re-retrieval is cheap
  (evidence ledger + cache) and raw snapshots keep the audit trail.

## 7. Adaptive Retrieval Policy

### 7.1 Level 0: no retrieval

Use when:

- the request is social or trivial;
- the answer is present in recent working context;
- the agent is continuing the current tool loop;
- required evidence is already in the evidence ledger;
- the agent has no indication that historical knowledge is needed.

This must be the most common path.

### 7.2 Level 1: deterministic fast retrieval

No additional LLM call.

Pipeline:

```text
query normalization
-> owner and permission filters
-> BM25 prefetch
-> dense-vector prefetch
-> RRF fusion
-> deterministic policy scoring
-> evidence selection
```

Deterministic trigger examples:

| Signal | Retrieval target |
|---|---|
| "last time", "previously", "have we ever" | episodic memory |
| "remember", "my preference", "from now on" | pinned memory |
| exact error, file, endpoint, ID, symbol | BM25-first |
| paraphrased decision or concept | dense-first hybrid |
| "usual workflow", "how do we normally" | procedures and Skills |
| "email from", "meeting yesterday" | authorized communications |
| current/fresh external information | external MCP/provider, not memory |

Trigger lexicons must be bilingual (Vietnamese and English, including
code-switched phrasing such as "lần trước", "nhớ là", "như mọi khi") and live
in one dedicated module, following the `backend/helpers/crawl_markers.py`
pattern. Never inline keyword lists at call sites.

### 7.3 Level 2: bounded agentic retrieval

Escalation from Level 1 is decided by a deterministic quality gate
(`quality_gate.py`): numeric thresholds on top score, BM25/dense rank
disagreement, and result count — data-driven configuration, no LLM and no
scattered conditionals.

Use only when:

- fast retrieval is weak or contradictory;
- the task spans multiple sources;
- the task is high risk;
- the user requests deep research or verification;
- the task agent explicitly requests deeper retrieval.

Allowed behavior:

- generate at most three subqueries;
- use at most one corrective round in balanced mode;
- use at most two corrective rounds in deep mode;
- switch retrieval strategies;
- fetch full source content for selected excerpts;
- return "insufficient evidence".

Forbidden behavior:

- unbounded self-reflection;
- unrestricted source expansion;
- repeated retrieval with no coverage improvement;
- silent web access;
- bypassing agent ownership or source permissions.

### 7.4 Default budgets

```json
{
  "mode": "balanced",
  "max_fast_retrievals": 2,
  "max_agentic_rounds": 1,
  "max_subqueries": 3,
  "max_candidates_per_retriever": 30,
  "max_fused_candidates": 20,
  "max_evidence_items": 5,
  "max_evidence_tokens": 2500,
  "retrieval_timeout_ms": 3000,
  "deep_retrieval_timeout_ms": 15000,
  "planner": "on_low_confidence",
  "reranker": "on_ambiguity"
}
```

Settings modes:

- `economical`: no planner, no reranker, 1,000 evidence tokens;
- `balanced`: conditional planner/reranker, 2,500 evidence tokens;
- `deep`: bounded multi-query retrieval, 5,000 evidence tokens.

The default is `balanced`.

## 8. Hybrid Retrieval

### 8.1 Qdrant collection

Use one collection per embedding schema/version, not one collection per agent:

```text
jarvis_memory_bge_m3_v1
```

Agent isolation is enforced with mandatory payload filters.

Named vectors:

```text
dense
bm25
```

Required payload:

```json
{
  "chunk_id": "uuid",
  "record_id": "uuid",
  "owner_agent_name": "normalized-agent-name",
  "memory_type": "episodic",
  "subject_scope": "project:jarvis",
  "source_type": "session_message",
  "source_id": "episodic-document-id",
  "status": "active",
  "authority": "user_confirmed",
  "confidence": 0.95,
  "created_at": 0,
  "valid_from": 0,
  "valid_until": null,
  "content_hash": "sha256",
  "embedding_model": "BAAI/bge-m3",
  "embedding_revision": "pinned-revision",
  "index_revision": 1
}
```

Create payload indexes for:

- `owner_agent_name`;
- `memory_type`;
- `subject_scope`;
- `source_type`;
- `status`;
- `authority`;
- `created_at`;
- `embedding_revision`.

### 8.2 BM25

BM25 is mandatory because technical memory contains exact identifiers that
dense retrieval may weaken:

- filenames;
- symbols;
- routes;
- error text;
- ticket IDs;
- config keys;
- model and package names.

Use Qdrant sparse BM25 retrieval for the primary production path.

SQLite FTS5 may be maintained as:

- admin/debug search;
- degraded fallback;
- an index-consistency reference.

### 8.3 Dense vector retrieval

Use `BAAI/bge-m3` with a pinned model revision.

Requirements:

- self-hosted by default;
- multilingual Vietnamese/English support;
- batch document embedding in background;
- query embedding on demand;
- embedding provider interface;
- embedding and reranker models load lazily in the main backend process only;
  agent subprocesses always call the backend memory API and never load a model
  instance themselves;
- document model footprint (BGE-M3 ~2.3 GB, reranker ~1.1 GB on disk) and
  minimum RAM headroom for the deployment server before enabling dense
  retrieval.

### 8.4 Fusion

Retrieve BM25 top 30 and dense top 30 in parallel.

Use Reciprocal Rank Fusion rather than adding raw scores:

```text
BM25 top 30
     \
      -> RRF -> top 20
     /
dense top 30
```

Apply deterministic policy adjustment after RRF:

```text
adjusted_score =
  rrf_score
  * owner_scope_weight
  * authority_weight
  * freshness_weight
  * confidence_weight
  * status_weight
```

Do not allow policy weights to make a low-relevance result outrank all
retrieval evidence. They are bounded modifiers, not replacements for
relevance.

### 8.5 Conditional reranking

Use `BAAI/bge-reranker-v2-m3` only when:

- BM25 and dense rankings disagree significantly;
- top candidates have close scores;
- the query is multi-intent;
- a high-risk workflow requires higher precision;
- deep mode is selected.

Rerank at most 20 candidates and return at most 5 evidence items.

No reranker is needed for exact pinned-memory lookup or high-confidence exact
identifier matches.

## 9. Evidence Contract

All retrievers must return a common structure:

```json
{
  "evidence_id": "memory:uuid:chunk:uuid",
  "record_id": "uuid",
  "owner_agent_name": "normalized-agent-name",
  "memory_type": "episodic",
  "excerpt": "Relevant source excerpt",
  "source": {
    "type": "session_message",
    "id": "episodic-document-id",
    "timestamp": 0,
    "uri": null
  },
  "scores": {
    "bm25_rank": 2,
    "dense_rank": 5,
    "rrf": 0.03,
    "reranker": null,
    "final": 0.028
  },
  "authority": "user_confirmed",
  "confidence": 0.95,
  "validity": {
    "valid_from": 0,
    "valid_until": null
  }
}
```

The agent receives a compact evidence block. Full source content is loaded only
through progressive disclosure.

## 10. Evidence Ledger and Cache

Each live working context tracks evidence already introduced:

```json
{
  "evidence_id": "memory:uuid:chunk:uuid",
  "content_hash": "sha256",
  "introduced_at_turn": 18,
  "last_used_at_turn": 20,
  "source_revision": 4
}
```

Before searching, the router checks whether sufficient evidence is already
present.

Cache key:

```text
owner_agent_name
+ normalized_query
+ filters
+ memory_index_revision
+ retrieval_policy_version
```

Cache rules:

- do not rely only on TTL;
- invalidate when relevant source/index revisions change;
- never reuse results across agents;
- never inject duplicate evidence;
- the evidence ledger survives compaction; evidence dropped by compaction is
  re-retrieved on demand (usually a cache hit), not specially preserved.

## 11. Memory Write Pipeline

### 11.1 Memory Router

The router is deterministic and event-driven.

| Trigger | Context view | Result |
|---|---|---|
| completed user message | user message + matching pinned memories | preference candidate |
| completed turn | normalized logical turn | episodic index job |
| context compaction | compactor extractions + source indexes | decision/fact candidates |
| task completion/idle | reduced task trace | procedural candidate |
| user correction | corrected answer + correction | superseding candidate |
| explicit "remember" | user message + minimal local context | immediate candidate |
| repeated workflow evidence | related successful task traces | Skill/MCP proposal |

Every session or agent history stream keeps a review cursor so the same content
is not repeatedly sent to a curator.

### 11.2 Compactor integration

Extend the compactor output contract with structured candidates:

```json
{
  "memory_candidates": [
    {
      "type": "semantic",
      "subtype": "architecture_decision",
      "content": "Use a dedicated compactor agent.",
      "subject_scope": "project:jarvis",
      "source_message_indexes": [28, 29],
      "confidence": 0.97,
      "explicit": true
    }
  ]
}
```

Requirements:

- candidate extraction must not weaken summary quality;
- compaction succeeds or fails independently from memory processing;
- candidates are queued only after compaction is safely applied/persisted;
- memory indexing runs outside `before_llm_call`;
- `promote_to_memory` is a proposal, not a write command.

### 11.3 Deterministic path

No curator LLM is needed for:

- episodic indexing;
- schema validation;
- exact duplicate rejection;
- source authorization;
- version creation;
- explicit user "remember this" candidates with unambiguous scope;
- candidates already extracted by compaction that pass strict policy.

### 11.4 Curator path

Call a Memory Curator LLM only when:

- a candidate conflicts with active memory;
- scope or type is ambiguous;
- multiple memories should be merged;
- sensitive information needs classification;
- a task trace must be generalized into a procedure;
- repeated evidence may justify a Skill/MCP proposal.

The curator returns a structured decision and has no database tools.

The curator model is configured from the UI exactly like the existing LLM
provider selection: model, provider, and an optional custom OpenAI-compatible
base URL plus API key (so a cheaper router endpoint can be used). Default is
a low-cost model; token usage is recorded per run.

### 11.5 MemoryService

`MemoryService` is the only write authority.

Responsibilities:

- derive the true owner agent from trusted runtime/DB state;
- validate candidate type and source access;
- apply sensitivity policy;
- detect exact and semantic duplicates;
- detect conflicts and supersession;
- enforce pinned token limits;
- create immutable versions;
- persist source links;
- create an indexing outbox event;
- emit audit and SSE events.

It must never trust `owner_agent_name`, team, or global visibility supplied by an
LLM.

Candidate state has one authoritative home: `memory_candidates.status`. When a
candidate requires approval, MemoryService creates an approval request through
the existing `approval_service` with a `memory_candidate` approval type whose
metadata references the candidate ID. Approval resolution is an input event:
MemoryService consumes it and updates the candidate row. The UI and all other
readers read candidate state only from `memory_candidates`, never from the
approval table.

## 12. Canonical Data Model

### 12.1 `memory_records`

```text
id TEXT PRIMARY KEY
owner_agent_name TEXT NOT NULL
memory_type TEXT NOT NULL
memory_subtype TEXT
subject_scope TEXT NOT NULL
content TEXT NOT NULL
normalized_content TEXT NOT NULL
status TEXT NOT NULL
importance REAL NOT NULL
confidence REAL NOT NULL
authority TEXT NOT NULL
sensitivity TEXT NOT NULL
pinned INTEGER NOT NULL DEFAULT 0
valid_from REAL
valid_until REAL
current_version INTEGER NOT NULL
created_at REAL NOT NULL
updated_at REAL NOT NULL
```

Allowed statuses:

```text
active
superseded
archived
expired
deleted
pending_approval
```

No entity columns ship now: entity extraction belongs to the deferred
GraphRAG phase and can be backfilled from stored memory content at that time
(SQLite keeps full text, so deferring loses nothing). Graph readiness today
means only: provenance edges in `memory_sources`, controlled scopes, and the
`GraphProvider` interface boundary.

`subject_scope` is a controlled taxonomy (`user`, `project:<name>`,
`agent:<name>`, `system`), validated by MemoryService; free-form scopes are
rejected.

### 12.2 `memory_versions`

```text
id INTEGER PRIMARY KEY AUTOINCREMENT
memory_id TEXT NOT NULL
version INTEGER NOT NULL
content TEXT NOT NULL
metadata_json TEXT NOT NULL
change_type TEXT NOT NULL
changed_by TEXT NOT NULL
reason TEXT
created_at REAL NOT NULL
UNIQUE(memory_id, version)
```

### 12.3 `memory_sources`

```text
id INTEGER PRIMARY KEY AUTOINCREMENT
memory_id TEXT NOT NULL
memory_version INTEGER NOT NULL
source_type TEXT NOT NULL
source_id TEXT NOT NULL
source_agent_name TEXT
source_excerpt TEXT
source_hash TEXT
source_timestamp REAL
authority TEXT NOT NULL
created_at REAL NOT NULL
```

### 12.4 `memory_candidates`

```text
id TEXT PRIMARY KEY
owner_agent_name TEXT NOT NULL
candidate_type TEXT NOT NULL
payload_json TEXT NOT NULL
source_refs_json TEXT NOT NULL
status TEXT NOT NULL
confidence REAL NOT NULL
requires_curator INTEGER NOT NULL
requires_approval INTEGER NOT NULL
dedupe_key TEXT
created_at REAL NOT NULL
resolved_at REAL
resolution_json TEXT
```

### 12.5 `episodic_documents`

Search projection of authorized historical data:

```text
id TEXT PRIMARY KEY
owner_agent_name TEXT NOT NULL
session_id TEXT
run_id TEXT
document_type TEXT NOT NULL
source_id TEXT NOT NULL
content TEXT NOT NULL
metadata_json TEXT NOT NULL
content_hash TEXT NOT NULL
created_at REAL NOT NULL
indexed_revision INTEGER NOT NULL DEFAULT 0
```

### 12.6 `memory_index_outbox`

```text
id INTEGER PRIMARY KEY AUTOINCREMENT
event_type TEXT NOT NULL
aggregate_id TEXT NOT NULL
aggregate_revision INTEGER NOT NULL
payload_json TEXT NOT NULL
status TEXT NOT NULL
attempt_count INTEGER NOT NULL DEFAULT 0
next_attempt_at REAL NOT NULL
last_error TEXT
created_at REAL NOT NULL
completed_at REAL
UNIQUE(event_type, aggregate_id, aggregate_revision)
```

### 12.7 `retrieval_runs`

```text
id TEXT PRIMARY KEY
owner_agent_name TEXT NOT NULL
session_id TEXT
run_id TEXT
query_hash TEXT NOT NULL
mode TEXT NOT NULL
route_json TEXT NOT NULL
filters_json TEXT NOT NULL
result_ids_json TEXT NOT NULL
used_evidence_ids_json TEXT
bm25_ms INTEGER
dense_ms INTEGER
rerank_ms INTEGER
total_ms INTEGER NOT NULL
evidence_tokens INTEGER NOT NULL
planner_input_tokens INTEGER NOT NULL DEFAULT 0
planner_output_tokens INTEGER NOT NULL DEFAULT 0
cache_hit INTEGER NOT NULL DEFAULT 0
status TEXT NOT NULL
error_message TEXT
created_at REAL NOT NULL
```

### 12.8 Retention

Unbounded growth is a design bug, not an operational surprise:

- `episodic_documents`: prune or archive after a configurable window (default
  90 days) unless referenced by an active memory record's sources;
- `retrieval_runs`: keep a configurable window (default 30 days), then delete;
- pruning runs in the background indexing worker and emits audit events;
- retention windows are exposed in memory settings.

## 13. Chunking and Projection

Chunk by domain rather than a universal fixed size:

| Source | Chunk policy |
|---|---|
| user/assistant message | one message or one logical turn |
| tool trace | goal + tool name + important args + outcome |
| meeting | topic/speaker block |
| email | subject + normalized body |
| decision/fact | one memory record |
| Skill/runbook | heading-aware sections |
| prose artifact | 400-800 tokens, 50-100 overlap |
| oversized tool output | short projection + artifact reference |

Do not embed:

- secrets;
- raw binary data;
- duplicate snapshots;
- unbounded tool output;
- transient progress events;
- content the agent is not authorized to read.

## 14. Communication Records

Communication records are not team memory.

In-scope sources: meeting transcripts (`meetings`, `meeting_transcripts`
tables), prompt injections, session history, and emails. Emails are not
persisted in the core database today, so email indexing ships together with
email persistence: capture messages agents send or receive at the email tool
boundary (after-tool-call hook on the existing email path) into a
`communication_records` table (`id, channel, sender, recipients_json,
subject, body, source_ref, created_at`), giving memory an authorized,
provenance-bearing source. Which email path (internal vs MCP provider) feeds
the hook is confirmed during implementation.

Authorization rules:

- sender and recipients may index an email captured in
  `communication_records`;
- meeting participants (as listed in the meeting `config_json`) may index the
  meeting transcript;
- an agent may index a prompt injection it received;
- access is rechecked during retrieval;
- forwarded or summarized information preserves the original chain of
  provenance.

An agent may promote received information into its own semantic memory, but the
authority must reflect the source:

```text
tool_verified
user_confirmed
agent_observed
reported_by_agent
external_document
inferred
```

`inferred` memory cannot be pinned or automatically promoted to a Skill.

## 15. Agent Tool Contracts

Expose stable domain tools, not Qdrant internals:

### `memory_search`

```json
{
  "query": "previous context compaction decision",
  "types": ["episodic", "semantic"],
  "mode": "balanced",
  "limit": 5
}
```

The backend derives owner scope from the tool server binding: each agent's
memory MCP server (`backend/tools/memory_server.py`) is bound to that agent's
normalized name at spawn time (server config/environment), following the
existing per-agent server wiring. Tool arguments never carry an agent
identity; any identity-like argument is ignored.

### `memory_fetch`

Fetch full authorized source content for selected evidence IDs.

### `memory_remember`

Create a candidate. It never writes an active memory directly.

### `memory_forget`

Create an archive/delete request subject to policy and audit.

### `memory_feedback` (deferred)

Record whether retrieved evidence was useful, irrelevant, or incorrect.
Deferred: user-facing evidence feedback in the chat drawer ships first; an
agent-side feedback tool is added only if evaluation shows it is needed.

### `procedure_propose`

Create a procedural candidate or Skill/MCP proposal. Publication requires
approval.

## 16. API Surface

Suggested routes:

```text
GET    /api/memory/settings
PATCH  /api/memory/settings

GET    /api/agents/{name}/memories
GET    /api/agents/{name}/memories/{id}
GET    /api/agents/{name}/memories/{id}/versions
POST   /api/agents/{name}/memories/{id}/rollback
POST   /api/agents/{name}/memories/{id}/archive
DELETE /api/agents/{name}/memories/{id}

GET    /api/agents/{name}/memory-candidates
POST   /api/agents/{name}/memory-candidates/{id}/approve
POST   /api/agents/{name}/memory-candidates/{id}/reject
PATCH  /api/agents/{name}/memory-candidates/{id}

GET    /api/agents/{name}/retrieval-runs
GET    /api/agents/{name}/retrieval-runs/{id}

POST   /api/agents/{name}/memory-search
POST   /api/memory/reindex
GET    /api/memory/index-status
```

Every route must use existing API-key authentication and verify the requested
agent is accessible.

## 17. Realtime Events

Use `services/activity_stream.py` and existing SSE infrastructure.

No polling.

Events:

```text
memory_candidate_created
memory_candidate_approved
memory_candidate_rejected
memory_created
memory_updated
memory_superseded
memory_archived
memory_index_queued
memory_indexed
memory_index_failed
retrieval_started
retrieval_completed
retrieval_degraded
procedure_candidate_created
```

Chat should not show every internal event as a message bubble. Use lightweight
status text and an expandable evidence drawer.

The frontend handles only a small subset in `stores/agents.js`
(`processEvent`): `memory_candidate_created`,
`memory_candidate_approved`/`memory_candidate_rejected`, `memory_indexed`,
and `retrieval_degraded`. All other events exist for the audit trail and the
Memory page, not for live chat state.

## 18. UX Requirements

### 18.1 Chat

Default experience:

- no visible interruption for fast retrieval;
- optional status such as "Recalling relevant history";
- response indicates when historical evidence was used;
- "Memory used" drawer lists sources and excerpts;
- user can mark evidence useful, irrelevant, or incorrect.

Deep retrieval may show:

```text
Searching previous decisions
Checking related conversations
Verifying conflicting memories
```

Implementation pattern: a compact "memory used" chip under the assistant
message expands in place, following the existing tool-details accordion in
`ChatMessages.vue` (`expandedTools`). No new message bubbles and no modal for
the default flow.

### 18.2 Agent Memory page

Implemented as a new `memory` tab in `frontend/src/views/AgentDetail.vue`
(added to `validTabs`, lazy-loaded on activation like the existing `context`
and `versions` tabs), placed next to `context`.

Per-agent page with:

- memory types and counts;
- pinned memory budget;
- search;
- source/provenance;
- confidence and authority;
- created/updated/expiry timestamps;
- conflict and supersession history;
- edit, pin, archive, delete, and rollback actions;
- candidates awaiting approval;
- procedural/Skill/MCP proposals;
- retrieval usage and quality metrics.

### 18.3 Settings

Settings must include:

- memory enabled;
- retrieval mode;
- auto-capture explicit preferences;
- approval policy;
- pinned token budget;
- evidence token budget;
- curator model/provider with optional custom base URL and API key (same UX
  as the existing LLM provider selection);
- retrieval trigger lexicon and quality-gate thresholds;
- embedding model/revision;
- reranker enabled;
- retention and expiry policy;
- Qdrant connection/status;
- indexing/rebuild controls.

Implement as `SettingsMemory.vue` following the `SettingsCompaction.vue`
pattern (GET on mount, PATCH changed keys only, dirty-key tracking),
registered in the `SettingsView.vue` sidebar. Use the current Jarvis design
system and existing settings navigation pattern.

### 18.4 Mobile and language

- All memory UI must be responsive at the existing 768px breakpoint
  (`useBreakpoint`); the evidence drawer becomes a bottom sheet on mobile and
  Memory page tables collapse to cards;
- settings reuse the existing mobile horizontal-strip navigation;
- every user-visible string is bilingual through `useLang()`
  (`lang === 'vi' ? … : …`); single-language literals are bugs.

## 19. Background Indexing

The indexing worker consumes `memory_index_outbox`.

Flow:

```text
SQLite commit
-> outbox event
-> projector/chunker
-> embedding batch
-> Qdrant upsert/delete
-> mark indexed revision
-> SSE completion/failure
```

Requirements:

- idempotent jobs;
- exponential backoff;
- bounded retries plus a dead-letter state;
- restart recovery;
- per-record revision checks;
- batch embeddings;
- index rebuild from SQLite;
- stale-index detection;
- graceful degraded retrieval when Qdrant is unavailable.

Do not reuse the existing idle-only TTS scheduler without separating resource
policies. Memory indexing needs a durable queue and may run continuously at low
priority.

Implement the worker as a new `BackgroundJobRunner` registered with the
existing `BackgroundJobScheduler` (`backend/services/background_jobs.py`),
running continuously at low priority rather than idle-only. It inherits the
scheduler's persisted job state and restart recovery; the durable queue itself
is `memory_index_outbox`, not in-memory task state.

## 20. Failure and Degraded Modes

### Qdrant unavailable

- memory writes still succeed in SQLite;
- outbox remains pending;
- retrieval may use SQLite FTS5 fallback;
- no dense retrieval;
- emit `retrieval_degraded`;
- chat continues unless the user explicitly requires memory evidence.

### Embedding model unavailable

- BM25 remains available;
- indexing dense vector is retried;
- records indicate partial index state;
- do not silently substitute another model in the same collection.

### Reranker unavailable

- use RRF results;
- log and expose degraded rerank status;
- do not fail ordinary retrieval.

### Curator unavailable

- candidate remains pending;
- compaction and response continue;
- deterministic writes are unaffected.

### Invalid or conflicting memory

- never overwrite active memory silently;
- create a conflict candidate;
- require approval according to policy;
- preserve both source histories.

## 21. Security and Privacy

- Mandatory owner filter on every memory query.
- Backend derives ownership from trusted state.
- No cross-agent private-memory retrieval.
- Communication access is checked at query and fetch time.
- Secrets are detected before persistence and embedding.
- Sensitive candidates require approval or are rejected.
- Deletion removes search-index projections through the outbox.
- Audit records remain according to retention policy.
- Qdrant must not be exposed publicly by default.
- Model downloads require explicit setup disclosure and pinned revisions.
- No memory text is sent to an external embedding API unless the user
  explicitly configures that provider.

## 22. Observability and Evaluation

Track:

- percentage of turns at retrieval levels 0/1/2;
- retrieval latency by stage;
- evidence tokens injected;
- planner and curator tokens;
- cache-hit rate;
- BM25/dense overlap;
- reranker activation rate;
- evidence usage rate;
- user relevance feedback;
- stale or contradictory memory rate;
- candidate acceptance/rejection rate;
- task success with and without memory;
- Qdrant/indexing failure rate.

Initial operating targets:

```text
60-75% turns: Level 0
20-35% turns: Level 1
<10% turns: Level 2
balanced evidence: <= 2500 tokens
balanced corrective rounds: <= 1
fast retrieval p95: <= 500 ms after warm-up
```

Create an offline evaluation set containing:

- exact identifier queries;
- Vietnamese/English paraphrases;
- old decisions;
- user preferences;
- conflicting memories;
- stale facts;
- unauthorized cross-agent queries;
- insufficient-evidence cases;
- workflow/procedure retrieval.

Measure Recall@K, MRR/nDCG, source correctness, authorization correctness, and
answer groundedness.

## 23. OSS and Licensing Requirements

Jarvis remains MIT.

Required practices:

- pin Qdrant image versions;
- pin model revisions, not only model names;
- include third-party notices and model attribution;
- generate an SBOM in release CI;
- run dependency and license scanning;
- keep optional integrations outside core requirements;
- do not package GitNexus as a runtime dependency;
- document model size, hardware needs, and license before download.

Graph database selection remains open and must be reviewed independently for
license, platform support, maintenance, and operational maturity.

## 24. Implementation Scope and Build Order

The feature ships as one complete implementation of this spec. The build
order below exists only because of technical dependencies (identity before
ownership, schema before indexing, indexing before retrieval); it does not
define intermediate releases, and no step may ship a temporary design that a
later step rewrites.

### 1. Identity prerequisite (blocking)

- the full list in section 3.1: shared `ensure_unique_agent_name()` at the
  three creation entry points with an audit test, `spawn_and_run_isolated`
  switched from `role` to `agent_name`, role-vs-name standardization across
  the codebase.

### 2. Foundation

- domain interfaces and typed contracts;
- settings and feature flags;
- retrieval/evaluation telemetry and offline evaluation fixtures;
- Qdrant local development profile, pinned BGE model revisions;
- all canonical tables (section 12).

### 3. Indexing and retrieval

- project session/message history into `episodic_documents`;
- outbox worker, projector/chunker, BGE-M3 embedding provider;
- Qdrant BM25 + dense vectors with RRF as the production search path;
- SQLite FTS5 as degraded fallback, admin search, and consistency reference;
- ownership and communication authorization;
- `memory_search`, evidence fetching, cache, evidence ledger, progressive
  disclosure;
- deterministic router, quality gate, bounded planner and corrective
  retrieval, conditional BGE reranker, economical/balanced/deep modes.

### 4. Memory writes and learning

- memory candidates, versions, provenance, approval integration;
- explicit preference detection (pinned memory);
- compactor candidate integration;
- conflict and supersession workflows;
- email capture into `communication_records` via the email tool-boundary
  hook, indexed as an episodic source;
- reduced task traces, repeated-workflow clustering, procedure and Skill/MCP
  proposals through the existing approval and Skill services; never
  auto-publish.

### 5. UI

- Memory tab in AgentDetail, chat evidence drawer, `SettingsMemory.vue`,
  SSE event handling (section 17-18).

### Deferred: GraphRAG

Before selecting a graph backend:

- prove multi-hop graph queries add measurable value beyond hybrid search;
- define `GraphProvider`;
- evaluate license, Python/runtime support, concurrency, backup, portability,
  and OSS distribution;
- project only stable semantic entities and relations;
- run entity extraction at this phase, backfilled from stored memory content
  (no entity columns or extraction ship before the graph phase);
- keep SQLite as source of truth and graph storage rebuildable.

## 25. Suggested Module Boundaries

```text
backend/services/memory/
  models.py
  memory_service.py
  candidate_service.py
  provenance_service.py
  version_service.py
  authorization.py
  sensitivity.py

backend/services/retrieval/
  contracts.py
  orchestrator.py
  intent_router.py
  budget.py
  evidence_builder.py
  quality_gate.py
  cache.py
  providers/
    qdrant_provider.py
    sqlite_fts_provider.py
    communication_provider.py
    artifact_provider.py
    graph_provider.py          # interface only, no implementation now

backend/services/indexing/
  outbox_service.py
  memory_index_worker.py
  projector.py
  chunker.py
  embedding_provider.py
  bge_embedding_provider.py
  qdrant_indexer.py
  consistency_service.py

backend/routes/
  memory.py
  memory_settings.py

backend/tools/
  memory_server.py
```

Keep route handlers thin and keep files under the repository size guideline.

## 26. Test Requirements

### Unit

- agent identity normalization and duplicate-name spawn rejection;
- ownership and permission filters;
- BM25/dense RRF behavior;
- budget enforcement;
- deterministic trigger routing;
- candidate validation and deduplication;
- conflict/supersession logic;
- source authority;
- pinned token limits;
- outbox idempotency;
- evidence-ledger deduplication;
- compactor candidate isolation.

### Integration

- SQLite write plus outbox plus Qdrant projection;
- index rebuild after Qdrant data loss;
- degraded BM25 fallback;
- session history indexing;
- email/meeting authorization;
- model revision migration;
- SSE lifecycle events;
- approval and rollback;
- Skill proposal publication path.

### End-to-end

- short session remembers an explicit preference;
- long session compacts and emits candidates without interruption;
- agent recalls a previous decision using hybrid retrieval;
- exact error text is found through BM25;
- paraphrased Vietnamese query is found through dense retrieval;
- cross-agent memory query is denied;
- received email becomes private memory with provenance;
- low-confidence retrieval escalates once and then stops;
- Qdrant outage does not lose memory or break chat;
- user inspects, edits, rejects, and rolls back memory.

## 27. Acceptance Criteria

The feature is complete only when:

1. Every memory record has one trusted owner agent (unique normalized agent
   name) and provenance.
2. No agent can retrieve another agent's private memory.
3. Most turns incur no planner or curator LLM call.
4. Fast retrieval combines BM25 and dense search through RRF.
5. Retrieval obeys hard call, time, result, and token budgets.
6. SQLite can fully rebuild Qdrant.
7. Qdrant failure degrades safely without losing writes.
8. Compaction and memory creation fail independently.
9. User can inspect all active memories, candidates, versions, sources, and
   retrieval evidence.
10. Procedural memory cannot silently publish or modify a Skill/MCP.
11. All live updates use SSE; no polling is introduced.
12. Graph DB is not required for the feature to function.
13. Offline evaluation demonstrates improvement over BM25-only retrieval before
    hybrid retrieval becomes the default.
14. Token and latency telemetry proves Level 2 retrieval remains exceptional,
    not the default path.

## 28. Explicit Non-Goals

- A shared team brain.
- Automatic access to another agent's private history.
- Running a retrieval planner on every turn.
- Using vector similarity as the only retrieval method.
- Treating summaries as authoritative raw evidence.
- Letting an LLM write directly to memory tables.
- Automatically creating or installing MCP servers.
- Automatically publishing Skills.
- Replacing GitNexus in the developer workflow.
- Selecting or integrating a graph database in this phase.
