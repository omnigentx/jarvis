"""Self-heal sidecar coverage for ladybug_store (pure filesystem — no ladybug).

Regression: the quarantine/wipe used a fixed suffix tuple that missed
``.wal.checkpoint``. After quarantining a corrupt graph, the leftover checkpoint
was re-read by the "fresh" reopen → "Corrupted wal file" again → the semantic
index stayed *unavailable* across every restart until a human deleted the file.
These lock in that ALL sidecars (globbed) are moved/removed.
"""
import os

from services.indexing import ladybug_store as ls

SIDECARS = ("", ".wal", ".wal.checkpoint", ".shadow", ".lock", ".tmp")


def _make(tmp_path, *suffixes):
    for s in suffixes:
        (tmp_path / f"memory_graph{s}").write_text("x")
    return str(tmp_path / "memory_graph")


def test_graph_files_lists_all_sidecars_excluding_corrupt(tmp_path):
    base = _make(tmp_path, *SIDECARS)
    (tmp_path / "memory_graph.wal.corrupt").write_text("old")   # a prior quarantine copy
    names = sorted(os.path.basename(p) for p in ls._graph_files(base))
    assert names == sorted(f"memory_graph{s}" for s in SIDECARS)  # every live sidecar, no .corrupt


def test_quarantine_moves_every_sidecar_including_wal_checkpoint(tmp_path):
    base = _make(tmp_path, *SIDECARS)
    ls._quarantine_graph_files(base)
    leftovers = [p.name for p in tmp_path.iterdir() if not p.name.endswith(".corrupt")]
    assert leftovers == []                                       # nothing left to re-corrupt the reopen
    assert (tmp_path / "memory_graph.wal.checkpoint.corrupt").exists()


def test_quarantine_preserves_prior_corrupt_copies(tmp_path):
    (tmp_path / "memory_graph.corrupt").write_text("prev")       # untouchable forensic copy
    base = _make(tmp_path, ".wal")
    ls._quarantine_graph_files(base)
    assert (tmp_path / "memory_graph.corrupt").exists()          # not re-quarantined
    assert (tmp_path / "memory_graph.wal.corrupt").exists()      # newly quarantined
    assert not (tmp_path / "memory_graph.corrupt.corrupt").exists()


def test_wipe_removes_all_sidecars(tmp_path):
    _make(tmp_path, *SIDECARS)
    ls._wipe_graph_files(str(tmp_path / "memory_graph"))
    assert list(tmp_path.iterdir()) == []
