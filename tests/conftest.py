from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = ROOT / "bin"
SHIM_ROOT = ROOT / "services" / "obsidian-mcp-oauth-shim"

if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

if str(SHIM_ROOT) not in sys.path:
    sys.path.insert(0, str(SHIM_ROOT))
