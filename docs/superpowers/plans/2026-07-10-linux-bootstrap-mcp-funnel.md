# Linux Bootstrap, MCP, and Funnel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `lightrag-kb` bootstrappable and operable on Linux through `./setup.sh`, with centralized OS-agnostic Docling config, Linux service management, public Tailscale Funnel exposure for the OAuth shim, and preserved MCP tool behavior.

**Architecture:** Add a thin Linux deployment layer around the current repo instead of rewriting existing flows. Centralize runtime/platform/OCR/ASR/publish choices in `kb_registry`, drive `setup.sh` through reusable `ragcli` helpers, and verify behavior only at confirmed seams: `ragcli`, shim HTTP endpoints, and publish/service orchestration.

**Tech Stack:** Python 3, FastAPI, FastMCP, requests/httpx, Docling, uv, Bash, systemd --user, Tailscale Serve/Funnel, pytest.

---

## File Structure

### Files to create

- `tests/conftest.py`
- `tests/test_kb_registry_linux_config.py`
- `tests/test_ingest_docling_config.py`
- `tests/test_ragcli_doctor_service.py`
- `tests/test_setup_script.py`
- `tests/test_shim_http_surface.py`
- `systemd/lightrag-server@.service`
- `systemd/lightrag-watchdog@.service`
- `systemd/obsidian-mcp-oauth-shim.service`

### Files to modify

- `setup.sh`
- `config/global.env.example`
- `bin/kb_registry.py`
- `bin/ingest.py`
- `bin/ragcli.py`
- `services/obsidian-mcp-oauth-shim/app/config.py`
- `services/obsidian-mcp-oauth-shim/README.md`
- `README.md`

### Responsibility map

- `bin/kb_registry.py`: single source of truth for platform/runtime/Docling/shim defaults.
- `bin/ingest.py`: resolve Docling OCR/ASR/accelerator from config and keep format coverage stable.
- `bin/ragcli.py`: public seam for diagnostics and Linux service maintenance.
- `setup.sh`: one-shot Linux bootstrap and publish orchestration.
- `systemd/*.service`: declarative Linux user services.
- `tests/*.py`: seam-level TDD coverage.

---

### Task 1: Create the Test Harness and Confirm Public Seams

**Files:**
- Create: `tests/conftest.py`
- Create: `tests/test_kb_registry_linux_config.py`
- Create: `tests/test_ragcli_doctor_service.py`
- Modify: `setup.sh`

- [ ] **Step 1: Write the failing config seam test**

```python
# tests/test_kb_registry_linux_config.py
from pathlib import Path

from kb_registry import load_env, global_env


def test_global_env_exposes_linux_docling_defaults(tmp_path: Path, monkeypatch):
    config_file = tmp_path / "global.env"
    config_file.write_text(
        "\n".join(
            [
                "ENGINE_PYTHON=/tmp/venv/bin/python",
                "LIGHTRAG_SERVER_BIN=/tmp/venv/bin/lightrag-server",
                "DEPLOY_PLATFORM=linux",
            ]
        )
        + "\n"
    )
    monkeypatch.setattr("kb_registry.CONFIG", config_file)

    env = global_env()

    assert env["DEPLOY_PLATFORM"] == "linux"
    assert env["DOCLING_ASR_ENGINE"] == "whisper_turbo_native"
    assert env["DOCLING_OCR_ENGINE"] in {"rapidocr", "easyocr", "auto"}
    assert env["SHIM_BIND_HOST"] == "127.0.0.1"
    assert env["TAILSCALE_FUNNEL_PORT"] == "8443"
```

- [ ] **Step 2: Write the failing CLI seam test**

