"""Tune/regress the rerank_min_score floor for the default reranker on a
multi-case Vietnamese labelled set.

Opt-in (``memory_eval`` marker) — loads the real cross-encoder, so it's skipped
unless sentence-transformers + the model are present. Synthetic profile corpus
(no real PII; vi+en code-switching is a core feature, VI fixtures are allowed).

Guards the decision in services/memory/settings.py: with bge-reranker-v2-m3 the
0.001 floor keeps ALL on-topic answers and drops ALL clear off-topic queries.
A reranker/floor change that breaks recall or off-topic suppression fails here.
"""
import importlib.util

import pytest

pytestmark = pytest.mark.memory_eval

_ST = importlib.util.find_spec("sentence_transformers") is not None
_MODEL = "BAAI/bge-reranker-v2-m3"
_FLOOR = 0.001

# Synthetic profile (mirrors the kinds of facts personal memory stores).
_CORPUS = [
    "Người dùng tên là An.",                                  # 0
    "Người dùng tên đầy đủ là Trần Hoàng An.",                # 1
    "Người dùng sinh năm 1990.",                              # 2
    "Người dùng làm software engineer tại FPT.",              # 3
    "Công việc chính là phát triển web frontend với React.",  # 4
    "Địa chỉ công ty là số 10 Lê Lợi, Quận 1, TP HCM.",       # 5
    "Người dùng đã có gia đình.",                             # 6
    "Người dùng có một bé gái tên là Bông.",                  # 7
    "Bé Bông được 2 tuổi.",                                   # 8
    "Buổi sáng người dùng đi làm lúc 7h.",                    # 9
    "Người dùng thích leo núi và đã đến Fansipan, Tà Xùa.",   # 10
    "Người dùng đam mê nhiếp ảnh.",                           # 11
]

def _i(*kw):
    return {i for i, d in enumerate(_CORPUS) if all(k.lower() in d.lower() for k in kw)}

# (query, relevant indices). Empty = off-topic-clear (nothing should be kept).
_ON_TOPIC = [
    ("tôi tên là gì", _i("tên là An") | _i("tên đầy đủ")),
    ("tên đầy đủ của tôi", _i("tên đầy đủ")),
    ("tôi sinh năm nào", _i("sinh năm")),
    ("tôi làm nghề gì", _i("software engineer") | _i("frontend")),
    ("tôi làm ở công ty nào", _i("FPT")),
    ("công ty tôi ở đâu", _i("địa chỉ công ty") | _i("Lê Lợi")),
    ("con tôi tên gì", _i("Bông") - _i("2 tuổi")),
    ("con tôi mấy tuổi", _i("2 tuổi")),
    ("sáng tôi đi làm lúc mấy giờ", _i("7h")),
    ("tôi code bằng gì", _i("React")),
    ("sở thích của tôi", _i("leo núi") | _i("nhiếp ảnh")),
    ("kể về gia đình tôi", _i("gia đình") | _i("Bông")),
]
_OFF_TOPIC = [
    "thời tiết hôm nay thế nào",
    "giá vàng hôm nay bao nhiêu",
    "công thức nấu phở bò",
    "thủ đô nước Pháp là gì",
    "cách cài docker trên ubuntu",
]


@pytest.fixture(scope="module")
def reranker():
    if not _ST:
        pytest.skip("sentence-transformers not installed")
    from services.retrieval.reranker import get_reranker
    r = get_reranker(_MODEL)
    try:
        r.warm()
    except Exception as exc:  # noqa: BLE001 — model not downloadable in this env
        pytest.skip(f"reranker model unavailable: {exc}")
    return r


def test_floor_keeps_all_on_topic(reranker):
    """At the 0.001 floor every on-topic query keeps >= 1 relevant memory."""
    misses = []
    for q, rel in _ON_TOPIC:
        s = reranker.rerank(q, _CORPUS)
        if not any(s[i] >= _FLOOR for i in rel):
            misses.append((q, max((s[i] for i in rel), default=0.0)))
    assert not misses, f"on-topic recall dropped below floor: {misses}"


def test_floor_drops_all_clear_off_topic(reranker):
    """At the 0.001 floor every clear off-topic query injects nothing."""
    leaks = []
    for q in _OFF_TOPIC:
        s = reranker.rerank(q, _CORPUS)
        if any(x >= _FLOOR for x in s):
            leaks.append((q, max(s)))
    assert not leaks, f"clear off-topic leaked above floor: {leaks}"
