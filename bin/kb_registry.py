from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from pathlib import PurePosixPath

import yaml

HOME = Path(__file__).resolve().parent.parent
CONFIG = HOME / "config" / "global.env"
REGISTRY = HOME / "config" / "registry.yaml"
KB_ROOT = HOME / "kb"
ENGINE_DIR = HOME / ".venv-engine"
ENGINE_PYTHON = ENGINE_DIR / "bin" / "python"
LIGHTRAG_SERVER = ENGINE_DIR / "bin" / "lightrag-server"
MCP_SCRIPT = HOME / "mcp" / "lightrag_mcp.py"
INGEST_SCRIPT = HOME / "bin" / "ingest.py"

DERIVED_SUBDIRS = ("text", "pdf", "images", "office", "audio", "cache", "manifests")


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def global_env() -> dict[str, str]:
    env = load_env(CONFIG)
    env.setdefault("ENGINE_PYTHON", str(ENGINE_PYTHON))
    env.setdefault("LIGHTRAG_SERVER_BIN", str(LIGHTRAG_SERVER))
    env.setdefault("DOCLING_PYTHON", env["ENGINE_PYTHON"])
    env.setdefault("DOCLING_LANG", "it,en")
    env.setdefault("DOCLING_TIMEOUT", "3600")
    env.setdefault("LLM_PROVIDER", "ollama")
    env.setdefault("OLLAMA_HOST", "http://localhost:11434")
    env.setdefault("LLM_MODEL", "granite4.1:3b")
    env.setdefault("EMBEDDING_MODEL", "qwen3-embedding:0.6b")
    env.setdefault("EMBEDDING_DIM", "1024")
    env.setdefault("OLLAMA_LLM_NUM_CTX", "8192")
    env.setdefault("OLLAMA_LLM_NUM_PREDICT", "4096")
    env.setdefault("OLLAMA_EMBEDDING_NUM_CTX", "8192")
    env.setdefault("OPENROUTER_HOST", "https://openrouter.ai/api/v1")
    env.setdefault("OPENROUTER_LLM_MODEL", "openrouter/owl-alpha")
    env.setdefault("OPENROUTER_EMBEDDING_MODEL", "nvidia/llama-nemotron-embed-vl-1b-v2:free")
    env.setdefault("OPENROUTER_EMBEDDING_DIM", "2048")
    env.setdefault("RERANK_BINDING", "null")
    env.setdefault("LLM_TIMEOUT", "900")
    env.setdefault("TIMEOUT", "900")
    env.setdefault("MAX_ASYNC", "1")
    env.setdefault("EMBEDDING_TIMEOUT", "120")
    env.setdefault("SUMMARY_LANGUAGE", "Italian")
    env.setdefault("MAX_GLEANING", "1")
    env.setdefault("CHUNK_SIZE", "1200")
    env.setdefault("CHUNK_OVERLAP_SIZE", "150")
    env.setdefault("COSINE_THRESHOLD", "0.25")
    env.setdefault("BASE_PORT", "9621")
    return env


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def normalize_kb(entry: dict, env: dict[str, str] | None = None) -> dict:
    env = env or global_env()
    source_raw = entry.get("source_dir") or entry.get("source_folder")
    if not source_raw:
        raise ValueError(f"KB '{entry.get('name', '?')}' senza source_dir/source_folder")
    source_dir = Path(source_raw).expanduser().resolve()
    data_raw = entry.get("data_dir")
    data_dir = Path(data_raw).expanduser().resolve() if data_raw else (source_dir / ".lightrag")
    derived_dir = Path(entry.get("derived_dir") or (data_dir / "derived")).expanduser().resolve()
    rag_storage = Path(entry.get("rag_storage") or (data_dir / "rag_storage")).expanduser().resolve()
    return {
        "name": entry["name"],
        "source_dir": str(source_dir),
        "data_dir": str(data_dir),
        "derived_dir": str(derived_dir),
        "rag_storage": str(rag_storage),
        "port": int(entry.get("port") or env["BASE_PORT"]),
        "exclude": list(entry.get("exclude", [])),
    }


