"""
Database module using SQLAlchemy with SQLite.
Provides database connection and ORM models.

Single-deployment-single-user model: each Jarvis instance has exactly one
``User`` row (``username='owner'``) seeded at init. The ``users`` table
exists to give ``passkey_credentials`` a proper FK target and to make a
later multi-user migration mechanical rather than a schema rewrite. Most
domain tables (books, agents, meetings, …) intentionally still do NOT
carry a ``user_id`` column — adding one would be a separate migration
when (if) Jarvis becomes multi-user.
"""
import os
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# Database configuration — CWD-relative (Docker WORKDIR=/app).
# Tests set JARVIS_DB_PATH to redirect to an isolated DB so autouse cleanup
# fixtures don't wipe the developer's runtime data.
_DB_PATH_OVERRIDE = os.environ.get("JARVIS_DB_PATH")
if _DB_PATH_OVERRIDE:
    DATA_DIR = os.path.dirname(_DB_PATH_OVERRIDE) or "."
    os.makedirs(DATA_DIR, exist_ok=True)
    DATABASE_URL = f"sqlite:///{_DB_PATH_OVERRIDE}"
else:
    DATA_DIR = os.path.join("data")
    os.makedirs(DATA_DIR, exist_ok=True)
    DATABASE_URL = f"sqlite:///{os.path.join(DATA_DIR, 'jarvis.db')}"

# Create engine and session
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

# --- ORM Models ---


class Book(Base):
    __tablename__ = "books"
    
    id = Column(String(100), primary_key=True)
    title = Column(String(255), nullable=False)
    chapter = Column(String(255), nullable=True)
    url = Column(String(500), nullable=True)
    file_path = Column(String(500), nullable=True)
    duration = Column(Float, default=0.0)
    current_time = Column(Float, default=0.0)
    status = Column(String(50), default="generating")  # generating, ready, error
    created_at = Column(Float, default=lambda: datetime.now().timestamp())
    last_played_at = Column(Float, nullable=True)


class TTSCache(Base):
    __tablename__ = "tts_cache"
    
    request_id = Column(String(100), primary_key=True)
    text = Column(Text, nullable=False)
    created_at = Column(Float, default=lambda: datetime.now().timestamp())


class StoryProgress(Base):
    """Track last chapter per story — enables 'Continue' feature."""
    __tablename__ = "story_progress"
    
    story_title = Column(String(255), primary_key=True)
    last_chapter_num = Column(Integer, default=0)
    last_chapter_file = Column(String(255), nullable=True)
    last_played_at = Column(Float, default=lambda: datetime.now().timestamp())


class BackgroundJobState(Base):
    """Persist background job state across server restarts (extensible)."""
    __tablename__ = "background_job_state"
    
    job_name = Column(String(100), primary_key=True)
    is_enabled = Column(Boolean, default=True)
    status = Column(String(50), default="idle")  # idle/running/paused/error
    current_task_info = Column(Text, nullable=True)  # JSON: current task details
    last_run_at = Column(Float, nullable=True)
    error_count = Column(Integer, default=0)
    stats_json = Column(Text, nullable=True)  # JSON: aggregate progress stats


class CrawlJob(Base):
    """Track crawl job status — bridges MCP subprocess and FastAPI process."""
    __tablename__ = "crawl_jobs"
    
    job_id = Column(String(100), primary_key=True)
    status = Column(String(50), default="pending")  # pending/running/completed/failed/cancelled
    story_title = Column(String(255), nullable=True)
    current_chapter = Column(Integer, default=0)
    total_chapters = Column(Integer, default=0)
    message = Column(String(500), nullable=True)
    start_url = Column(String(500), nullable=True)
    params = Column(Text, nullable=True)  # JSON: content_selector, title_selector, next_selector, speed, max_chapters
    created_at = Column(Float, default=lambda: datetime.now().timestamp())
    updated_at = Column(Float, default=lambda: datetime.now().timestamp())


class StoryProvider(Base):
    """Story-website config — replaces story_providers.json."""
    __tablename__ = "story_providers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    domain = Column(String(255), nullable=False, unique=True, index=True)
    name = Column(String(255), nullable=False)
    selectors_json = Column(Text, nullable=False)  # JSON: {content, title, next_chapter}
    search_url = Column(String(500), nullable=True)
    list_selector = Column(String(255), nullable=True)
    title_selector = Column(String(255), nullable=True)
    trust_level = Column(String(50), default="auto-learned")  # auto-learned | verified
    known_stories_json = Column(Text, nullable=True)  # JSON array [{title, url}]
    created_at = Column(Float, default=lambda: datetime.now().timestamp())
    updated_at = Column(Float, default=lambda: datetime.now().timestamp())


class StoryMeta(Base):
    """Story metadata — replaces per-folder metadata.json.
    Authoritative registry of every story in the system."""
    __tablename__ = "story_meta"

    story_id = Column(String(255), primary_key=True)  # = folder name under data/stories/
    title = Column(String(255), nullable=False)
    source_url = Column(String(500), nullable=True)
    chapter_count = Column(Integer, default=0)
    total_audio_size = Column(Integer, default=0)  # bytes
    cover_image = Column(String(500), nullable=True)
    created_at = Column(Float, default=lambda: datetime.now().timestamp())
    updated_at = Column(Float, default=lambda: datetime.now().timestamp())


class AudioCacheEntry(Base):
    """Audio-cache registry — tracks MP3 files under data/audio_cache/.
    MP3 files stay on disk (binary, streamed); the DB only tracks metadata."""
    __tablename__ = "audio_cache"

    content_hash = Column(String(64), primary_key=True)  # MD5 hash of text content
    file_path = Column(String(500), nullable=False)       # relative: data/audio_cache/{hash}.mp3
    file_size = Column(Integer, default=0)
    duration = Column(Float, nullable=True)  # seconds (from mutagen)
    story_id = Column(String(255), nullable=True, index=True)  # FK → story_meta.story_id
    chapter_file = Column(String(255), nullable=True)  # original .txt filename
    status = Column(String(20), default="generating")  # generating | ready | failed
    created_at = Column(Float, default=lambda: datetime.now().timestamp())
    last_accessed_at = Column(Float, nullable=True)  # for LRU eviction


