"""WS02 foundation: scope taxonomy, enums, budget presets, evidence shape."""

import pytest

from services.memory.models import (
    Authority,
    CandidateStatus,
    MemoryType,
    PIN_FORBIDDEN_AUTHORITIES,
    validate_subject_scope,
    is_valid_subject_scope,
)
from services.retrieval.contracts import (
    DEFAULT_BUDGETS,
    Evidence,
    EvidenceScores,
    EvidenceSource,
    RetrievalMode,
)


@pytest.mark.parametrize("scope", ["user", "system", "project:jarvis", "agent:Riley [SA]"])
def test_valid_scopes_pass(scope):
    assert validate_subject_scope(scope) == scope


@pytest.mark.parametrize("scope", ["", "project:", "team:foo", "Project:Jarvis", "random", "project: jarvis"])
def test_invalid_scopes_rejected(scope):
    assert not is_valid_subject_scope(scope)
    with pytest.raises(ValueError):
        validate_subject_scope(scope)


def test_enum_values_match_spec():
    assert {t.value for t in MemoryType} == {"pinned", "episodic", "semantic", "procedural"}
    assert Authority.INFERRED.value in PIN_FORBIDDEN_AUTHORITIES
    assert CandidateStatus.PENDING.value == "pending"


def test_budget_presets_per_mode():
    assert set(DEFAULT_BUDGETS) == {m.value for m in RetrievalMode}
    assert DEFAULT_BUDGETS["economical"].max_evidence_tokens == 1000
    assert DEFAULT_BUDGETS["balanced"].max_evidence_tokens == 2500
    assert DEFAULT_BUDGETS["deep"].max_evidence_tokens == 5000
    # economical disables the planner + reranker
    assert DEFAULT_BUDGETS["economical"].planner == "off"
    assert DEFAULT_BUDGETS["economical"].reranker == "off"


def test_evidence_to_dict_shape():
    ev = Evidence(
        evidence_id="memory:1:chunk:2",
        record_id="1",
        owner_agent_name="Jarvis",
        memory_type="semantic",
        excerpt="hello",
        source=EvidenceSource(type="session_message", id="doc-1", timestamp=0.0),
        scores=EvidenceScores(bm25_rank=2, dense_rank=5, rrf=0.03, final=0.028),
        authority="user_confirmed",
        confidence=0.95,
    )
    d = ev.to_dict()
    assert d["source"]["type"] == "session_message"
    assert d["scores"]["final"] == 0.028
    assert d["validity"] == {"valid_from": None, "valid_until": None}
