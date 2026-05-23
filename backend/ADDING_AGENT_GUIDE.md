# Guide to Adding a New Agent to Jarvis (Standard Process)

This document standardizes the steps for adding a new Agent to the Jarvis system (`fast-agent` v0.4.40). Follow this **exact order** to ensure the Agent works smoothly within the Master Agent + MAKER architecture.

## Summary Checklist
- [ ] **Step 1**: Create a new Skill file (`.fast-agent/skills/...`).
- [ ] **Step 2**: Define the new Agent function in `agent.py`.
- [ ] **Step 3**: Update the classification Prompt for `IntentClassifier`.
- [ ] **Step 4**: Register the new Agent in the `agents` list of `Jarvis`.
- [ ] **Step 5**: Restart server & Verify.

---

## Step Details

### Step 1: Create a Skill
Every new Agent needs its own Skill file to hold specialized context and instructions. Do not hard-code instructions into the code.

1.  **Create the directory**: `backend/.fast-agent/skills/<skill-folder-name>/`
    *   *Rule*: Folder name in lowercase, using hyphens (kebab-case). Examples: `football-news`, `image-generation`.
2.  **Create the file**: `SKILL.md` inside that directory.
3.  **Template content**:

```markdown
---
name: <short-skill-name>
description: Describe what this skill does
---
# <Skill Name> Instructions

## Role
You are an expert in...

## Capabilities
- [List capabilities]

## Response Rules
- [Rule 1]
- [Rule 2]
```

### Step 2: Define the Agent in `agent.py`
Open `backend/agent.py` and add the Agent definition block.

**Location:** Place it together with the other "Specialized Agents".

```python
# Find the line: @fast.agent
@fast.agent(
    name="<AgentName>",          # Example: SportsAgent (PascalCase)
    instruction="You are... \n\n{{agentSkills}}", # REQUIRED: keep this placeholder
    skills=CORE_SKILLS + get_skills("<skill-folder-name-from-step-1>"),
    model="openai.gpt-4o-mini",
    servers=["<server-1>", "<server-2>"], # Example: "serpapi", "fpl-server"
    # tools={...} # (Optional) Specify tools explicitly if you need to restrict them
)
async def <agent_function_name>(prompt: str):
    pass
```

### Step 3: Update the Intent Classifier (MAKER)
For Jarvis to know **when** to call this Agent, you need to teach `IntentClassifier` to recognize the new intent.

**Location:** Find the `IntentClassifier` Agent in `agent.py`.

```python
@fast.agent(
    name="IntentClassifier",
    # ...
    instruction="""
    Classify the user's request...
    # ... (Existing intents)
    - IOT_CONTROL: ...
    # ADD THE LINE BELOW:
    - <NEW_INTENT_NAME>: <Short description of when the user picks this>.
    
    Respond with ONLY the category name.
    """
)
```
*Example:* `- SPORTS_NEWS: Asking for football scores, team news, fixtures.`

### Step 4: Register With the Master Agent (Jarvis)
Finally, tell "Boss" Jarvis about the new employee and their responsibilities.

**Location:** Find the `Jarvis` Agent (Master Agent) at the end of `agent.py`.

1.  **Update the Instruction Ranking/Mapping**:
    ```python
    instruction="""
    # ...
    MAPPING INTENT -> AGENT:
    # ...
    - IOT_CONTROL -> IoTAgent
    # ADD THE LINE BELOW:
    - <NEW_INTENT_NAME> -> <AgentName>
    # ...
    """
    ```

2.  **Add to the `agents` list**:
    ```python
    agents=[
        "ReliableRouter",
        # ...
        "<AgentName>",  # <--- ADD THE AGENT NAME HERE (must match the name in Step 2)
    ],
    ```

### Step 5: Restart & Verify
The new Agent will **not** be active until you restart the server.

1.  **Restart Backend**:
    ```bash
    uv run uvicorn server:app --host 0.0.0.0 --reload
    ```
2.  **Test**:
    *   Chat a command that triggers the new Intent.
    *   Check the logs to ensure `ReliableRouter` returns the correct Intent and `Jarvis` calls the correct new Agent.
