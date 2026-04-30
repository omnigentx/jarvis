---
name: brainstorming
description: Structured approach to explore ideas, clarify requirements, and propose designs before implementation. Use before any creative or planning work.
---

# Brainstorming Ideas Into Designs

Inspired by [obra/superpowers](https://github.com/obra/superpowers/tree/main/skills/brainstorming).

Turn rough ideas into fully-formed designs through structured exploration.

**Core rule:** Do NOT implement anything until the design is agreed upon.

## Process

### 1. Understand Context
- Review current project state (files, docs, existing code)
- Assess scope — is this one task or multiple sub-projects?
- If too large, decompose into smaller pieces first

### 2. Ask Clarifying Questions
- One question at a time
- Prefer multiple-choice when possible
- Focus on: purpose, constraints, success criteria

### 3. Explore Approaches
- Propose 2-3 different approaches with trade-offs
- Lead with your recommendation and explain why
- Be honest about complexity vs. simplicity

### 4. Present Design
- Architecture overview
- Components and their responsibilities
- Data flow
- Error handling strategy
- Testing approach

### 5. Get Agreement
- Present each section, confirm before moving on
- Be ready to revise based on feedback
- Document the final design

## Key Principles

- **YAGNI ruthlessly** — Remove features that aren't needed yet
- **Explore alternatives** — Always propose 2-3 approaches
- **Incremental validation** — Confirm each section
- **Design for isolation** — Each component should have one clear purpose
- **Smaller units** — Easier to understand, test, and modify

## In Multi-Agent Context

When brainstorming in a meeting:
- Use `speak()` to propose ideas and ask questions
- Wait for other agents' input before finalizing
- Summarize the agreed design before moving to implementation