class PendingAction(Base):
    """Cross-process action queue — replaces pending_read.json.
    MCP subprocess writes; FastAPI routes read and delete."""
    __tablename__ = "pending_actions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    action_type = Column(String(50), nullable=False)  # READ_LOCAL | READ_STORY | READ_LIBRARY
    payload_json = Column(Text, nullable=False)  # JSON: {story_title, chapter_filename, url, ...}
    created_at = Column(Float, default=lambda: datetime.now().timestamp())


class AgentActivity(Base):
    """Track agent activity events for monitoring."""
    __tablename__ = "agent_activities"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_name = Column(String(100), nullable=False, index=True)
    run_id = Column(String(100), nullable=True, index=True)
    session_id = Column(String(100), nullable=True, index=True)  # Links to conversation/session
    event_type = Column(String(50), nullable=False)  # started, tool_call, tool_result, response, error, idle
    message = Column(Text, nullable=True)
    data_json = Column(Text, nullable=True)  # JSON: tool args, result preview, duration_ms...
    created_at = Column(Float, default=lambda: datetime.now().timestamp(), index=True)


class TokenUsageRecord(Base):
    """Each record = 1 LLM API call (1 TurnUsage). Persisted for historical analysis."""
    __tablename__ = "token_usage"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_name = Column(String(100), nullable=False, index=True)
    run_id = Column(String(100), nullable=True, index=True)
    model = Column(String(100), nullable=False)
    # Spend category so non-agent LLM work (memory extraction, compaction) is
    # filterable separately from normal agent turns. "agent" = a normal turn.
    category = Column(String(40), nullable=False, default="agent", index=True)

    # Token counts (for this specific LLM call)
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    cache_hit_tokens = Column(Integer, default=0)
    cache_read_tokens = Column(Integer, default=0)
    cache_write_tokens = Column(Integer, default=0)
    reasoning_tokens = Column(Integer, default=0)
    
    # Estimated cost in USD (from model_pricing.yaml)
    est_cost = Column(Float, default=0.0)
    
    created_at = Column(Float, default=lambda: datetime.now().timestamp(), index=True)


class SpawnRecordModel(Base):
    """SQLite-backed agent spawn registry (mirrors fast-agent JSON registry)."""
    __tablename__ = "spawn_records"
    
    run_id = Column(String(100), primary_key=True)
    agent_name = Column(String(100), nullable=False, index=True)
    role = Column(String(100), nullable=True)
    team_name = Column(String(100), nullable=True, index=True)
    task = Column(Text, nullable=True)
    status = Column(String(50), default="running", index=True)
    lifecycle = Column(String(50), default="oneshot")
    cleanup = Column(String(50), default="delete")
    session_id = Column(String(100), nullable=True)
    pid = Column(Integer, nullable=True)
    started_at = Column(Float, nullable=False, index=True)
    completed_at = Column(Float, nullable=True)
    result = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    servers_json = Column(Text, nullable=True)  # JSON array
    original_config_json = Column(Text, nullable=True)  # JSON object
    runtime_config_json = Column(Text, nullable=True)  # JSON: resolved instruction, skills, tools
    restart_count = Column(Integer, default=0)
    metadata_json = Column(Text, nullable=True)  # JSON object


class AgentPauseStateModel(Base):
    """Persisted pause state for cross-restart recovery.

    PauseController upserts here on every ``_pause_one`` and deletes on
    ``_resume_one``. Covers both in-process agents (Jarvis) and
    subprocess agents — the latter also have ``spawn_records.status =
    'paused'`` for UI/registry queries, but this table is the
    controller's authoritative source for "who was paused when the
    server last ran". On startup, ``PauseController.restore_on_startup``
    re-applies the pauses so a manual pause survives a backend restart.

    No FK to spawn_records — Jarvis has no spawn_record but can still
    be paused. ``agent_name`` is the PK; secondary index on
    ``team_name`` so the orchestrator can ask "which teams have any
    paused member?" cheaply.
    """
    __tablename__ = "agent_pause_state"

    agent_name = Column(String(100), primary_key=True)
    paused_at = Column(Float, nullable=False)
    team_name = Column(String(100), nullable=True, index=True)
    reason = Column(String(50), default="manual")  # 'manual' | 'approval' | 'team'


class McpServerToolModel(Base):
    """Cached MCP server tool metadata — single source of truth.

    Populated from:
    - Spawned agent lifecycle events (runtime_config → subprocess)
    - Static agent aggregators at startup
    """
    __tablename__ = "mcp_server_tools"

    id = Column(Integer, primary_key=True, autoincrement=True)
    server_name = Column(String(100), nullable=False, index=True)
    tool_name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    updated_at = Column(Float, default=lambda: datetime.now().timestamp())


class McpServerModel(Base):
    """MCP server catalog — single source of truth for runtime MCP CRUD.

    Built-in servers are seeded from fastagent.config.yaml on first boot
    (boot-time upsert: insert if missing, never overwrite). User-created
    servers live alongside with is_builtin=False.
    """
    __tablename__ = "mcp_servers"

    name = Column(String(100), primary_key=True)
    transport = Column(String(20), nullable=False)        # 'stdio' | 'http' | 'sse'
    command = Column(String(500), nullable=True)          # null for url-based
    args_json = Column(Text, nullable=True)               # JSON list[str]
    env_json = Column(Text, nullable=True)                # JSON dict[str, str]
    url = Column(Text, nullable=True)                     # null for stdio
    cwd = Column(Text, nullable=True)                     # working dir for stdio subprocess
    is_builtin = Column(Boolean, default=False, nullable=False, index=True)
    created_at = Column(Float, default=lambda: datetime.now().timestamp())
    updated_at = Column(Float, default=lambda: datetime.now().timestamp())


