# MS Durable

MS Durable is a small asyncio/FastAPI host for the Microsoft Copilot SDK, with browser chat and SQLite session storage. **This is not an official Microsoft project.** I built it partly as a tech demo for the SDK and partly because I needed a reusable `pi-durable`-like service and starter chat app for other projects.

![Architecture: browser, FastAPI host, native Copilot runtime and SQLite](docs/architecture.svg)

Copilot handles inference, the agent loop, coding tools, `AGENTS.md`, skills, MCP, compaction and transcript recovery. My `asyncio` shell adds a session list, a durable queue, permission prompts and a SQLite adapter for the native session filesystem; it also records events for the UI. It's an _approximation_ of Pi's durable storage: the Copilot runtime is a separate process, so transactional guarantees are... weak, but good enough to play around with and prototype.

## Run from source

On Linux, install Python 3.11+ and Bun. `make install` uses [requirements.lock](requirements.lock) and [bun.lock](bun.lock).

```sh
make install
make check
make run
```

Open `http://127.0.0.1:8015` and enter the token from `data/web-token`, or set `MS_DURABLE_TOKEN` before starting. The server doesn't print the token or put it in a URL (for, well, obvious reasons). The UI keeps it in browser session storage because that was the laziest thing I could do.

Set `COPILOT_GITHUB_TOKEN` for Copilot authentication. If it's unset, the server reuses the Pi login in `~/.pi/agent/auth.json` (another lazy shortcut). For your own inference endpoint, see [Azure Foundry and Azure OpenAI](docs/reference.md#azure-foundry-and-azure-openai). The source install includes Azure Identity so I can use a managed identity on Azure and skip the key management shenanigans.

Use one Uvicorn worker per data directory. An advisory lock rejects a second server using the same directory (humans and agents get creative, especially when running tests). Do not restart the server from one of its own active tools. It won't like it.

For remote access, use HTTPS directly or through a reverse proxy that forwards server-sent events (SSE) without buffering. The app does not manage certificates or install a service (I had Azure PaaS or an App Gateway in mind). See [configuration](docs/reference.md#configuration) for bind addresses, TLS, credentials and workspace paths.

## Run in a container

The Linux amd64 image includes the pinned Copilot runtime and runs as UID/GID `10001`. Keep the state directory `/state` and the coding workspace `/work` in separate persistent volumes. Writable bind mounts must be writable by UID `10001`.

```sh
docker run --init --rm -p 127.0.0.1:8015:8015 \
  --env-file ./ms-durable.env \
  -v ms-durable-state:/state -v ms-durable-work:/work \
  ghcr.io/rcarmo/ms-durable:v0.2.0
```

Set `COPILOT_GITHUB_TOKEN` and `MS_DURABLE_TOKEN` in the protected environment file. For your own provider, mount the configuration read-only, set `MS_DURABLE_PROVIDERS_FILE` and supply the required credential variables. If `MS_DURABLE_TOKEN` is unset, the app generates `/state/web-token`. Keep credentials out of the image.

The [Publish container workflow](.github/workflows/container.yml) smoke-tests the image before pushing to GHCR. It runs on version tags or manually from `main` (see [publishing options](docs/reference.md#container-publishing)).

## What the browser adds

I may have gone a bit overboard for a baseline shell. Chats have streaming replies, expandable tool calls, approvals, cancellation, file sharing, per-session plans and a per-turn timing chart of model calls and tools. The UI includes a model picker and native context and compaction controls. Type `/` in the composer for shortcuts. Use the pencil in the header to set an optional `@name` and description. Model changes take effect between turns.

**Multiplayer:** agents can send a message to another session and reply once to the original sender. Opt in on a named session to let other agents delegate work to it in parallel. This is for those of you who like having sub-agents or just burn up tokens.

See the [browser and tool reference](docs/reference.md#browser-and-application-tools) for limits, tool names and delegation. [MCP and skills](docs/reference.md#mcp-and-skills) covers native extensions.

## State and safety

All authenticated browsers share the same sessions. This is a single-user app. **Tool approvals and the filesystem adapter are not a sandbox:** native shell tools run as the server user. Use a dedicated OS account or an isolated container or VM with restricted mounts and network access before running untrusted prompts, repositories, skills or MCP tools.

The database stores native transcripts as well as chat history and queue state. Back up the state directory after shutdown or use SQLite's online backup API; don't copy the live database without its WAL. Waiting inputs resume after restart, except queued delegations. Interrupted inputs are never sent again automatically. Check the transcript and any external effects before retrying.

Don't give gasoline and matches to this thing. It's a reusable module for me to build upon, not an enterprise-grade thing. Yet.

The reference covers [recovery and backups](docs/reference.md#persistence-and-failure-handling), [security controls](docs/reference.md#security-boundaries) and [native compaction and prompt caching](docs/reference.md#compaction-and-prompt-prefix-caching), including what the tests do and don't establish.

## Tests

`make check` runs lint and offline tests. See [verification](docs/reference.md#verification) for the native runtime tests, browser tests and optional performance and live probes.

## Licence

[MIT](LICENSE).
