"""Translate server-owned profiles to native Copilot BYOK configuration.

No inference HTTP implementation: route selection, streaming, tools, retries and
compaction are implemented by Copilot. Secret values never enter the catalogue.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


class Providers:
    def __init__(self, path: Path | None):
        self.path = path
        self.config = json.loads(path.read_text()) if path else {}
        self.credentials = {}
        self.secrets = set()
        self.connections = []
        for name, value in self.config.get("providers", {}).items():
            if "/" in name or not name:
                raise ValueError("Provider names must be nonempty and cannot contain /")
            if value.get("type") not in {"azure", "openai", "anthropic", "google"}:
                raise ValueError("Unsupported native provider type")
            provider = {
                key: val
                for key, val in value.items()
                if key in {"type", "base_url", "wire_api", "transport", "model_provider", "azure"}
            }
            provider["name"] = name
            env = value.get("api_key_env")
            if env:
                if not os.getenv(env):
                    raise ValueError(f"Missing API key environment variable: {env}")
                provider["api_key"] = os.environ[env]
                self.secrets.add(os.environ[env])
            bearer_env = value.get("bearer_token_env")
            if bearer_env:
                if not os.getenv(bearer_env):
                    raise ValueError(f"Missing bearer token environment variable: {bearer_env}")
                provider["bearer_token"] = os.environ[bearer_env]
                self.secrets.add(os.environ[bearer_env])
            if value.get("auth") == "entra":
                from azure.identity.aio import DefaultAzureCredential

                credential = DefaultAzureCredential()
                self.credentials[name] = credential
                scope = value.get("scope", "https://cognitiveservices.azure.com/.default")

                async def token(args, credential=credential, scope=scope):
                    value = (await credential.get_token(scope)).token
                    self.secrets.add(value)
                    return value

                provider["bearer_token_provider"] = token
            self.connections.append(provider)
        names = {p["name"] for p in self.connections}
        self.models = []
        for value in self.config.get("models", []):
            if value.get("provider") not in names or not value.get("id") or "/" in value["id"]:
                raise ValueError(
                    "Models must reference a configured provider and a provider-local id"
                )
            self.models.append(
                {
                    key: val
                    for key, val in value.items()
                    if key
                    in {
                        "id",
                        "provider",
                        "wire_model",
                        "model_id",
                        "name",
                        "max_prompt_tokens",
                        "max_context_window_tokens",
                        "max_output_tokens",
                        "capabilities",
                    }
                }
            )

    def inference(self):
        return {"providers": self.connections, "models": self.models} if self.connections else {}

    def catalogue(self):
        return [
            {
                "id": f"{m['provider']}/{m['id']}",
                "name": m.get("name", f"{m['provider']}/{m['id']}"),
                "provider": m["provider"],
            }
            for m in self.models
        ]

    async def close(self):
        for credential in self.credentials.values():
            await credential.close()