class AgentMcpAttachmentModel(Base):
    """Per-agent MCP server allowlist. Composite PK (agent_name, server_name).

    Seeded on first boot from agent.py @fast.agent(servers=[...]) decorators.
    Subsequent boots read this table; decorator becomes documentation only.
    """
    __tablename__ = "agent_mcp_attachments"

    agent_name = Column(String(100), primary_key=True)
    server_name = Column(String(100), primary_key=True)
    created_at = Column(Float, default=lambda: datetime.now().timestamp())


class McpEventLogModel(Base):
    """Audit log for MCP catalog/attachment lifecycle events.

    One row per audit() context-manager invocation in services.mcp_runtime.
    Used by /api/mcp/events endpoints and the dashboard Events tab.
    """
    __tablename__ = "mcp_event_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(Float, default=lambda: datetime.now().timestamp(), index=True)
    action = Column(String(30), nullable=False, index=True)
    server_name = Column(String(100), nullable=True, index=True)
    agent_name = Column(String(100), nullable=True, index=True)
    actor = Column(String(50), default="user")
    outcome = Column(String(20), nullable=False)          # 'ok' | 'fail'
    duration_ms = Column(Integer, nullable=True)
    detail_json = Column(Text, nullable=True)             # JSON dict


class AgentContextSnapshot(Base):
    """Full agent context window snapshots — single source of truth for audit/resume.

    Each row = one serialized context window captured at a specific trigger point.
    Multiple snapshots per agent form an audit trail (no upsert, append-only).
    """
    __tablename__ = "agent_context_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(100), nullable=False, index=True)
    agent_name = Column(String(100), nullable=False, index=True)
    session_id = Column(String(100), nullable=True, index=True)
    team_name = Column(String(100), nullable=True, index=True)

    # Context data — serialized PromptMessageExtended[] via prompt_serialization.to_json()
    context_json = Column(Text, nullable=False)
    message_count = Column(Integer, default=0)

    # Token stats at snapshot time
    total_input_tokens = Column(Integer, default=0)
    total_output_tokens = Column(Integer, default=0)

    # Trigger: what caused this snapshot
    trigger = Column(String(50), nullable=False)  # task_complete | idle | error | manual

    created_at = Column(Float, default=lambda: datetime.now().timestamp(), index=True)


class TeamTemplateHistory(Base):
    """Audit log for ``team_sessions.template`` edits.

    Append-only: every PATCH / rollback / yaml-reset writes one row. Lets the
    UI show "who changed what when" and supports 1-click rollback. Schema is
    identical to the raw DDL emitted by ``scripts/patch_team_template.py``
    (Phase 0) so audit history is continuous across the migration.

    Field-level granularity (``field`` + ``before_json`` / ``after_json``)
    means we can attribute and rollback individual server / instruction /
    skill / override edits without serialising the whole template each time.
    """
    __tablename__ = "team_template_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(100), nullable=False, index=True)
    # role is the key inside template.roles (e.g. "qe", "dev"). NULL means a
    # template-level edit (e.g. team-wide setting). NOT a foreign key — team
    # sessions can be deleted while we keep the audit trail.
    role = Column(String(100), nullable=True)
    # Specific field inside the role config (e.g. "servers", "instruction",
    # "server_overrides"). NULL means whole-role replace.
    field = Column(String(100), nullable=True)
    before_json = Column(Text, nullable=True)  # JSON-encoded prior value
    after_json = Column(Text, nullable=True)   # JSON-encoded new value
    # 'ui' | 'api' | 'yaml-reset' | 'phase0-script' | 'rollback'
    source = Column(String(50), nullable=False)
    edited_by = Column(String(100), nullable=True, default="system")
    edited_at = Column(Float, default=lambda: datetime.now().timestamp(), index=True)
    comment = Column(Text, nullable=True)


class CronJobModel(Base):
    """Cron job definition — unified cron model (solar/lunar, one-shot/recurring)."""
    __tablename__ = "cron_jobs"

    id = Column(String(100), primary_key=True)
    user_id = Column(String(100), nullable=False, default="default", index=True)
    name = Column(String(255), nullable=False)

    # Schedule
    schedule_cron = Column(String(100), nullable=False)  # 5-field cron expression
    calendar_type = Column(String(10), default="solar")  # solar | lunar
    one_shot = Column(Boolean, default=False)
    lunar_leap = Column(Boolean, default=False)
    schedule_timezone = Column(String(50), default="Asia/Ho_Chi_Minh")

    # Execution
    exec_mode = Column(String(20), nullable=False)  # reminder | agent_turn
    exec_payload = Column(Text, nullable=False)
    exec_agent = Column(String(100), nullable=True)  # target agent for agent_turn

    # State
    status = Column(String(20), default="active", index=True)  # active | paused | disabled | completed
    last_run_at = Column(Float, nullable=True)
    last_result = Column(String(20), nullable=True)  # success | failed | error
    last_error = Column(Text, nullable=True)
    next_run_at = Column(Float, nullable=True, index=True)  # pre-computed

    # Reliability
    run_count = Column(Integer, default=0)
    fail_count = Column(Integer, default=0)  # consecutive
    total_fail = Column(Integer, default=0)  # lifetime

    # Metadata
    created_at = Column(Float, nullable=False, default=lambda: datetime.now().timestamp())
    updated_at = Column(Float, nullable=False, default=lambda: datetime.now().timestamp())
    created_by = Column(String(20), default="user")  # user | agent

    # Approval — SINGLE SOURCE OF TRUTH for "may this job fire?".
    # Decided ONCE at creation time (not at fire time) so an agent-created
    # job can't run unsupervised before a human has vetted its payload —
    # the prompt-injection defence. Dashboard-created jobs are 'approved'
    # immediately (the user is present and in control). Agent-created
    # agent_turn jobs start 'pending' until the user resolves the matching
    # ApprovalRequest card. At fire time the scheduler only READS this flag;
    # it never blocks waiting for a human (see cron_scheduler._execute_agent_turn).
    # Default 'approved' so jobs created before this column existed keep running.
    approval_status = Column(String(20), default="approved")  # approved | pending | rejected


