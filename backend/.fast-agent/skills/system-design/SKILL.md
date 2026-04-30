---
name: system-design
description: "RESERVED — Future SA role. Guide for system-level design including architecture diagrams, component boundaries, API contracts, and scalability considerations."
# STATUS: Reserved for future SA (Solution Architect) role. Not currently assigned.
---

# System Design (SA)

> **⚠️ RESERVED:** This skill is prepared for the future SA role. Not currently in use.

## Purpose

Guide the Solution Architect role in:
- Designing system-level architecture
- Defining component boundaries and interfaces
- Creating API contracts
- Evaluating scalability and performance trade-offs

## Architecture Design Process

### 1. Understand Requirements
- Functional requirements (what the system does)
- Non-functional requirements (performance, scalability, security)
- Constraints (technology, budget, timeline)

### 2. Define Components
- Identify major subsystems
- Draw boundaries using bounded contexts (DDD)
- Define interfaces between components
- Minimize coupling, maximize cohesion

### 3. Design APIs
- Define contracts (input/output schemas)
- Choose communication patterns (sync/async, REST/events)
- Version strategy
- Error handling strategy

### 4. Evaluate Trade-offs
- CAP theorem considerations
- Consistency vs. availability
- Simplicity vs. flexibility
- Build vs. buy

### 5. Document
- Architecture decision records (ADRs)
- Component diagrams
- Sequence diagrams for key flows
- API specifications

## In Meeting Context

As SA in a meeting:
- Present architecture proposals with diagrams
- Review DEV's implementation against architecture
- Evaluate technical debt and propose solutions
- Make technology decisions when team needs guidance
