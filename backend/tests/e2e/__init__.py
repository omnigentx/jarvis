"""E2E integration tests for Jarvis — deterministic LLM + real code paths.

See README.md in this directory for the full pattern guide, fixture schema,
and checklist for adding new tests.

Quick primitives:
    build_scripted_agent()      — in-process agent wired to a YAML fixture
    run_scripted_subprocess()   — real isolated_runner subprocess with ScriptedLLM
    ToolCallRecorder()          — strict (name, args) sequence assertion
    ScriptedLLM                 — raises on exhaustion; never silently fills
"""