class CronRunModel(Base):
    """Cron job execution history."""
    __tablename__ = "cron_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(100), nullable=False, index=True)

    started_at = Column(Float, nullable=False, index=True)
    completed_at = Column(Float, nullable=True)
    status = Column(String(20), nullable=False, index=True)  # running | success | failed | error | skipped
    duration_ms = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)

    # Output
    result_type = Column(String(20), nullable=True)  # text | files | images | email | notify | mixed
    result_json = Column(Text, nullable=True)
    output_path = Column(String(500), nullable=True)

    attempt = Column(Integer, default=1)
    created_at = Column(Float, default=lambda: datetime.now().timestamp())


class NotificationModel(Base):
    """Notification inbox — aggregates scheduler results, reminders, errors."""
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, nullable=True, index=True)   # FK to cron_runs
    job_id = Column(String(100), nullable=True, index=True)  # FK to cron_jobs
    type = Column(String(20), nullable=False)     # reminder | agent_result | error
    title = Column(String(255), nullable=False)
    preview = Column(String(300), nullable=True)  # plain-text snippet
    content = Column(Text, nullable=True)         # full content (markdown)
    content_type = Column(String(20), default="text")  # text | markdown
    is_read = Column(Integer, default=0, index=True)
    created_at = Column(Float, nullable=False, default=lambda: datetime.now().timestamp(), index=True)
    read_at = Column(Float, nullable=True)
    metadata_json = Column(Text, nullable=True)   # JSON: {agent, duration_ms, exec_mode, status}


class ApprovalRequestModel(Base):
    """Approval request — agent requests user review before proceeding."""
    __tablename__ = "approval_requests"

    id = Column(String(100), primary_key=True)  # UUID
    agent_name = Column(String(100), nullable=False, index=True)
    team_name = Column(String(100), nullable=True, index=True)
    run_id = Column(String(100), nullable=True, index=True)
    conversation_id = Column(String(100), nullable=True)

    # Content
    approval_type = Column(String(50), nullable=False)  # team_plan, architecture, implementation_plan, solution_confirm, budget, deploy, custom
    title = Column(String(500), nullable=False)
    content = Column(Text, nullable=False)
    content_format = Column(String(20), default="text")  # text, markdown, json
    urgency = Column(String(20), default="normal")  # low, normal, high, urgent

    # Status
    status = Column(String(20), default="pending", index=True)  # pending, approved, rejected, cancelled

    # Impact analysis (optional)
    impact_files = Column(Integer, nullable=True)
    impact_services = Column(Integer, nullable=True)
    impact_downtime = Column(String(100), nullable=True)
    impact_risk = Column(String(20), nullable=True)  # low, medium, high, critical

    # Resolution
    user_decision = Column(String(20), nullable=True)  # approve, reject
    user_comment = Column(Text, nullable=True)

    # Team pause tracking
    paused_agents = Column(Text, nullable=True)  # JSON array of agent names

    # Chain (reject → revise → resubmit)
    previous_id = Column(String(100), nullable=True)

    # Timestamps
    created_at = Column(Float, nullable=False, default=lambda: datetime.now().timestamp())
    resolved_at = Column(Float, nullable=True)

    # Extra
    metadata_json = Column(Text, nullable=True)


class MeetingModel(Base):
    """Meeting config and state — single source of truth for meeting data."""
    __tablename__ = "meetings"

    meeting_id = Column(String(100), primary_key=True)
    config_json = Column(Text, nullable=False)   # JSON: agenda, participants, max_rounds, created_by, created_at
    state_json = Column(Text, nullable=False)     # JSON: current_turn, round, joined, ended, outcome
    created_at = Column(Float, default=lambda: datetime.now().timestamp(), index=True)


class MeetingTranscriptModel(Base):
    """Meeting transcript entries — one row per speak/skip turn."""
    __tablename__ = "meeting_transcripts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    meeting_id = Column(String(100), nullable=False, index=True)
    turn = Column(Integer, nullable=False)
    round = Column(Integer, nullable=False)
    agent = Column(String(100), nullable=False)
    message = Column(Text, nullable=True)
    type = Column(String(20), default="speak")   # speak, skip, decision
    created_at = Column(Float, default=lambda: datetime.now().timestamp())


class MeetingEventModel(Base):
    """Cross-process event bus — subprocess writes, bridge polls."""
    __tablename__ = "meeting_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String(50), nullable=False)  # meeting_created, transcript_entry, state_changed, ...
    meeting_id = Column(String(100), nullable=False, index=True)
    data_json = Column(Text, nullable=True)
    created_at = Column(Float, default=lambda: datetime.now().timestamp(), index=True)


class ApprovalCommentModel(Base):
    """Inline comment on an approval request — supports line click and text range selection."""
    __tablename__ = "approval_comments"

    id = Column(String(100), primary_key=True)  # UUID
    approval_id = Column(String(100), nullable=False, index=True)

    # Mode 1: Click line number
    line_number = Column(Integer, nullable=True)

    # Mode 2: Select text range
    selection_start_line = Column(Integer, nullable=True)
    selection_end_line = Column(Integer, nullable=True)
    selection_start_offset = Column(Integer, nullable=True)
    selection_end_offset = Column(Integer, nullable=True)
    selected_text = Column(Text, nullable=True)

    # Comment content
    author = Column(String(100), default="user")  # user or agent name
    body = Column(Text, nullable=False)
    created_at = Column(Float, nullable=False, default=lambda: datetime.now().timestamp())


