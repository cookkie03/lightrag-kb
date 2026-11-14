import requests
import pytest
from bin.kb_registry import global_env

def test_ollama_server_reachable():
    env = global_env()
    host = env.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    if "localhost" in host:
        host = host.replace("localhost", "127.0.0.1")
    try:
        # Check that server responds to version
        ver_res = requests.get(f"{host}/api/version", timeout=5)
        assert ver_res.status_code == 200, f"Ollama /api/version failed with code {ver_res.status_code}"
        assert "version" in ver_res.json(), "Ollama version missing from response"

        # Check tags list
        res = requests.get(f"{host}/api/tags", timeout=5)
        assert res.status_code == 200, f"Ollama /api/tags failed with code {res.status_code}"
        data = res.json()
        assert "models" in data, "Ollama response missing 'models' key"

        # Check that the configured embedding model actually generates an embedding
        embed_model = env.get("EMBEDDING_MODEL", "embeddinggemma-2:latest")
        embed_res = requests.post(
            f"{host}/api/embeddings",
            json={"model": embed_model, "prompt": "healthcheck"},
            timeout=15,
        )
        assert embed_res.status_code == 200, f"Ollama /api/embeddings returned {embed_res.status_code}"
        embed_data = embed_res.json()
        assert "embedding" in embed_data and len(embed_data["embedding"]) > 0, "No embedding returned"
    except requests.exceptions.ConnectionError:
        pytest.fail(f"Could not connect to Ollama at {host}. Is Ollama running?")