def _registry_entry(kb: dict) -> dict:
    entry = {
        "name": kb["name"],
        "source_dir": kb["source_dir"],
        "data_dir": kb["data_dir"],
        "derived_dir": kb["derived_dir"],
        "rag_storage": kb["rag_storage"],
        "port": int(kb["port"]),
    }
    if kb.get("exclude"):
        entry["exclude"] = list(kb["exclude"])
    return entry


def load_registry(env: dict[str, str] | None = None) -> dict:
    env = env or global_env()
    if not REGISTRY.exists():
        return {"kbs": []}
    data = yaml.safe_load(REGISTRY.read_text()) or {}
    kbs = []
    for entry in data.get("kbs", []):
        kbs.append(normalize_kb(entry, env))
    return {"kbs": kbs}


def save_registry(data: dict) -> None:
    header = "# Registro LightRAG-KB (gestito da ragcli, editabile a mano).\n"
    payload = {"kbs": [_registry_entry(kb) for kb in data.get("kbs", [])]}
    REGISTRY.write_text(header + yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))


def find_kb(registry: dict, name: str) -> dict | None:
    return next((kb for kb in registry.get("kbs", []) if kb["name"] == name), None)


def next_port(registry: dict, env: dict[str, str] | None = None) -> int:
    env = env or global_env()
    used = {int(kb["port"]) for kb in registry.get("kbs", [])}
    port = int(env["BASE_PORT"])
    while port in used:
        port += 1
    return port


def ensure_kb_dirs(kb: dict) -> None:
    data_dir = Path(kb["data_dir"])
    derived_dir = Path(kb["derived_dir"])
    rag_storage = Path(kb["rag_storage"])
    data_dir.mkdir(parents=True, exist_ok=True)
    derived_dir.mkdir(parents=True, exist_ok=True)
    rag_storage.mkdir(parents=True, exist_ok=True)
    for subdir in DERIVED_SUBDIRS:
        (derived_dir / subdir).mkdir(parents=True, exist_ok=True)


def storage_mode(kb: dict) -> str:
    data_dir = Path(kb["data_dir"])
    source_dir = Path(kb["source_dir"])
    return "source" if is_relative_to(data_dir, source_dir) else "repo"


def split_exclude_specs(raw_patterns: list[str]) -> list[str]:
    specs: list[str] = []
    for raw in raw_patterns:
        for chunk in raw.replace(";", ",").split(","):
            spec = chunk.strip()
            if spec:
                specs.append(spec)
    return specs


def canonicalize_exclude_specs(specs: list[str]) -> tuple[list[str], bool]:
    canonical: list[str] = []
    seen: set[str] = set()
    changed = False
    for spec in specs:
        normalized = spec.strip()
        if normalized.endswith("/*") and not normalized.endswith("/**"):
            normalized = normalized[:-1]
        if normalized != spec:
            changed = True
        if normalized in seen:
            changed = True
            continue
        seen.add(normalized)
        canonical.append(normalized)
    return canonical, changed


def normalize_exclude_specs_for_kb(specs: list[str], source_dir: Path) -> tuple[list[str], bool]:
    canonical, changed = canonicalize_exclude_specs(specs)
    normalized: list[str] = []
    seen: set[str] = set()
    for spec in canonical:
        upgraded = spec
        if "/" not in spec and not any(char in spec for char in "*?[]"):
            candidate = source_dir / spec
            try:
                if candidate.is_dir():
                    upgraded = f"{spec}/"
            except OSError:
                pass
        if upgraded != spec:
            changed = True
        if upgraded in seen:
            changed = True
            continue
        seen.add(upgraded)
        normalized.append(upgraded)
    return normalized, changed