# --- Settings & Setup Wizard models (Phase 1) ---

# Canonical wizard step order — used by routes/migrations.
SETUP_WIZARD_STEPS = ("auth", "llm", "services", "yaml_config", "verify")
SETUP_WIZARD_CRITICAL_STEPS = ("auth", "llm", "verify")


class SystemConfig(Base):
    """Key-value config store powering the Settings UI.

    Replaces the scattered ``.env`` + YAML knobs for user-managed settings.
    Secrets are encrypted at rest using ``core.secrets_crypto`` (Fernet); the
    ``value`` column stores the urlsafe-base64 ciphertext when ``is_secret`` is
    true, plain text otherwise.
    """
    __tablename__ = "system_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    category = Column(String(50), nullable=False)
    key = Column(String(100), nullable=False)
    value = Column(Text, nullable=True)
    is_secret = Column(Boolean, default=False, nullable=False)
    source = Column(String(20), default="user", nullable=False)  # user | wizard | import | system
    updated_at = Column(
        Float,
        nullable=False,
        default=lambda: datetime.now().timestamp(),
    )
    updated_by = Column(String(50), default="user", nullable=False)

    __table_args__ = (
        UniqueConstraint("category", "key", name="uq_system_config_category_key"),
        Index("ix_system_config_category", "category"),
    )


class SetupWizardStep(Base):
    """Tracks the user's progress through the Setup Wizard.

    Five rows are seeded on first init (one per step in ``SETUP_WIZARD_STEPS``).
    A step is "done" if either ``completed`` or ``skipped`` is true; critical
    steps cannot be skipped (enforced at the API layer).
    """
    __tablename__ = "setup_wizard"

    step_name = Column(String(50), primary_key=True)
    completed = Column(Boolean, default=False, nullable=False)
    skipped = Column(Boolean, default=False, nullable=False)
    completed_at = Column(Float, nullable=True)
    # Step-specific summary (provider chosen, services configured, etc.).
    # Never store raw secrets here — only references / metadata.
    data_json = Column(Text, nullable=True)


class ConfigHistory(Base):
    """Append-only audit log for every ``SystemConfig`` change.

    For secret keys, ``old_value`` and ``new_value`` are masked to ``***`` so
    that historical ciphertext (which may be unreadable after key rotation)
    does not leak into the log.
    """
    __tablename__ = "config_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    category = Column(String(50), nullable=False)
    key = Column(String(100), nullable=False)
    old_value = Column(Text, nullable=True)
    new_value = Column(Text, nullable=True)
    is_secret = Column(Boolean, default=False, nullable=False)
    action = Column(String(20), default="update", nullable=False)  # create | update | delete
    changed_at = Column(
        Float,
        nullable=False,
        default=lambda: datetime.now().timestamp(),
    )
    changed_by = Column(String(50), default="user", nullable=False)

    __table_args__ = (
        Index("ix_config_history_changed_at", "changed_at"),
        Index("ix_config_history_category_key", "category", "key"),
    )


# --- Auth: User + Passkey credentials ---


# Stable id of the seeded single-user row. Hardcoded so all session tokens,
# passkey credentials, and any future user-scoped rows can reference it
# deterministically across restarts and deployments. If/when Jarvis adopts
# real multi-user, this constant becomes the "system owner" id.
DEFAULT_USER_ID = "00000000-0000-0000-0000-000000000001"
DEFAULT_USERNAME = "owner"


class User(Base):
    """One row per deployment for now (``username='owner'``). Schema
    supports many rows so a future multi-user feature is a route change
    rather than a migration."""
    __tablename__ = "users"

    id = Column(String(36), primary_key=True)
    username = Column(String(100), nullable=False, unique=True)
    created_at = Column(Float, default=lambda: datetime.now().timestamp())