```python
# tests/test_ragcli_doctor_service.py
import argparse
from types import SimpleNamespace

import pytest

import ragcli


def test_service_parser_accepts_status_restart_logs():
    parser = ragcli.build_parser()
    status_args = parser.parse_args(["service", "status"])
    restart_args = parser.parse_args(["service", "restart", "obsidian-mcp-oauth-shim"])
    logs_args = parser.parse_args(["service", "logs", "obsidian-mcp-oauth-shim"])

    assert status_args.service_cmd == "status"
    assert restart_args.service_cmd == "restart"
    assert logs_args.service_cmd == "logs"


def test_doctor_reports_missing_runtime(monkeypatch, capsys):
    monkeypatch.setattr(ragcli, "_doctor_report", lambda: ["runtime: missing"])

    with pytest.raises(SystemExit) as exc:
        ragcli.cmd_doctor(SimpleNamespace())

    assert exc.value.code == 1
    assert "runtime: missing" in capsys.readouterr().out
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```bash
python -m pytest tests/test_kb_registry_linux_config.py tests/test_ragcli_doctor_service.py -q
```

Expected:

- FAIL because `build_parser`, `cmd_doctor`, and new default config keys do not exist yet.

- [ ] **Step 4: Add pytest to the bootstrap toolchain**

```bash
# setup.sh
uv pip install --python .venv-engine/bin/python \
  "lightrag-hku[api]" \
  ollama \
  "docling[asr,ocrmac,easyocr]" \
  requests \
  pyyaml \
  fastmcp \
  pytest
```

- [ ] **Step 5: Add the minimal parser seam and doctor stub**

```python
# bin/ragcli.py
def cmd_doctor(_: argparse.Namespace) -> None:
    lines = _doctor_report()
    print("\n".join(lines))
    if any("missing" in line or "down" in line or "error" in line for line in lines):
        raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ragcli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    doctor = sub.add_parser("doctor", help="verifica runtime, config, servizi e publish")
    doctor.set_defaults(func=cmd_doctor)

    service = sub.add_parser("service", help="gestione servizi Linux systemd --user")
    service_sub = service.add_subparsers(dest="service_cmd", required=True)
    service_sub.add_parser("status")
    restart = service_sub.add_parser("restart")
    restart.add_argument("name", nargs="?")
    logs = service_sub.add_parser("logs")
    logs.add_argument("name", nargs="?")
    return parser
```

- [ ] **Step 6: Add the minimal config defaults**

```python
# bin/kb_registry.py
def global_env() -> dict[str, str]:
    env = load_env(CONFIG)
    env.setdefault("DEPLOY_PLATFORM", "auto")
    env.setdefault("DOCLING_OCR_ENGINE", "auto")
    env.setdefault("DOCLING_ASR_ENGINE", "whisper_turbo_native" if env.get("DEPLOY_PLATFORM") == "linux" else "auto")
    env.setdefault("DOCLING_ACCELERATOR", "auto")
    env.setdefault("DOCLING_ENABLE_TABLES", "1")
    env.setdefault("DOCLING_ENABLE_CODE_ENRICHMENT", "1")
    env.setdefault("DOCLING_ENABLE_FORMULA_ENRICHMENT", "1")
    env.setdefault("DOCLING_IMAGE_SCALE", "2.0")
    env.setdefault("SHIM_BIND_HOST", "127.0.0.1")
    env.setdefault("SHIM_BIND_PORT", "8420")
    env.setdefault("TAILSCALE_FUNNEL_PORT", "8443")
    env.setdefault("SHIM_PUBLIC_URL", "https://your-node.your-tailnet.ts.net")
    return env
```

- [ ] **Step 7: Run tests to verify they pass**

Run:

```bash
python -m pytest tests/test_kb_registry_linux_config.py tests/test_ragcli_doctor_service.py -q
```

Expected:

- PASS

- [ ] **Step 8: Commit**

```bash
git add setup.sh bin/kb_registry.py bin/ragcli.py tests/test_kb_registry_linux_config.py tests/test_ragcli_doctor_service.py
git commit -m "test: add linux config and ragcli seam coverage"
```

---

### Task 2: Drive Docling OCR/ASR Selection from Centralized Config

**Files:**
- Create: `tests/test_ingest_docling_config.py`
- Modify: `bin/ingest.py`
- Modify: `config/global.env.example`

- [ ] **Step 1: Write the failing ingest seam test for Linux OCR/ASR selection**

```python
# tests/test_ingest_docling_config.py
import ingest