def _source_rel_dirs(source_dir: Path, data_dir: Path) -> list[str]:
    data_root = data_dir.resolve()
    rel_dirs: list[str] = []
    seen_dirs: set[Path] = set()
    for root, dirs, _files in os.walk(source_dir, topdown=True, followlinks=True):
        root_path = Path(root)
        try:
            root_resolved = root_path.resolve()
        except OSError:
            dirs[:] = []
            continue
        if root_resolved in seen_dirs:
            dirs[:] = []
            continue
        seen_dirs.add(root_resolved)

        kept_dirs: list[str] = []
        for dirname in dirs:
            child = root_path / dirname
            try:
                child_resolved = child.resolve()
            except OSError:
                continue
            if is_relative_to(child_resolved, data_root):
                continue
            kept_dirs.append(dirname)
            rel_dirs.append(child.relative_to(source_dir).as_posix())
        dirs[:] = kept_dirs
    rel_dirs.sort()
    return rel_dirs


def _folder_rule_matches(rel_dir: str, folder_spec: str) -> bool:
    candidate = PurePosixPath(rel_dir)
    if "/" in folder_spec:
        target = rel_dir
    else:
        if len(candidate.parts) != 1:
            return False
        target = candidate.name
    return fnmatch.fnmatch(target, folder_spec)


def _ancestor_dirs(rel_path: str) -> list[str]:
    path = PurePosixPath(rel_path)
    parents = list(path.parents[:-1])
    return [parent.as_posix() for parent in reversed(parents) if parent.as_posix() not in {".", ""}]


def dir_is_excluded(rel_dir: str, specs: list[str]) -> bool:
    canonical, _ = canonicalize_exclude_specs(specs)
    candidates = _ancestor_dirs(rel_dir) + [rel_dir]
    for spec in canonical:
        if not spec.endswith("/"):
            continue
        folder_spec = spec[:-1]
        if any(_folder_rule_matches(candidate, folder_spec) for candidate in candidates):
            return True
    return False


def path_is_excluded(rel_path: str, specs: list[str]) -> bool:
    canonical, _ = canonicalize_exclude_specs(specs)
    basename = PurePosixPath(rel_path).name
    for spec in canonical:
        if spec.endswith("/"):
            if any(_folder_rule_matches(candidate, spec[:-1]) for candidate in _ancestor_dirs(rel_path)):
                return True
            continue
        if "/" in spec:
            if fnmatch.fnmatch(rel_path, spec):
                return True
            continue
        if fnmatch.fnmatch(basename, spec):
            return True
    return False


def expand_exclude_specs(specs: list[str], source_dir: Path, data_dir: Path) -> list[str]:
    canonical, _ = canonicalize_exclude_specs(specs)
    expanded: list[str] = []
    seen: set[str] = set()
    source_dirs = _source_rel_dirs(source_dir, data_dir)
    for spec in canonical:
        if not spec.endswith("/"):
            if spec not in seen:
                seen.add(spec)
                expanded.append(spec)
            continue
        folder_spec = spec[:-1]
        matched = False
        for rel_dir in source_dirs:
            if _folder_rule_matches(rel_dir, folder_spec):
                display = f"{rel_dir}/"
                if display in seen:
                    continue
                seen.add(display)
                expanded.append(display)
                matched = True
        if not matched and spec not in seen:
            seen.add(spec)
            expanded.append(spec)
    return expanded


