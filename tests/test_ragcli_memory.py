import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from bin.ragcli import build_parser


def test_ragcli_memory_cli_commands():
    parser = build_parser()

    with tempfile.TemporaryDirectory() as tmpdir:
        source_dir = Path(tmpdir)
        mock_reg = {
            "kbs": [
                {
                    "name": "test-agent-kb",
                    "source_dir": str(source_dir),
                }
            ]
        }

        with patch("bin.ragcli.load_registry", return_value=mock_reg):
            # 1. remember command
            args = parser.parse_args([
                "memory", "remember", "test-agent-kb", "Scelta di design: REST API",
                "--type", "decision", "--title", "API Choice", "--tags", "api,rest"
            ])
            args.func(args)

            # 2. list command
            args = parser.parse_args(["memory", "list", "test-agent-kb"])
            args.func(args)

            # 3. recall command
            args = parser.parse_args(["memory", "recall", "test-agent-kb", "REST API"])
            args.func(args)

            # 4. dream command
            args = parser.parse_args(["memory", "dream", "test-agent-kb", "--dry-run"])
            args.func(args)


def test_ragcli_import_archive_cli():
    parser = build_parser()

    with tempfile.TemporaryDirectory() as tmpdir:
        source_dir = Path(tmpdir) / "kb_source"
        source_dir.mkdir()
        archive_dir = Path(tmpdir) / "fake_archive"
        archive_dir.mkdir()
        (archive_dir / "chat.json").write_text(json.dumps([{
            "uuid": "c-1",
            "name": "Chat 1",
            "chat_messages": [{"sender": "human", "text": "Ricordati che usiamo Linux Debian."}]
        }]))

        mock_reg = {
            "kbs": [
                {
                    "name": "import-kb",
                    "source_dir": str(source_dir),
                }
            ]
        }

        with patch("bin.ragcli.load_registry", return_value=mock_reg):
            args = parser.parse_args(["import-archive", "import-kb", str(archive_dir)])
            args.func(args)

            imported = list((source_dir / "imported").rglob("*.md"))
            assert len(imported) == 1