def test_linux_docling_visual_uses_rapidocr_when_configured(monkeypatch):
    captured = {}

    def fake_run_docling(py, code, timeout, label):
        captured["code"] = code
        return "ok"

    monkeypatch.setattr(ingest, "_run_docling", fake_run_docling)
    monkeypatch.setattr(ingest, "_docling_probe_text", lambda *args, **kwargs: "")

    ingest.render_docling_visual(
        path=__import__("pathlib").Path("/tmp/sample.pdf"),
        env={
            "DOCLING_PYTHON": "/tmp/python",
            "DOCLING_LANG": "it,en",
            "DOCLING_TIMEOUT": "3600",
            "DEPLOY_PLATFORM": "linux",
            "DOCLING_OCR_ENGINE": "rapidocr",
            "DOCLING_ACCELERATOR": "cpu",
            "DOCLING_ENABLE_TABLES": "1",
            "DOCLING_ENABLE_CODE_ENRICHMENT": "1",
            "DOCLING_ENABLE_FORMULA_ENRICHMENT": "1",
            "DOCLING_IMAGE_SCALE": "2.0",
        },
    )

    assert "RapidOcr" in captured["code"]


def test_linux_docling_audio_uses_native_whisper(monkeypatch):
    captured = {}
    monkeypatch.setattr(ingest.shutil, "which", lambda _: "/usr/bin/ffmpeg")

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        captured["script"] = cmd[2]
        return type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

    monkeypatch.setattr(ingest.subprocess, "run", fake_run)

    ingest.render_docling_audio(
        __import__("pathlib").Path("/tmp/sample.wav"),
        {
            "DOCLING_PYTHON": "/tmp/python",
            "DOCLING_TIMEOUT": "3600",
            "DOCLING_LANG": "it,en",
            "DEPLOY_PLATFORM": "linux",
            "DOCLING_ASR_ENGINE": "whisper_turbo_native",
        },
    )

    assert "WHISPER_TURBO_NATIVE" in captured["script"]
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python -m pytest tests/test_ingest_docling_config.py -q
```

Expected:

- FAIL because `render_docling_visual` and `render_docling_audio` do not resolve engines from centralized config.

- [ ] **Step 3: Extract config-driven Docling resolution helpers**

```python
# bin/ingest.py
def _env_flag(env: dict[str, str], key: str, default: str = "1") -> bool:
    return env.get(key, default).strip().lower() not in {"0", "false", "no", "off"}


def _docling_platform(env: dict[str, str]) -> str:
    configured = env.get("DEPLOY_PLATFORM", "auto").strip().lower()
    if configured in {"linux", "mac"}:
        return configured
    return "mac" if sys.platform == "darwin" else "linux"


def _docling_ocr_engine(env: dict[str, str]) -> str:
    engine = env.get("DOCLING_OCR_ENGINE", "auto").strip().lower()
    if engine != "auto":
        return engine
    return "ocrmac" if _docling_platform(env) == "mac" else "rapidocr"


def _docling_asr_engine(env: dict[str, str]) -> str:
    engine = env.get("DOCLING_ASR_ENGINE", "auto").strip().lower()
    if engine != "auto":
        return engine
    return "whisper_turbo_mlx" if _docling_platform(env) == "mac" else "whisper_turbo_native"
```

- [ ] **Step 4: Update visual OCR code generation to honor engine and quality flags**

```python
# bin/ingest.py
def _docling_visual_options_code(env: dict[str, str], force_full_page: bool, langs: list[str], locales: list[str]) -> tuple[str, str]:
    engine = _docling_ocr_engine(env)
    if engine == "ocrmac":
        return (
            "OcrMacOptions",
            f"OcrMacOptions(lang={locales!r}, recognition='accurate', force_full_page_ocr={force_full_page!r})",
        )
    if engine == "easyocr":
        return (
            "EasyOcrOptions",
            f"EasyOcrOptions(lang={langs!r}, force_full_page_ocr={force_full_page!r})",
        )
    if engine == "rapidocr":
        return (
            "RapidOcrOptions",
            f"RapidOcrOptions(lang={langs!r}, force_full_page_ocr={force_full_page!r})",
        )
    raise RuntimeError(f"DOCLING_OCR_ENGINE non supportato: {engine}")
