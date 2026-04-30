# Jarvis E2E Test Harness

> Deterministic integration tests. Scripted LLM + real tools + real subprocess.
> Designed so both humans and AI agents can add new test cases correctly.

## Why this exists

Jarvis is an LLM-driven agent system. Traditional unit tests can't catch
regressions in the paths where LLM decisions, tool-call arguments, and
subprocess env-var propagation all interact — those are exactly the paths
that break most often in production.

The E2E harness fixes this by **scripting** the LLM side of the loop while
leaving **everything else real**: real tool functions, real MCP subprocess
spawn, real context persistence. If your code change alters what args the
LLM is "supposed" to send, what a tool returns, or how env vars flow into
a child subprocess, an E2E test fails with a specific diff.

## Core primitives

| File | Purpose |
|---|---|
| `scripted_llm.py` | `ScriptedLLM` — reads YAML fixture, returns scripted assistant turns (including tool_calls). Strict: exhaustion raises. |
| `harness.py` | `build_scripted_agent()` (in-process), `run_scripted_subprocess()` (real subprocess), `ToolCallRecorder` (asserts call sequence). |
| `subprocess_entry.py` | Subprocess entrypoint: registers `ScriptedLLM` as the `playback` model class, then hands off to `fast_agent.spawn.isolated_runner`. |
| `fixtures/*.yaml` | One file per flow. Describes the LLM turns: content, tool_calls, and args. |

## Fixture schema

```yaml
# turns: each item is one assistant response from the LLM.
# If `tool_calls` is present, stop_reason is TOOL_USE; otherwise END_TURN.

turns:
  - tool_calls:
      call_1:                     # opaque id; any string, unique per turn
        name: my_tool              # REQUIRED
        arguments:                 # OPTIONAL (defaults to {})
          arg_a: "value"
          arg_b: 42
    content: ""                    # OPTIONAL — assistant text alongside the call

  - tool_calls:
      call_2:
        name: another_tool
        arguments: {}

  - content: "final user-facing answer"     # terminal turn, no tool_calls
```

Validation is strict: unknown keys at the turn or tool-call level raise at
load time. Mis-spelling `arguments` as `argumants` fails fixture load, not
at assertion time.

## Writing a new E2E test — 3 steps

### Step 1: Pick a real flow you want to protect

Look for paths that:
- Have broken before (see git log / incident notes)
- Span multiple agents, multiple tools, or a subprocess boundary
- Involve credentials, env vars, or context persistence

**Do not** write tests for code paths you fully control in-module —
existing unit tests in `tests/test_services/`, `tests/test_core/`,
`tests/test_routes/` are better for that.

### Step 2: Author the fixture

Drop `fixtures/<descriptive_flow_name>.yaml`. Keep it short:
one fixture = one focused flow.

### Step 3: Write the test

Pattern for **in-process** tests (fast, good for tool-call logic):

```python
import pytest
from pathlib import Path
from tests.e2e.harness import ToolCallRecorder, build_scripted_agent

FIXTURES = Path(__file__).parent / "fixtures"

@pytest.fixture
def seeded_data(tmp_path, monkeypatch):
    # Set up whatever real state your real tool needs.
    # Example: create files under tmp_path, monkeypatch module-level DATA_DIR.
    import tools.my_server as my_server
    monkeypatch.setattr(my_server, "DATA_DIR", str(tmp_path))

@pytest.mark.asyncio
async def test_my_flow(seeded_data):
    from tools.my_server import my_real_tool  # REAL tool, not a stub

    recorder = ToolCallRecorder()
    agent = await build_scripted_agent(
        fixture_path=FIXTURES / "my_flow.yaml",
        tools=[my_real_tool],
        agent_name="MyAgent",
        recorder=recorder,
    )

    final = await agent.generate("user prompt that triggers the flow")

    recorder.assert_matches([
        ("my_real_tool", {"expected_arg": "expected_value"}),
    ])
    assert "expected substring" in final.last_text()
```

Pattern for **subprocess** tests (when you care about env vars, process
boundary, or context persistence):

