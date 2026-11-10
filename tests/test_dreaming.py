import tempfile
from pathlib import Path

import pytest
from pipeline.dreaming import (
    deprecate_memory_by_id,
    find_conflicting_decisions,
    load_kb_memories,
    query_memories,
    remember_memory,
    run_dreaming_cycle,
)


def test_remember_and_load_memory():
    with tempfile.TemporaryDirectory() as tmpdir:
        source_dir = Path(tmpdir)
        mem = remember_memory(
            source_dir=source_dir,
            content="Usiamo sempre Tailscale per l'accesso remoto con TLS.",
            memory_type="decision",
            title="Architettura Rete",
            tags=["network", "tailscale"],
        )
        assert mem.id.startswith("mem_deci_")
        assert mem.status == "active"
        assert mem.type == "decision"

        memories = load_kb_memories(source_dir)
        assert len(memories) == 1
        assert memories[0].id == mem.id
        assert "Tailscale" in memories[0].content


def test_remember_with_supersedes():
    with tempfile.TemporaryDirectory() as tmpdir:
        source_dir = Path(tmpdir)
        mem1 = remember_memory(
            source_dir=source_dir,
            content="Usare SQLite per catalog.",
            memory_type="decision",
            title="Database Decision",
        )

        mem2 = remember_memory(
            source_dir=source_dir,
            content="Usare json_store.py invece di SQLite per catalog.",
            memory_type="decision",
            title="Database Decision v2",
            supersedes_id=mem1.id,
        )

        memories = load_kb_memories(source_dir)
        assert len(memories) == 2

        m1_reloaded = next(m for m in memories if m.id == mem1.id)
        assert m1_reloaded.status == "superseded"
        assert m1_reloaded.superseded_by == mem2.id

        m2_reloaded = next(m for m in memories if m.id == mem2.id)
        assert m2_reloaded.status == "active"


def test_deprecate_memory():
    with tempfile.TemporaryDirectory() as tmpdir:
        source_dir = Path(tmpdir)
        mem = remember_memory(
            source_dir=source_dir,
            content="Regola temporanea per debug.",
            memory_type="preference",
            title="Debug flag",
        )

        updated = deprecate_memory_by_id(source_dir, mem.id, reason="Debug session ended")
        assert updated is not None
        assert updated.status == "deprecated"
        assert updated.deprecation_reason == "Debug session ended"

        memories = load_kb_memories(source_dir)
        assert memories[0].status == "deprecated"


def test_query_memories_active_only():
    with tempfile.TemporaryDirectory() as tmpdir:
        source_dir = Path(tmpdir)
        m1 = remember_memory(source_dir, "Preferisco i commenti Redis", "preference", "Comment Style")
        m2 = remember_memory(source_dir, "Commenti stile JSDoc", "preference", "Old Comment Style")
        deprecate_memory_by_id(source_dir, m2.id, "Deprecated")

        active_res = query_memories(source_dir, "commenti", active_only=True)
        assert len(active_res) == 1
        assert active_res[0].id == m1.id

        all_res = query_memories(source_dir, "commenti", active_only=False)
        assert len(all_res) == 2


def test_dreaming_cycle_supersedes_conflicting_decision():
    with tempfile.TemporaryDirectory() as tmpdir:
        source_dir = Path(tmpdir)
        # Create older decision
        d1 = remember_memory(
            source_dir,
            content="Configurazione server: usare porta 8080 per HTTP.",
            memory_type="decision",
            title="Porta Server",
            tags=["server", "port"],
        )
        # Create newer decision overriding with clear contradiction keyword
        d2 = remember_memory(
            source_dir,
            content="Configurazione server: migrato a porta 9621 invece di 8080.",
            memory_type="decision",
            title="Porta Server",
            tags=["server", "port"],
        )

        report = run_dreaming_cycle(source_dir, dry_run=False)
        assert report["superseded_decisions"] >= 1

        memories = load_kb_memories(source_dir)
        d1_reloaded = next(m for m in memories if m.id == d1.id)
        assert d1_reloaded.status == "superseded"
        assert d1_reloaded.superseded_by == d2.id


def test_dreaming_cycle_episodic_compaction():
    with tempfile.TemporaryDirectory() as tmpdir:
        source_dir = Path(tmpdir)
        for i in range(5):
            remember_memory(
                source_dir,
                content=f"Log sessione {i}: completato task {i}.",
                memory_type="episodic",
                title=f"Sessione {i}",
            )

        report = run_dreaming_cycle(source_dir, dry_run=False)
        assert report["episodic_compaction"]["compacted_count"] == 3
        assert report["episodic_compaction"]["milestones_created"] == 1

        memories = load_kb_memories(source_dir)
        # Total memories: 5 initial episodics + 1 milestone = 6
        assert len(memories) == 6
        active_episodics = [m for m in memories if m.type == "episodic" and m.status == "active"]
        assert len(active_episodics) == 2