```

- [ ] **Step 5: Update audio ASR code generation to honor Linux native Whisper**

```python
# bin/ingest.py
def _docling_asr_model_symbol(env: dict[str, str]) -> str:
    mapping = {
        "whisper_turbo": "WHISPER_TURBO",
        "whisper_turbo_native": "WHISPER_TURBO_NATIVE",
        "whisper_turbo_mlx": "WHISPER_TURBO_MLX",
        "whisper_s2t": "WHISPER_SMALL_S2T",
    }
    engine = _docling_asr_engine(env)
    if engine not in mapping:
        raise RuntimeError(f"DOCLING_ASR_ENGINE non supportato: {engine}")
    return mapping[engine]
```

- [ ] **Step 6: Extend the sample config with Linux-first keys**

```dotenv
# config/global.env.example
DEPLOY_PLATFORM=auto
DOCLING_OCR_ENGINE=auto
DOCLING_ASR_ENGINE=auto
DOCLING_ACCELERATOR=auto
DOCLING_ENABLE_TABLES=1
DOCLING_ENABLE_CODE_ENRICHMENT=1
DOCLING_ENABLE_FORMULA_ENRICHMENT=1
DOCLING_IMAGE_SCALE=2.0
SHIM_BIND_HOST=127.0.0.1
SHIM_BIND_PORT=8420
TAILSCALE_FUNNEL_PORT=8443
SHIM_PUBLIC_URL=https://your-node.your-tailnet.ts.net
```

- [ ] **Step 7: Run tests to verify they pass**

Run:

```bash
python -m pytest tests/test_ingest_docling_config.py tests/test_kb_registry_linux_config.py -q
```

Expected:

- PASS

- [ ] **Step 8: Commit**

```bash
git add bin/ingest.py config/global.env.example tests/test_ingest_docling_config.py
git commit -m "feat: centralize docling linux engine selection"
```

---

### Task 3: Add Linux Service Management to `ragcli`

**Files:**
- Modify: `bin/ragcli.py`
- Create: `systemd/lightrag-server@.service`
- Create: `systemd/lightrag-watchdog@.service`
- Create: `systemd/obsidian-mcp-oauth-shim.service`
- Modify: `tests/test_ragcli_doctor_service.py`

- [ ] **Step 1: Write the failing service orchestration test**

```python
def test_service_status_uses_systemctl_user(monkeypatch, capsys):
    calls = []

    def fake_run(cmd, capture_output=True, text=True, check=False):
        calls.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": "active\n", "stderr": ""})()

    monkeypatch.setattr(ragcli.subprocess, "run", fake_run)

    ragcli.cmd_service_status(__import__("types").SimpleNamespace(name=None))

    assert calls[0][:2] == ["systemctl", "--user"]
    assert "active" in capsys.readouterr().out
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python -m pytest tests/test_ragcli_doctor_service.py -q
```

Expected:

- FAIL because systemd wrapper functions do not exist.

- [ ] **Step 3: Add service wrapper functions and doctor report lines**

```python
# bin/ragcli.py
def _systemctl_user(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def cmd_service_status(args: argparse.Namespace) -> None:
    units = [args.name] if getattr(args, "name", None) else _default_linux_units()
    for unit in units:
        result = _systemctl_user("is-active", unit)
        state = result.stdout.strip() or result.stderr.strip() or "unknown"
        print(f"{unit}: {state}")


def cmd_service_restart(args: argparse.Namespace) -> None:
    units = [args.name] if getattr(args, "name", None) else _default_linux_units()
    for unit in units:
        result = _systemctl_user("restart", unit)
        if result.returncode != 0:
            raise SystemExit(result.stderr.strip() or f"restart fallito: {unit}")
        print(f"restarted {unit}")
```

- [ ] **Step 4: Add unit templates to the repo**

```ini
# systemd/obsidian-mcp-oauth-shim.service
[Unit]
Description=Obsidian MCP OAuth Shim
After=network-online.target tailscaled.service

[Service]
Type=simple
WorkingDirectory=%h/lightrag-kb/services/obsidian-mcp-oauth-shim
ExecStart=%h/lightrag-kb/.venv-engine/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8420
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
```

- [ ] **Step 5: Run tests to verify they pass**

Run:

```bash
python -m pytest tests/test_ragcli_doctor_service.py -q
```

Expected:

- PASS

- [ ] **Step 6: Commit**

```bash
git add bin/ragcli.py systemd tests/test_ragcli_doctor_service.py
git commit -m "feat: add linux service management"
```

---

### Task 4: Turn `setup.sh` Into Full Linux Bootstrap and Publish Orchestrator

**Files:**
- Create: `tests/test_setup_script.py`
- Modify: `setup.sh`
- Modify: `bin/ragcli.py`

- [ ] **Step 1: Write the failing setup orchestration test**

```python
# tests/test_setup_script.py
from pathlib import Path


def test_setup_script_contains_linux_bootstrap_phases():
    script = Path("setup.sh").read_text()

    assert "pytest" in script
    assert "systemctl --user daemon-reload" in script
    assert "tailscale serve --bg" in script
    assert "tailscale funnel --bg" in script
    assert "ragcli doctor" in script
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python -m pytest tests/test_setup_script.py -q
```

Expected:

- FAIL because `setup.sh` does not yet orchestrate systemd and Funnel.

- [ ] **Step 3: Add reusable helpers inside `ragcli` for setup-facing operations**

```python
# bin/ragcli.py
def install_user_units(repo_root: Path, home: Path) -> list[Path]:
    systemd_dir = home / ".config" / "systemd" / "user"
    systemd_dir.mkdir(parents=True, exist_ok=True)
    installed = []
    for name in ("lightrag-server@.service", "lightrag-watchdog@.service", "obsidian-mcp-oauth-shim.service"):
        src = repo_root / "systemd" / name
        dst = systemd_dir / name
        dst.write_text(src.read_text())
        installed.append(dst)
    return installed


def configure_tailscale_funnel(bind_port: str, public_port: str) -> list[list[str]]:
    return [
        ["tailscale", "serve", "--bg", public_port, f"http://127.0.0.1:{bind_port}"],
        ["tailscale", "funnel", "--bg", public_port],
    ]


def cmd_service_install_units(args: argparse.Namespace) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    install_user_units(repo_root, Path.home())
    print("installed user units")
```

- [ ] **Step 4: Expand `setup.sh` into the Linux bootstrap sequence**

```bash
# setup.sh
echo "==> Installo unit systemd --user"
python3 bin/ragcli.py service install-units
systemctl --user daemon-reload
systemctl --user enable --now obsidian-mcp-oauth-shim.service

echo "==> Configuro publish Tailscale per lo shim"
tailscale serve --bg 8443 http://127.0.0.1:8420
tailscale funnel --bg 8443

echo "==> Verifico lo stato finale"
ragcli doctor
```

- [ ] **Step 5: Run tests to verify they pass**

Run:

```bash
python -m pytest tests/test_setup_script.py tests/test_ragcli_doctor_service.py -q
```

Expected:

- PASS

- [ ] **Step 6: Commit**

```bash
git add setup.sh bin/ragcli.py tests/test_setup_script.py
git commit -m "feat: automate linux bootstrap and funnel publish"
```

---

### Task 5: Preserve Shim HTTP Surface While Making Linux Packaging Explicit

**Files:**
- Create: `tests/test_shim_http_surface.py`
- Modify: `services/obsidian-mcp-oauth-shim/app/config.py`
- Modify: `services/obsidian-mcp-oauth-shim/README.md`

- [ ] **Step 1: Write the failing shim seam test**

```python
# tests/test_shim_http_surface.py
from fastapi.testclient import TestClient

from services.obsidian-mcp-oauth-shim.app.main import app


def test_discovery_endpoints_remain_available():
    client = TestClient(app)

    root = client.get("/")
    authz = client.get("/.well-known/oauth-authorization-server")
    resource = client.get("/.well-known/oauth-protected-resource")

    assert root.status_code == 200
    assert authz.status_code == 200
    assert resource.status_code == 200
    assert resource.json()["resource"].endswith("/mcp")
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python -m pytest tests/test_shim_http_surface.py -q
```

Expected:

- FAIL because import path/test harness for the shim is not wired yet.

- [ ] **Step 3: Make shim config Linux-friendly without changing public behavior**

```python
# services/obsidian-mcp-oauth-shim/app/config.py
from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent
HOST = os.getenv("SHIM_BIND_HOST", "127.0.0.1")
PORT = int(os.getenv("SHIM_BIND_PORT", "8420"))
ISSUER_URL = os.getenv("ISSUER_URL", os.getenv("SHIM_PUBLIC_URL", "https://your-node.your-tailnet.ts.net"))
```

- [ ] **Step 4: Document the Linux launch model**

```md
## Linux service model

On Linux, the shim is expected to run under `systemd --user`, binding only to `127.0.0.1:${SHIM_BIND_PORT}`.
Public exposure is handled by:

- `tailscale serve --bg 8443 http://127.0.0.1:8420`
- `tailscale funnel --bg 8443`
```

- [ ] **Step 5: Run tests to verify they pass**

Run:

```bash
python -m pytest tests/test_shim_http_surface.py -q
```

Expected:

- PASS

- [ ] **Step 6: Commit**

```bash
git add services/obsidian-mcp-oauth-shim/app/config.py services/obsidian-mcp-oauth-shim/README.md tests/test_shim_http_surface.py
git commit -m "test: preserve shim http surface on linux packaging"
```

---

### Task 6: Finish Documentation and End-to-End Verification Commands

**Files:**
- Modify: `README.md`
- Modify: `config/global.env.example`
- Test: `tests/test_setup_script.py`

- [ ] **Step 1: Write the failing documentation seam test**

```python
def test_setup_docs_reference_doctor_service_and_funnel():
    readme = __import__("pathlib").Path("README.md").read_text()

    assert "./setup.sh" in readme
    assert "ragcli doctor" in readme
    assert "ragcli service status" in readme
    assert "tailscale funnel" in readme
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python -m pytest tests/test_setup_script.py::test_setup_docs_reference_doctor_service_and_funnel -q
```

Expected:

- FAIL because the README has not been updated yet.

- [ ] **Step 3: Update README with the Linux-first flow**

```md
## Linux bootstrap

- `./setup.sh`
- `ragcli doctor`
- `ragcli service status`

This setup flow:

- prepares the shared runtime
- updates `config/global.env`
- installs `systemd --user` units
- publishes the OAuth shim through Tailscale Funnel
```

- [ ] **Step 4: Run the full test suite for this project slice**

Run:

```bash
python -m pytest tests/test_kb_registry_linux_config.py tests/test_ingest_docling_config.py tests/test_ragcli_doctor_service.py tests/test_setup_script.py tests/test_shim_http_surface.py -q
```

Expected:

- PASS

- [ ] **Step 5: Run manual verification commands**

Run:

```bash
./setup.sh
ragcli doctor
ragcli service status
tailscale funnel status
curl -sS -i https://your-node.your-tailnet.ts.net/
curl -sS -i https://your-node.your-tailnet.ts.net/.well-known/oauth-authorization-server
```

Expected:

- `setup.sh` completes without fatal errors
- `ragcli doctor` reports runtime/config/services/publish healthy
- `ragcli service status` shows active units
- `tailscale funnel status` shows the configured public listener
- both `curl` commands return HTTP 200

- [ ] **Step 6: Commit**

```bash
git add README.md config/global.env.example tests/test_setup_script.py
git commit -m "docs: document linux bootstrap and verification flow"
```

---

## Self-Review Checklist

- Spec coverage:
  - Linux bootstrap in `setup.sh`: covered by Tasks 1, 4, 6
  - Centralized OS-agnostic config: covered by Tasks 1 and 2
  - Docling OCR/ASR Linux behavior: covered by Task 2
  - MCP surface preservation: covered by Task 5
  - `systemd --user` services: covered by Task 3
  - Tailscale Serve/Funnel publish: covered by Task 4 and manual verification in Task 6
  - `ragcli doctor` and `ragcli service`: covered by Tasks 1 and 3
- Placeholder scan:
  - No `TODO`/`TBD` placeholders intentionally left in task steps.
- Type consistency:
  - All planned helper names are reused consistently across tasks: `build_parser`, `cmd_doctor`, `cmd_service_status`, `install_user_units`, `configure_tailscale_funnel`, `_docling_ocr_engine`, `_docling_asr_engine`.
