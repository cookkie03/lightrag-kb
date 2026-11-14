import json
from pathlib import Path

from bin.ragcli import build_parser
from pipeline.dreaming import remember_memory


def test_retrieve_hierarchical_parser_has_memory_and_url():
    parser = build_parser()
    memory_args = parser.parse_args(["retrieve", "memory", "query text"])
    url_args = parser.parse_args(["retrieve", "url", "kb", "https://example.com/video"])
    assert memory_args.query == "query text"
    assert memory_args.kb is None
    assert url_args.kb == "kb"


def test_memory_scope_defaults_to_all_kbs(tmp_path, monkeypatch, capsys):
    from bin import ragcli
    kb_a = tmp_path / "a"
    kb_b = tmp_path / "b"
    remember_memory(kb_a, "alpha shared fact", title="A")
    remember_memory(kb_b, "beta shared fact", title="B")
    registry = {"kbs": [
        {"name": "a", "source_dir": str(kb_a)},
        {"name": "b", "source_dir": str(kb_b)},
    ]}
    monkeypatch.setattr(ragcli, "global_env", lambda: {})
    monkeypatch.setattr(ragcli, "load_registry", lambda env: registry)
    args = build_parser().parse_args(["retrieve", "memory", "shared"])
    args.func(args)
    result = json.loads(capsys.readouterr().out)
    assert {item["kb"] for item in result["memories"]} == {"a", "b"}
