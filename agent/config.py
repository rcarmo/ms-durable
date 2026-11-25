"""Server-only configuration and read-only reuse of the Pi Copilot login."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    data: Path = field(default_factory=lambda: Path(os.getenv("MS_DURABLE_DATA", "data")).resolve())
    working_directory: Path = field(
        default_factory=lambda: Path(os.getenv("MS_DURABLE_CWD", ".")).resolve()
    )
    auth_file: Path = field(
        default_factory=lambda: Path(
            os.getenv("MS_DURABLE_AUTH_FILE", str(Path.home() / ".pi/agent/auth.json"))
        )
    )
    mcp_file: Path | None = field(
        default_factory=lambda: (
            Path(os.environ["MS_DURABLE_MCP_FILE"]) if os.getenv("MS_DURABLE_MCP_FILE") else None
        )
    )
    skill_directories: list[str] = field(
        default_factory=lambda: [
            str(Path(p).resolve())
            for p in os.getenv("MS_DURABLE_SKILLS", "").split(os.pathsep)
            if p
        ]
    )
    agent_directories: list[str] = field(
        default_factory=lambda: [
            str(Path(p).resolve())
            for p in os.getenv("MS_DURABLE_AGENTS", "").split(os.pathsep)
            if p
        ]
    )
    model: str = field(default_factory=lambda: os.getenv("MS_DURABLE_MODEL", ""))
    access_token: str = field(default_factory=lambda: os.getenv("MS_DURABLE_TOKEN", ""))
    auth_required: bool = field(
        default_factory=lambda: os.getenv("MS_DURABLE_AUTH_REQUIRED", "1") != "0"
    )
    providers_file: Path | None = field(
        default_factory=lambda: (
            Path(os.environ["MS_DURABLE_PROVIDERS_FILE"])
            if os.getenv("MS_DURABLE_PROVIDERS_FILE")
            else None
        )
    )

    def token(self) -> str:
        """Pi refresh is the GitHub OAuth token; access is a short-lived CAPI token."""
        if os.getenv("COPILOT_GITHUB_TOKEN"):
            return os.environ["COPILOT_GITHUB_TOKEN"]
        auth = json.loads(self.auth_file.read_text())
        entry = auth.get("github-copilot", {})
        if entry.get("type") != "oauth" or not entry.get("refresh"):
            raise ValueError(
                "No reusable GitHub Copilot login; set MS_DURABLE_AUTH_FILE or COPILOT_GITHUB_TOKEN"
            )
        return entry["refresh"]

    def mcp_servers(self) -> dict:
        if not self.mcp_file:
            return {}
        source = json.loads(self.mcp_file.read_text())
        result = {}
        for name, raw in source.get("mcpServers", source.get("servers", {})).items():
            if raw.get("disabled"):
                continue
            server = {
                k: v
                for k, v in raw.items()
                if k
                in {"type", "url", "command", "args", "env", "cwd", "headers", "timeout", "tools"}
            }
            server.setdefault("tools", ["*"])
            if "url" in server:
                server.setdefault("type", "http")
            else:
                server.setdefault("type", "stdio")
            bearer = raw.get("auth", {})
            token = None
            if isinstance(bearer, dict) and bearer.get("type") == "bearer":
                token = bearer.get("token")
            elif bearer == "bearer":
                env_name = raw.get("bearerTokenEnv", "")
                token = os.environ.get(env_name)
                if not token:
                    raise ValueError(f"MCP server {name} requires environment variable {env_name}")
            if token:
                server.setdefault("headers", {})["Authorization"] = "Bearer " + token
            result[name] = server
        return result
