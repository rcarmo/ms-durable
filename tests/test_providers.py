import json

import pytest

from agent.providers import Providers


def test_native_foundry_mapping_and_no_catalogue_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("FOUNDRY_TEST_KEY", "server-only-secret")
    config = tmp_path / "providers.json"
    config.write_text(
        json.dumps(
            {
                "providers": {
                    "foundry": {
                        "type": "azure",
                        "base_url": "https://example.openai.azure.com",
                        "wire_api": "responses",
                        "api_key_env": "FOUNDRY_TEST_KEY",
                        "azure": {"api_version": "2025-04-01-preview"},
                    }
                },
                "models": [
                    {
                        "provider": "foundry",
                        "id": "reasoning",
                        "wire_model": "deployment-42",
                        "model_id": "o3",
                        "name": "Foundry reasoning",
                        "max_context_window_tokens": 200000,
                    }
                ],
            }
        )
    )
    providers = Providers(config)
    native = providers.inference()
    assert native["providers"][0]["api_key"] == "server-only-secret"
    assert native["providers"][0]["azure"]["api_version"] == "2025-04-01-preview"
    assert native["models"][0]["wire_model"] == "deployment-42"
    assert providers.catalogue()[0]["id"] == "foundry/reasoning"
    assert "server-only-secret" not in json.dumps(providers.catalogue())
    assert "api_key" not in config.read_text().replace("api_key_env", "")


def test_missing_foundry_secret_fails_closed(tmp_path):
    path = tmp_path / "providers.json"
    path.write_text(
        json.dumps(
            {"providers": {"f": {"type": "azure", "api_key_env": "ABSENT_MS_DURABLE_TEST_KEY"}}}
        )
    )
    with pytest.raises(ValueError, match="Missing API key"):
        Providers(path)


async def test_workspace_adapter_protects_ancestor_operations(store, tmp_path):
    from agent.session_fs import SQLiteSessionFS

    root = tmp_path / "workspace"
    state = root / "state"
    state.mkdir(parents=True)
    protected = state / "token"
    protected.write_text("private")
    fs = SQLiteSessionFS(store, "protected", [root], [protected])
    with pytest.raises(PermissionError):
        await fs.read_file(str(protected))
    with pytest.raises(PermissionError):
        await fs.rm(str(state), recursive=True)
    with pytest.raises(PermissionError):
        await fs.rename(str(state), str(root / "renamed"))
    normal = root / "note.md"
    await fs.write_file(str(normal), "# Note")
    assert await fs.read_file(str(normal)) == "# Note"
    assert (await fs.stat(str(normal))).is_file
    assert (await fs.readdir_with_types(str(root)))[0].name
    assert protected.read_text() == "private"


async def test_entra_refresh_callback_and_close(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import azure.identity.aio

    scopes = []

    class Credential:
        closed = False

        async def get_token(self, scope):
            scopes.append(scope)
            return SimpleNamespace(token="synthetic-entra-access-token")

        async def close(self):
            self.closed = True

    credential = Credential()
    monkeypatch.setattr(azure.identity.aio, "DefaultAzureCredential", lambda: credential)
    path = tmp_path / "entra.json"
    path.write_text(
        json.dumps(
            {
                "providers": {
                    "entra": {
                        "type": "azure",
                        "auth": "entra",
                        "base_url": "https://example.openai.azure.com",
                    }
                },
                "models": [],
            }
        )
    )
    providers = Providers(path)
    callback = providers.inference()["providers"][0]["bearer_token_provider"]
    for _ in range(2):
        assert (
            await callback({"provider_name": "entra", "session_id": "test"})
            == "synthetic-entra-access-token"
        )
    assert scopes == ["https://cognitiveservices.azure.com/.default"] * 2
    assert "synthetic-entra-access-token" not in json.dumps(providers.catalogue())
    await providers.close()
    assert credential.closed
