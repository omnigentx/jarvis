"""Regression guards for the meeting-skill content.

Production incident 2026-04-24 (meeting ``cbc7fbb2``): a Dev agent followed
``meeting-participant`` skill line "Conclude clearly — when asked for a
verdict, state [DECISION] VERDICT: PASS or FAIL" and emitted the phrase in
its ``speak()`` message. ``meeting_room_server`` regex-matched the phrase
and ended the meeting on turn 2 — before the facilitator (PM) could
orchestrate the full discussion.

Fix model: the verdict phrase is facilitator-only. Non-facilitator roles
(Dev, BA, SA, QE, DSO, Designer) all read ``meeting-participant`` via the
``team-communication`` skill, so that file MUST NOT teach or quote the
verdict phrase. The authoritative place is ``project-management`` (loaded
only by PM/orchestrator).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


SKILLS_DIR = Path(__file__).resolve().parents[2] / ".fast-agent" / "skills"


# Matches the exact phrase that ``meeting_room_server.py`` treats as a
# meeting-ending signal:
#   re.search(r"\[DECISION\]\s*VERDICT:\s*(PASS|FAIL|ESCALATE|ESCALATE_TO_USER|RESOLVED)", ...)
# Mirror the same tolerance (whitespace, case) so the guard catches any
# variant that would also trip the server.
VERDICT_END_PHRASE = re.compile(
    r"\[DECISION\]\s*VERDICT:\s*(PASS|FAIL|ESCALATE|ESCALATE_TO_USER|RESOLVED)",
    re.IGNORECASE,
)


def test_participant_skill_does_not_teach_verdict_phrase():
    """If this fails, a non-PM role (Dev/BA/QE/…) will see the verdict
    phrase in its skill and emit it in meetings — ending them prematurely.
    Move any verdict guidance into ``project-management`` (PM only) or
    ``meeting-facilitator`` instead."""
    skill = (SKILLS_DIR / "meeting-participant" / "SKILL.md").read_text(encoding="utf-8")
    matches = VERDICT_END_PHRASE.findall(skill)
    assert not matches, (
        f"meeting-participant SKILL.md leaks the facilitator-only verdict "
        f"phrase — found variants: {matches}. Only the meeting facilitator "
        f"(PM via project-management skill) may teach or use this phrase."
    )


def test_pm_skill_still_owns_verdict_phrase():
    """Defense against over-correction: the PM (via ``project-management``)
    is the legitimate owner of the verdict phrase and must still teach it.
    If this fails, nobody will end meetings and they'll hit max_rounds."""
    skill = (SKILLS_DIR / "project-management" / "SKILL.md").read_text(encoding="utf-8")
    assert VERDICT_END_PHRASE.search(skill), (
        "project-management SKILL.md lost the verdict phrase — PM needs it "
        "to conclude meetings. Restore the [DECISION] VERDICT: PASS guidance."
    )


_MEETING_CONTEXT = re.compile(r"\bmeeting\b|\bspeak\(|meeting_room", re.IGNORECASE)

# How many lines around a verdict match to scan for meeting-context words.
# Paragraph-split missed cross-paragraph framing like
#     "Use [DECISION] VERDICT: PASS to conclude.
#
#      The meeting will end once the verdict is spoken."
# A line window catches that while staying tight enough to avoid false
# positives on skills that mention both topics in separate, unrelated sections.
_VERDICT_CONTEXT_WINDOW_LINES = 6


@pytest.mark.parametrize("skill_name", [
    "team-communication",  # everyone reads this — points at meeting-participant
    "ba-workflow",
    "dev-workflow",
    "qe-workflow",          # async code review verdict lives here — ok as long
                            # as it's not framed as a meeting-ending action
    "dso-workflow",
    "software-architecture",
    "figma-designer",
])
def test_non_pm_skills_do_not_describe_verdict_as_meeting_end(skill_name):
    """A non-PM skill may mention the verdict phrase in a code-review or
    report context (qe-workflow/code-review write verdicts to file/email,
    never to ``speak()``). It must NOT frame the phrase as a way to end
    a live meeting — that's what caused the incident."""
    path = SKILLS_DIR / skill_name / "SKILL.md"
    if not path.exists():
        pytest.skip(f"{skill_name} skill not present in this build")
    lines = path.read_text(encoding="utf-8").splitlines()

    for i, line in enumerate(lines):
        if not VERDICT_END_PHRASE.search(line):
            continue
        lo = max(0, i - _VERDICT_CONTEXT_WINDOW_LINES)
        hi = min(len(lines), i + _VERDICT_CONTEXT_WINDOW_LINES + 1)
        window = "\n".join(lines[lo:hi])
        if _MEETING_CONTEXT.search(window):
            raise AssertionError(
                f"{skill_name} SKILL.md ties the verdict phrase to a meeting "
                f"context — this instructs non-PM roles to end meetings, which "
                f"caused the 2026-04-24 incident. Offending region (line "
                f"{lo + 1}–{hi}):\n{window!r}"
            )
