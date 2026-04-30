---
name: agent-card-hooks
description: Guide for implementing fast-agent hooks. Use when adding hook functions to agent cards or Python code for tasks like history compaction, saving sessions, modifying tool calls, or managing agent lifecycle.
---
# Implement agent hooks
Hooks let you intercept and customize agent behavior at specific points in the
tool loop (per-turn) or agent lifecycle (start/shutdown).
**Audience**: Developers adding custom hook logic to fast-agent agents.
## Quick workflow
1. Identify which hook points you need (tool loop vs lifecycle).
2. Implement async hook functions with the correct context type.
3. Wire hooks via agent card YAML or Python class assignment.
4. Test hooks with the smoke test script or real agents. You may need the User to assist with this step.
## Hook points
### Tool loop hooks
Run during the LLM/tool execution cycle. Configure via `tool_hooks:` in agent cards
or `ToolRunnerHooks` in Python.
| Hook | When it fires |
| --------------------- | ----------------------------------------------------- |
| `before_llm_call` | Before each LLM call (receives pending messages) |
| `after_llm_call` | After each assistant response |
| `before_tool_call` | Before executing tool calls |
| `after_tool_call` | After tool results are received |
| `after_turn_complete` | Once after the turn finishes (stop reason ≠ TOOL_USE) |
### Lifecycle hooks
Run when agent instances start or shut down. Configure via `lifecycle_hooks:` in
agent cards or `AgentLifecycleHooks` in Python.
| Hook | When it fires |
| ------------- | --------------------------- |
| `on_start` | During agent initialization |
| `on_shutdown` | During agent shutdown |
### Built-in shortcut
Set `trim_tool_history: true` in agent cards to apply the history trimmer after each turn.
## Approach 1: Agent card YAML
Reference hook functions using `module.py:function` specs. Paths are resolved
relative to the agent card location.
```yaml
tool_hooks:
 before_llm_call: hooks.py:log_pending_messages
 after_turn_complete: hooks.py:save_after_turn
lifecycle_hooks:
 on_start: hooks.py:start_service
 on_shutdown: hooks.py:stop_service
```
Hook functions must be `async def` with the appropriate context type:
```python
# hooks.py
from fast_agent.hooks import HookContext, AgentLifecycleContext
async def save_after_turn(ctx: HookContext) -> None:
 if ctx.is_turn_complete:
   history = ctx.message_history
   ctx.load_message_history(history[-10:])
async def start_service(ctx: AgentLifecycleContext) -> None:
 ctx.agent._service_handle = "started"
```
## Approach 2: Python classes
For programmatic control, assign hooks directly to agent instances or use the
dataclass constructors.
```python
from fast_agent.agents.tool_runner import ToolRunnerHooks
from fast_agent.hooks.lifecycle_hook_loader import AgentLifecycleHooks
# Tool loop hooks
async def my_after_turn(runner, message):
 print(f"Turn complete: {message.stop_reason}")
hooks = ToolRunnerHooks(after_turn_complete=my_after_turn)
agent.tool_runner_hooks = hooks
# Lifecycle hooks
async def my_on_start(ctx):
 print(f"Agent {ctx.agent_name} starting")
lifecycle = AgentLifecycleHooks(on_start=my_on_start)
```
## Hook context objects
### HookContext (tool hooks via agent cards)
```python
from fast_agent.hooks import HookContext
async def my_hook(ctx: HookContext) -> None:
 ctx.agent_name       # Agent name
 ctx.iteration        # Current tool loop iteration
 ctx.is_turn_complete # True if stop_reason != TOOL_USE
 ctx.message_history  # Current message history
 ctx.message          # The message that triggered this hook
 ctx.hook_type        # "before_llm_call", "after_turn_complete", etc.
 ctx.usage            # Token usage stats (UsageAccumulator | None)
 ctx.context          # Agent's Context object if available
 ctx.get_agent(name)  # Look up another agent by name
 ctx.load_message_history(messages)  # Replace history
```
### AgentLifecycleContext (lifecycle hooks)
```python
from fast_agent.hooks import AgentLifecycleContext
async def my_hook(ctx: AgentLifecycleContext) -> None:
 ctx.agent_name   # Agent name
 ctx.agent        # The agent instance
 ctx.context      # Context object (or None)
 ctx.config       # AgentConfig
 ctx.hook_type    # "on_start" or "on_shutdown"
 ctx.has_context  # True if context is available
 ctx.get_agent(name)  # Look up another agent by name
```
## Built-in hooks
| Hook | Import | Purpose |
| ------------------------ | ------------------ | ------------------------------- |
| `trim_tool_loop_history` | `fast_agent.hooks` | Compact tool call/result pairs |
| `save_session_history` | `fast_agent.hooks` | Save history to session storage |
## Source
- Repository: https://github.com/fast-agent-ai/skills
- Path: skills/agent-card-hooks/