def write_kb_env(kb: dict, env: dict[str, str] | None = None) -> Path:
    env = env or global_env()
    ensure_kb_dirs(kb)
    provider = env.get("LLM_PROVIDER", "ollama")
    if provider == "openrouter":
        llm_binding = "openai"
        llm_host = env["OPENROUTER_HOST"]
        llm_model = env["OPENROUTER_LLM_MODEL"]
        llm_api_key = env.get("OPENROUTER_API_KEY", "")
        embedding_binding = "openai"
        embedding_host = env["OPENROUTER_HOST"]
        embedding_model = env["OPENROUTER_EMBEDDING_MODEL"]
        embedding_dim = env["OPENROUTER_EMBEDDING_DIM"]
        embedding_api_key = llm_api_key
        embedding_use_base64 = env.get("EMBEDDING_USE_BASE64", "false")
    else:
        llm_binding = "ollama"
        llm_host = env["OLLAMA_HOST"]
        llm_model = env["LLM_MODEL"]
        llm_api_key = ""
        embedding_binding = "ollama"
        embedding_host = env["OLLAMA_HOST"]
        embedding_model = env["EMBEDDING_MODEL"]
        embedding_dim = env["EMBEDDING_DIM"]
        embedding_api_key = ""
        embedding_use_base64 = env.get("EMBEDDING_USE_BASE64", "true")

    lines = [
        "# Generato da ragcli — non editare a mano.",
        f"ENGINE_PYTHON={env['ENGINE_PYTHON']}",
        f"LIGHTRAG_SERVER_BIN={env['LIGHTRAG_SERVER_BIN']}",
        f"DOCLING_PYTHON={env['DOCLING_PYTHON']}",
        f"DOCLING_LANG={env['DOCLING_LANG']}",
        f"DOCLING_TIMEOUT={env['DOCLING_TIMEOUT']}",
        "HOST=127.0.0.1",
        f"PORT={kb['port']}",
        f"SOURCE_DIR={kb['source_dir']}",
        f"DATA_DIR={kb['data_dir']}",
        f"DERIVED_DIR={kb['derived_dir']}",
        f"WORKING_DIR={kb['rag_storage']}",
        f"INPUT_DIR={kb['source_dir']}",
        f"RAG_STORAGE_DIR={kb['rag_storage']}",
        f"LLM_BINDING={llm_binding}",
        f"LLM_BINDING_HOST={llm_host}",
        f"LLM_MODEL={llm_model}",
        f"EMBEDDING_BINDING={embedding_binding}",
        f"EMBEDDING_BINDING_HOST={embedding_host}",
        f"EMBEDDING_MODEL={embedding_model}",
        f"EMBEDDING_DIM={embedding_dim}",
        f"EMBEDDING_USE_BASE64={embedding_use_base64}",
        f"RERANK_BINDING={env['RERANK_BINDING']}",
        f"LLM_TIMEOUT={env['LLM_TIMEOUT']}",
        f"TIMEOUT={env['TIMEOUT']}",
        f"MAX_ASYNC={env['MAX_ASYNC']}",
        f"EMBEDDING_TIMEOUT={env['EMBEDDING_TIMEOUT']}",
        f"SUMMARY_LANGUAGE={env['SUMMARY_LANGUAGE']}",
        f"MAX_GLEANING={env['MAX_GLEANING']}",
        f"CHUNK_SIZE={env['CHUNK_SIZE']}",
        f"CHUNK_OVERLAP_SIZE={env['CHUNK_OVERLAP_SIZE']}",
        f"COSINE_THRESHOLD={env['COSINE_THRESHOLD']}",
    ]
    if provider == "ollama":
        lines.extend(
            [
                f"OLLAMA_LLM_NUM_CTX={env['OLLAMA_LLM_NUM_CTX']}",
                f"OLLAMA_LLM_NUM_PREDICT={env['OLLAMA_LLM_NUM_PREDICT']}",
                f"OLLAMA_EMBEDDING_NUM_CTX={env['OLLAMA_EMBEDDING_NUM_CTX']}",
            ]
        )
    if llm_api_key:
        lines.append(f"LLM_BINDING_API_KEY={llm_api_key}")
    if embedding_api_key:
        lines.append(f"EMBEDDING_BINDING_API_KEY={embedding_api_key}")
    env_path = Path(kb["data_dir"]) / ".env"
    env_path.write_text("\n".join(lines) + "\n")
    return env_path


def merged_runtime_env(kb: dict, env: dict[str, str] | None = None) -> dict[str, str]:
    env = env or global_env()
    merged = os.environ.copy()
    merged.update(env)
    merged.update(load_env(Path(kb["data_dir"]) / ".env"))
    return merged