class PasskeyCredential(Base):
    """A WebAuthn credential bound to a (user, RP-domain) pair.

    One user can have many credentials (laptop + phone + YubiKey, or
    one per RP domain if they access the deployment from multiple
    origins). Recovery is via the ``JARVIS_API_KEY`` in ``.env`` — no
    backup codes table.
    """
    __tablename__ = "passkey_credentials"

    # Base64url-encoded credential id returned by the authenticator.
    # WebAuthn spec allows up to 1023 bytes; base64url expansion ~1.37x.
    id = Column(String(1400), primary_key=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    # CBOR-encoded COSE public key (raw bytes from the attestation).
    public_key = Column(LargeBinary, nullable=False)
    # Authenticator-reported counter; must be strictly increasing across
    # successful assertions or we treat the credential as cloned.
    sign_count = Column(Integer, default=0, nullable=False)
    # JSON-encoded transports hint (e.g. ``["internal","hybrid"]``) —
    # passed back to the client in allowCredentials so the browser picks
    # the right authenticator UX (Touch ID vs QR vs USB).
    transports = Column(Text, nullable=True)
    # RP ID at registration time. Stored so we can later filter
    # credentials by the request's RP ID (a passkey registered on
    # ``localhost`` must NOT be offered on ``jarvis.alice.com``).
    rp_id = Column(String(255), nullable=False)
    # Human label set by the user ("MacBook Touch ID", "iPhone").
    label = Column(String(100), nullable=True)
    created_at = Column(Float, default=lambda: datetime.now().timestamp())
    last_used_at = Column(Float, nullable=True)

    __table_args__ = (
        Index("ix_passkey_user", "user_id"),
        Index("ix_passkey_rp", "user_id", "rp_id"),
    )


# ──────────────────────────────────────────────────────────────────────
# Agent memory + adaptive RAG (docs/agent-memory-adaptive-rag-spec.md)
#
# SQLite is the source of truth for all memory state; Qdrant is a
# rebuildable index. Every table keys on ``owner_agent_name`` (the
# normalized agent name — see helpers/agent_identity.py); there is no
# team/global memory. Schemas mirror spec §12 exactly.
# ──────────────────────────────────────────────────────────────────────


class MemoryRecord(Base):
    """A durable memory owned by exactly one agent (pinned/episodic/
    semantic/procedural). ``content`` is the current text; immutable history
    lives in ``memory_versions``; provenance in ``memory_sources``."""
    __tablename__ = "memory_records"

    id = Column(String(100), primary_key=True)
    owner_agent_name = Column(String(100), nullable=False, index=True)
    memory_type = Column(String(30), nullable=False, index=True)
    memory_subtype = Column(String(50), nullable=True)
    subject_scope = Column(String(120), nullable=False, index=True)
    content = Column(Text, nullable=False)
    normalized_content = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="active", index=True)
    importance = Column(Float, nullable=False, default=0.5)
    confidence = Column(Float, nullable=False, default=0.5)
    authority = Column(String(30), nullable=False)
    sensitivity = Column(String(20), nullable=False, default="normal")
    pinned = Column(Integer, nullable=False, default=0)
    valid_from = Column(Float, nullable=True)
    valid_until = Column(Float, nullable=True)
    entities_json = Column(Text, nullable=True)   # [{name,etype}] for the graph (memory v2)
    current_version = Column(Integer, nullable=False, default=1)
    created_at = Column(Float, nullable=False, default=lambda: datetime.now().timestamp())
    updated_at = Column(Float, nullable=False, default=lambda: datetime.now().timestamp())

    __table_args__ = (
        Index("ix_memory_owner_status", "owner_agent_name", "status"),
        Index("ix_memory_owner_type", "owner_agent_name", "memory_type"),
    )


class MemoryVersion(Base):
    """Immutable version row — one per change to a ``memory_records`` row."""
    __tablename__ = "memory_versions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    memory_id = Column(String(100), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    metadata_json = Column(Text, nullable=False, default="{}")
    change_type = Column(String(30), nullable=False)  # create|update|supersede|archive|rollback
    changed_by = Column(String(50), nullable=False)
    reason = Column(Text, nullable=True)
    created_at = Column(Float, nullable=False, default=lambda: datetime.now().timestamp())

    __table_args__ = (
        UniqueConstraint("memory_id", "version", name="uq_memory_version"),
    )


class MemorySource(Base):
    """Provenance edge: which source authorized a memory (version)."""
    __tablename__ = "memory_sources"

    id = Column(Integer, primary_key=True, autoincrement=True)
    memory_id = Column(String(100), nullable=False, index=True)
    memory_version = Column(Integer, nullable=False)
    source_type = Column(String(40), nullable=False)
    source_id = Column(String(200), nullable=False)
    source_agent_name = Column(String(100), nullable=True)
    source_excerpt = Column(Text, nullable=True)
    source_hash = Column(String(80), nullable=True)
    source_timestamp = Column(Float, nullable=True)
    authority = Column(String(30), nullable=False)
    created_at = Column(Float, nullable=False, default=lambda: datetime.now().timestamp())


class MemoryCandidate(Base):
    """A proposed memory awaiting deterministic/curator/approval resolution.
    ``status`` is the ONE authoritative candidate state (approval rows are
    input events, never a second source of truth)."""
    __tablename__ = "memory_candidates"

    id = Column(String(100), primary_key=True)
    owner_agent_name = Column(String(100), nullable=False, index=True)
    candidate_type = Column(String(40), nullable=False)
    payload_json = Column(Text, nullable=False)
    source_refs_json = Column(Text, nullable=False, default="[]")
    status = Column(String(30), nullable=False, default="pending", index=True)
    confidence = Column(Float, nullable=False, default=0.5)
    requires_curator = Column(Integer, nullable=False, default=0)
    requires_approval = Column(Integer, nullable=False, default=0)
    dedupe_key = Column(String(120), nullable=True, index=True)
    created_at = Column(Float, nullable=False, default=lambda: datetime.now().timestamp())
    resolved_at = Column(Float, nullable=True)
    resolution_json = Column(Text, nullable=True)


class EpisodicDocument(Base):
    """Immutable, hash-verified, SELF-CONTAINED search projection of
    authorized historical data. ``content`` is the durable record served to
    retrieval; it is never re-derived from mutable session files."""
    __tablename__ = "episodic_documents"

    id = Column(String(100), primary_key=True)
    owner_agent_name = Column(String(100), nullable=False, index=True)
    session_id = Column(String(100), nullable=True, index=True)
    run_id = Column(String(100), nullable=True)
    document_type = Column(String(40), nullable=False)
    source_id = Column(String(200), nullable=False)
    content = Column(Text, nullable=False)
    metadata_json = Column(Text, nullable=False, default="{}")
    content_hash = Column(String(80), nullable=False, index=True)
    created_at = Column(Float, nullable=False, default=lambda: datetime.now().timestamp())
    indexed_revision = Column(Integer, nullable=False, default=0)


class MemoryIndexOutbox(Base):
    """Transactional outbox: index intents written in the SAME transaction as
    the domain write, drained by the background index worker. The UNIQUE
    constraint makes re-enqueue of a revision idempotent."""
    __tablename__ = "memory_index_outbox"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String(40), nullable=False)
    aggregate_id = Column(String(100), nullable=False)
    aggregate_revision = Column(Integer, nullable=False)
    payload_json = Column(Text, nullable=False, default="{}")
    status = Column(String(20), nullable=False, default="pending", index=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(Float, nullable=False, default=0.0, index=True)
    last_error = Column(Text, nullable=True)
    lease_expires_at = Column(Float, nullable=True)  # in_progress lease for crash recovery
    created_at = Column(Float, nullable=False, default=lambda: datetime.now().timestamp())
    completed_at = Column(Float, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "event_type", "aggregate_id", "aggregate_revision",
            name="uq_outbox_event",
        ),
    )