```python
import pytest
from pathlib import Path
from tests.e2e.harness import run_scripted_subprocess

FIXTURES = Path(__file__).parent / "fixtures"

@pytest.mark.slow
def test_subprocess_flow(tmp_path):
    result = run_scripted_subprocess(
        fixture_path=FIXTURES / "subprocess_flow.yaml",
        task="user task",
        tmp_path=tmp_path,
        extra_env={
            "TEAM_MY_NAME": "Linh [PM]",
            "TEAM_MY_ROLE": "pm",
        },
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

    snaps = result.context_snapshots()
    assert snaps[-1]["agent_name"] == "Linh [PM]"
```

## The five rules

Every E2E test must follow these or it's not pulling its weight.

1. **Use the REAL production code.** Import real tools from `tools/`,
   real services from `services/`, real hooks from `agent.py`. Stubbing
   the thing you want to protect defeats the point.

2. **Assert specific contracts, not just "it ran".** Check tool args by
   name AND value. Check response fields by key. A test that asserts
   `result is not None` is smoke, not regression.

3. **Include a negative control when behavior is conditional.** If your
   test proves "X happens when Y is present", add a second test proving
   "X does NOT happen when Y is absent". See `test_tag_relay_hook.py`.

4. **Fail loud on silent failures.** Explicitly assert that error paths
   produce error-shaped results, not success-shaped empty ones. See
   `test_credential_missing.py` and `test_subprocess_spawn.py`.

5. **One fixture = one flow.** Do not pile multiple scenarios into one
   YAML. Small files are easier to read, diff, and reuse.

## Anti-patterns (don't do this)

- **Stubbing the real tool with a fake.** If you monkeypatch the function
  you're testing to return a canned value, the test only verifies the
  fixture matches itself.
- **Asserting on fixture values instead of real output.** `assert
  final.last_text() == fixture.turns[-1].content` is tautological.
- **Writing "comprehensive" tests that touch 5+ flows.** Break them up.
- **Adding retries, timeouts, or flakiness tolerance.** If the test is
  non-deterministic, something is wrong with the scripting.
- **Committing fixtures generated from live API calls without trimming.**
  Fixtures should be hand-reviewed; anything that adds noise should be
  removed.

## Checklist for AI agents creating tests

When an AI agent is asked to add an E2E test, it should verify each:

- [ ] Fixture file is in `fixtures/`, filename matches the flow
- [ ] Fixture uses only `content` and `tool_calls` keys at turn level
- [ ] Tool names in fixture match the actual callable names (no typos)
- [ ] Tool arguments in fixture match the real function signature
- [ ] Test imports the REAL tool/service — no inline stubs of the SUT
- [ ] For tools reading module-level paths (e.g. `DATA_DIR`), test uses
      `monkeypatch.setattr(module, "DATA_DIR", str(tmp_path))`
- [ ] `ToolCallRecorder.assert_matches(...)` lists expected calls in order
- [ ] Final assertion checks actual tool output or message history —
      not just the scripted fixture value
- [ ] Subprocess tests use `@pytest.mark.slow`
- [ ] Subprocess tests assert `returncode == 0` AND something specific
      about `result.result` / `result.context_snapshots()`
- [ ] If the flow has a conditional branch, add a negative-control test
- [ ] Run `uv run pytest tests/e2e/<new_test>.py -v` and confirm pass

## When to write a new test

| Situation | Write an E2E test? |
|---|---|
| Fixed a bug in a tool's JSON response shape | YES |
| Added a new tool that's called by agents | YES, if agents rely on specific args/response |
| Added new env var that subprocess needs | YES, verify propagation + snapshot |
| Added a new hook (before_llm_call / after_tool_call) | YES, hook firing is fragile |
| Refactored internal tool logic, same contract | NO, unit test in `test_services/` is better |
| Pure FastAPI route change | NO, unit test with `app_client` fixture |

## Known gaps (work to do)

- **Transcript extractor** (`scripts/extract_transcript.py`): read
  `agent_context_snapshots` from a prod DB and emit a fixture YAML. Build
  when authoring fixtures by hand starts to hurt.
- **Full team spawn with 2+ subprocess agents**: current
  `test_team_spawn.py` spawns one. A test that spawns PM → Dev and
  verifies team_communicate message flow is the logical next step.
- **SSE event shape snapshots**: use `syrupy` or similar to guard frontend
  contract (`token_delta`, `team_spawned`, etc.).
- **Crawler provider fixtures**: scrape once, record HTML, replay.
  Truyenzing URL format drift is a historical pain point.
