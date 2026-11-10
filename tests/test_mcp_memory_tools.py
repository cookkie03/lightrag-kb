import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "mcp") not in sys.path:
    sys.path.insert(0, str(ROOT / "mcp"))

import pytest
from kb_router import KBRouter


def test_kb_router_memory_tools():
    with tempfile.TemporaryDirectory() as tmpdir:
        source_dir = Path(tmpdir) / "source"
        data_dir = source_dir / ".lightrag"
        source_dir.mkdir(parents=True)
        data_dir.mkdir(parents=True)

        registry_data = {
            "kbs": [
                {
                    "name": "agent-memory",
                    "source_dir": str(source_dir),
                    "data_dir": str(data_dir),
                    "derived_dir": str(data_dir / "derived"),
                    "rag_storage": str(data_dir / "rag_storage"),
                    "port": 9621,
                }
            ]
        }

        router = KBRouter(registry=registry_data)

        # 1. remember tool
        rem_res = json.loads(router.remember(
            kb="agent-memory",
            content="Usiamo sempre snake_case per le funzioni.",
            memory_type="preference",
            title="Convenzione Naming",
            tags=["python", "style"],
        ))
        assert rem_res["status"] == "ok"
        mem_id = rem_res["memory"]["id"]
        assert mem_id.startswith("mem_pref_")

        # 2. search_memory tool
        search_res = json.loads(router.search_memory(
            kb="agent-memory",
            query="naming style",
            active_only=True,
        ))
        assert search_res["status"] == "ok"
        assert search_res["count"] == 1
        assert search_res["memories"][0]["id"] == mem_id

        # 3. deprecate_memory tool
        dep_res = json.loads(router.deprecate_memory(
            kb="agent-memory",
            memory_id=mem_id,
            reason="Adottato standard diverso",
        ))
        assert dep_res["status"] == "ok"
        assert dep_res["memory"]["status"] == "deprecated"

        # Search active_only should now be empty
        search_active_empty = json.loads(router.search_memory(
            kb="agent-memory",
            query="naming style",
            active_only=True,
        ))
        assert search_active_empty["count"] == 0