class RetrievalRun(Base):
    """Telemetry per retrieval — level distribution, latency, token cost."""
    __tablename__ = "retrieval_runs"

    id = Column(String(100), primary_key=True)
    owner_agent_name = Column(String(100), nullable=False, index=True)
    session_id = Column(String(100), nullable=True)
    run_id = Column(String(100), nullable=True)
    query_hash = Column(String(80), nullable=False)
    mode = Column(String(20), nullable=False)
    route_json = Column(Text, nullable=False, default="{}")
    filters_json = Column(Text, nullable=False, default="{}")
    result_ids_json = Column(Text, nullable=False, default="[]")
    used_evidence_ids_json = Column(Text, nullable=True)
    bm25_ms = Column(Integer, nullable=True)
    dense_ms = Column(Integer, nullable=True)
    rerank_ms = Column(Integer, nullable=True)
    total_ms = Column(Integer, nullable=False, default=0)
    evidence_tokens = Column(Integer, nullable=False, default=0)
    planner_input_tokens = Column(Integer, nullable=False, default=0)
    planner_output_tokens = Column(Integer, nullable=False, default=0)
    cache_hit = Column(Integer, nullable=False, default=0)
    status = Column(String(20), nullable=False, default="ok")
    error_message = Column(Text, nullable=True)
    created_at = Column(Float, nullable=False, default=lambda: datetime.now().timestamp(), index=True)


class CommunicationRecord(Base):
    """Persisted inter-agent communication (email) so it can be authorized
    and indexed as an episodic source. Meetings/injections live in their own
    existing tables; this fills the email gap (spec §14)."""
    __tablename__ = "communication_records"

    id = Column(String(100), primary_key=True)
    channel = Column(String(30), nullable=False)  # email | ...
    sender = Column(String(200), nullable=False)
    recipients_json = Column(Text, nullable=False, default="[]")
    subject = Column(Text, nullable=True)
    body = Column(Text, nullable=False)
    source_ref = Column(String(200), nullable=True)
    created_at = Column(Float, nullable=False, default=lambda: datetime.now().timestamp(), index=True)


def get_db():
    """Dependency for getting database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Initialize database tables and run lightweight migrations."""
    import json
    import logging
    logger = logging.getLogger(__name__)
    
    Base.metadata.create_all(bind=engine)
    from sqlalchemy import text

    # Enable WAL mode for concurrent access
    with engine.connect() as conn:
        try:
            conn.execute(text("PRAGMA journal_mode=WAL"))
            conn.commit()
        except Exception:
            pass

    # Lightweight migration: add columns that create_all won't add to existing tables
    with engine.connect() as conn:
        migrations = [
            "ALTER TABLE books ADD COLUMN last_played_at FLOAT",
            "ALTER TABLE crawl_jobs ADD COLUMN params TEXT",
            "ALTER TABLE spawn_records ADD COLUMN runtime_config_json TEXT",
            "ALTER TABLE agent_activities ADD COLUMN session_id VARCHAR(100)",
            "ALTER TABLE mcp_servers ADD COLUMN cwd TEXT",
            # Pre-existing cron jobs predate creation-time approval — default
            # them to 'approved' so the new gate doesn't silently freeze jobs
            # that were already running.
            "ALTER TABLE cron_jobs ADD COLUMN approval_status VARCHAR(20) DEFAULT 'approved'",
            "ALTER TABLE memory_records ADD COLUMN entities_json TEXT",
            "ALTER TABLE token_usage ADD COLUMN category VARCHAR(40) DEFAULT 'agent'",
        ]
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                # Column already exists, skip
                conn.rollback()

    # Memory degraded-search index (FTS5). Single virtual table fed from both
    # episodic_documents.content and memory_records.normalized_content by the
    # indexing layer. It is the degraded fallback / admin search / consistency
    # reference — never the production search path (that is Qdrant). Wrapped in
    # try/except because a stripped SQLite build may lack FTS5; memory then
    # degrades to Qdrant-only with no SQLite fallback (surfaced at runtime).
    with engine.connect() as conn:
        try:
            conn.execute(text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5("
                "doc_kind UNINDEXED, doc_id UNINDEXED, "
                "owner_agent_name UNINDEXED, content)"
            ))
            conn.commit()
        except Exception:
            conn.rollback()
            logger.warning("[MEMORY] FTS5 unavailable — SQLite degraded search disabled")

    # --- One-time data migration: JSON files → SQLite ---
    _migrate_story_providers(logger)
    _migrate_story_metadata(logger)
    _cleanup_legacy_files(logger)
    _seed_setup_wizard(logger)
    _seed_default_user(logger)
    _backfill_mcp_cwd(logger)


def _seed_default_user(logger):
    """Ensure the single-deployment owner row exists; idempotent."""
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.id == DEFAULT_USER_ID).first()
        if existing:
            return
        db.add(User(id=DEFAULT_USER_ID, username=DEFAULT_USERNAME))
        db.commit()
        logger.info("[INIT] Seeded default user: %s", DEFAULT_USERNAME)
    except Exception as exc:
        logger.error("[INIT] Failed to seed default user: %s", exc)
        db.rollback()
    finally:
        db.close()


