from unittest.mock import MagicMock, patch
from services.spawn_progress_bridge import SpawnProgressBridge


def test_actual_per_call_producer_values_are_not_subtracted():
    bridge = SpawnProgressBridge(progress_manager=MagicMock())
    calls = [
        (7208, 49, 0),
        (7357, 77, 4864),
        (7860, 114, 4864),
        (8079, 52, 0),
        (8273, 62, 5888),
        (8757, 100, 5888),
        (8952, 58, 5888),
        (9480, 66, 5888),
        (9655, 44, 6912),
    ]
    captured = []
    with patch(
        "services.sse_progress._persist_and_broadcast_token_usage",
        side_effect=lambda *a: captured.append(a[2]),
    ):
        for idx, (i, o, cache) in enumerate(calls):
            bridge._handle_token_usage(
                "Emerson [PM]",
                {
                    "input_tokens": i,
                    "output_tokens": o,
                    "cache_hit_tokens": cache,
                    "model": "coding-agent",
                },
                {"run_id": "21e92d08", "timestamp": 1790827529 + idx},
            )
    assert sum(x["input"] for x in captured) == 75621
    assert sum(x["output"] for x in captured) == 622
    assert sum(x["cache_hit"] for x in captured) == 40192


def test_event_identity_is_stable_across_bridge_restart():
    data = {"input_tokens": 1000, "output_tokens": 10, "model": "test"}
    raw = {"run_id": "r", "timestamp": 123.45}
    events = []
    with patch(
        "services.sse_progress._persist_and_broadcast_token_usage",
        side_effect=lambda *a: events.append(a[2]),
    ):
        for _ in range(2):
            SpawnProgressBridge(progress_manager=MagicMock())._handle_token_usage(
                "Dev", data, raw
            )
    assert events[0]["source_event_id"] == events[1]["source_event_id"]


def test_database_dedup_survives_recreated_bridge(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import core.database as db
    from services import sse_progress

    engine = create_engine("sqlite:///" + str(tmp_path / "usage.db"))
    db.Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine))
    db.init_db()
    stream = MagicMock()
    monkeypatch.setattr(sse_progress, "_get_activity_stream", lambda: stream)
    for _ in range(2):
        b = SpawnProgressBridge(progress_manager=MagicMock())
        b._handle_token_usage(
            "Dev",
            {
                "input_tokens": 1000,
                "output_tokens": 10,
                "cache_hit_tokens": 800,
                "model": "coding-agent",
            },
            {"run_id": "r", "timestamp": 1},
        )
    with db.SessionLocal() as session:
        records = session.query(db.TokenUsageRecord).all()
        assert len(records) == 1
        assert records[0].total_tokens == 1010
        assert records[0].cache_hit_tokens == 800
    stream.broadcast.assert_called_once()
    engine.dispose()


def test_model_switch_keeps_per_call_cache_and_reasoning(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import core.database as db
    from services import sse_progress

    engine = create_engine("sqlite:///" + str(tmp_path / "switch.db"))
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine))
    db.init_db()
    monkeypatch.setattr(sse_progress, "_get_activity_stream", MagicMock())
    bridge = SpawnProgressBridge(progress_manager=MagicMock())
    for timestamp, model in [(1, "model-a"), (2, "model-b"), (2, "model-b")]:
        bridge._handle_token_usage(
            "Dev",
            {
                "model": model,
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_tokens": 80,
                "reasoning_tokens": 10,
            },
            {"run_id": "same-run", "timestamp": timestamp},
        )
    with db.SessionLocal() as session:
        records = session.query(db.TokenUsageRecord).all()
        assert len(records) == 2
        assert {x.model for x in records} == {"model-a", "model-b"}
        assert sum(x.input_tokens for x in records) == 200
        assert sum(x.cache_read_tokens for x in records) == 160
        assert sum(x.reasoning_tokens for x in records) == 20
    engine.dispose()
