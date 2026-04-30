---
name: compaction-strategies
description: Implement hook-based history compaction in fast-agent (rolling window, context threshold truncation, tool-result clearing, summary prompt compaction). Use when adding or extending compaction hooks and wiring them into agent cards.
---
# Compaction strategies (hooks)
Implement and wire compaction hooks via `after_turn_complete`.
## Quick start
- Copy `scripts/compaction_hooks.py` into your project hooks module.
- Pick a strategy and wire it via `tool_hooks.after_turn_complete`.
- Tune defaults in the hook or wrap it in a custom function (hook specs do not accept params).
### Agent card wiring
```yaml
tool_hooks:
 after_turn_complete: hooks.py:rolling_window
```
## Strategy menu
- **Rolling window** → `rolling_window`
- **Truncate over threshold** → `truncate_over_threshold`
- **Clear results (soft)** → `clear_results_soft`
- **Clear results (hard)** → `clear_results_hard`
- **Compaction prompt** → `compaction_prompt`
## Hook requirements
- Guard with `if not ctx.is_turn_complete: return`.
- Use `ctx.usage` when available to read context usage.
- Use `ctx.load_message_history(...)` to replace history.
- Use `show_hook_message(...)` to display a compaction notice.
## Source
- Repository: https://github.com/fast-agent-ai/skills
- Path: skills/compaction-strategies/