def _seed_setup_wizard(logger):
    """Ensure one row per wizard step exists; idempotent on every boot."""
    db = SessionLocal()
    try:
        existing = {row.step_name for row in db.query(SetupWizardStep).all()}
        added = []
        for step_name in SETUP_WIZARD_STEPS:
            if step_name in existing:
                continue
            db.add(SetupWizardStep(step_name=step_name))
            added.append(step_name)
        if added:
            db.commit()
            logger.info("[INIT] Seeded setup_wizard rows: %s", added)
    except Exception as exc:
        logger.error("[INIT] Failed to seed setup_wizard: %s", exc)
        db.rollback()
    finally:
        db.close()


def _migrate_story_providers(logger):
    """Migrate data/story_providers.json → story_providers table (one-time)."""
    import json
    json_path = os.path.join(DATA_DIR, "story_providers.json")
    if not os.path.exists(json_path):
        return
    
    db = SessionLocal()
    try:
        # Check if already migrated
        existing = db.query(StoryProvider).count()
        if existing > 0:
            # Already have data, rename JSON as backup
            bak_path = json_path + ".bak"
            if not os.path.exists(bak_path):
                os.rename(json_path, bak_path)
                logger.info("[MIGRATION] story_providers.json → .bak (DB already has data)")
            return
        
        with open(json_path, "r", encoding="utf-8") as f:
            providers = json.load(f)
        
        for p in providers:
            selectors = p.get("selectors", {})
            provider = StoryProvider(
                domain=p["domain"],
                name=p.get("name", p["domain"]),
                selectors_json=json.dumps(selectors, ensure_ascii=False),
                search_url=p.get("search_url"),
                list_selector=p.get("list_selector"),
                title_selector=p.get("title_selector"),
                trust_level=p.get("trust_level", "auto-learned"),
                known_stories_json=json.dumps(p.get("known_stories", []), ensure_ascii=False),
            )
            db.add(provider)
        
        db.commit()
        logger.info(f"[MIGRATION] Migrated {len(providers)} providers from JSON → DB")
        
        # Rename original as backup
        os.rename(json_path, json_path + ".bak")
    except Exception as e:
        logger.error(f"[MIGRATION] story_providers migration failed: {e}")
        db.rollback()
    finally:
        db.close()


def _migrate_story_metadata(logger):
    """Migrate per-story metadata.json → story_meta table (one-time)."""
    import json
    stories_dir = os.path.join(DATA_DIR, "stories")
    if not os.path.exists(stories_dir):
        return
    
    db = SessionLocal()
    try:
        for story_name in os.listdir(stories_dir):
            story_path = os.path.join(stories_dir, story_name)
            if not os.path.isdir(story_path) or story_name.startswith('.'):
                continue
            
            meta_file = os.path.join(story_path, "metadata.json")
            
            # Check if already in DB
            existing = db.query(StoryMeta).filter(StoryMeta.story_id == story_name).first()
            if existing:
                # Already migrated, clean up JSON if exists
                if os.path.exists(meta_file):
                    os.remove(meta_file)
                continue
            
            # Read metadata
            title = story_name
            source_url = None
            if os.path.exists(meta_file):
                try:
                    with open(meta_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    title = data.get("title", story_name)
                    source_url = data.get("source")
                except Exception:
                    pass
            
            # Count chapters
            chapter_count = len([
                f for f in os.listdir(story_path)
                if f.endswith(".txt") and not f.startswith(".")
            ])
            
            meta = StoryMeta(
                story_id=story_name,
                title=title,
                source_url=source_url,
                chapter_count=chapter_count,
            )
            db.add(meta)
            
            # Clean up JSON file
            if os.path.exists(meta_file):
                os.remove(meta_file)
        
        db.commit()
        logger.info("[MIGRATION] Story metadata migrated to DB")
    except Exception as e:
        logger.error(f"[MIGRATION] story_meta migration failed: {e}")
        db.rollback()
    finally:
        db.close()


def _cleanup_legacy_files(logger):
    """Remove orphaned legacy files that are no longer used by any code."""
    legacy_files = [
        os.path.join(DATA_DIR, "tts_cache.json"),
        os.path.join(DATA_DIR, "chat_history.json"),
    ]
    for f in legacy_files:
        if os.path.exists(f):
            try:
                os.remove(f)
                logger.info(f"[CLEANUP] Removed legacy file: {os.path.basename(f)}")
            except Exception as e:
                logger.warning(f"[CLEANUP] Failed to remove {f}: {e}")
    
    # Warn about library_media (66MB, needs manual confirmation)
    media_dir = os.path.join(DATA_DIR, "library_media")
    if os.path.exists(media_dir):
        logger.warning(
            f"[CLEANUP] Legacy directory exists: {media_dir} — "
            "no code references this. Consider removing manually."
        )


def _backfill_mcp_cwd(logger):
    """Backfill mcp_servers.cwd for self-authored servers promoted before the
    column existed. Only updates rows where cwd IS NULL and the command points
    inside .fast-agent/mcp_workspace/generated/<name>/. Idempotent.
    """
    from pathlib import Path
    from sqlalchemy import text
    workspace = Path(__file__).parent.parent / ".fast-agent" / "mcp_workspace" / "generated"
    if not workspace.exists():
        return
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT name FROM mcp_servers "
                "WHERE cwd IS NULL "
                "AND command LIKE '%/mcp_workspace/generated/%'"
            )).fetchall()
            updated = 0
            for (name,) in rows:
                sdir = workspace / name
                if sdir.exists() and (sdir / "server.py").exists():
                    conn.execute(
                        text("UPDATE mcp_servers SET cwd = :c WHERE name = :n"),
                        {"c": str(sdir), "n": name},
                    )
                    updated += 1
            if updated:
                conn.commit()
                logger.info(f"[MIGRATION] Backfilled cwd for {updated} generated MCP server(s)")
    except Exception as e:
        logger.warning(f"[MIGRATION] mcp_servers cwd backfill failed: {e}")


def get_db_session():
    """Get a database session (non-generator version)."""
    return SessionLocal()
