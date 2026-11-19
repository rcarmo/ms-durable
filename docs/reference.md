# MS Durable reference

See the [README](../README.md) to start the app.

## Configuration

| Environment variable | Default / purpose |
| --- | --- |
| `MS_DURABLE_DATA` | `./data`; persistent database, web token and native runtime state |
| `MS_DURABLE_CWD` | Current directory; native coding workspace |
| `MS_DURABLE_HOST` | `127.0.0.1`; use a deliberate bind address for remote access |
| `MS_DURABLE_PORT` | `8015` |
| `MS_DURABLE_TOKEN` | Optional server-owned web bearer token |
| `MS_DURABLE_AUTH_FILE` | `~/.pi/agent/auth.json`; read-only credential source |
| `MS_DURABLE_PROVIDERS_FILE` | Optional server-owned BYOK JSON file |
| `MS_DURABLE_MCP_FILE` | Optional server-owned MCP JSON file |
| `MS_DURABLE_SKILLS` | OS-path-separated native skill directories |
| `MS_DURABLE_AGENTS` | OS-path-separated directories for native Copilot custom agents |
| `MS_DURABLE_TLS_KEY`, `MS_DURABLE_TLS_CERT` | Optional uvicorn TLS files |

## Browser and application tools

The UI renders messages with bundled Markdown-it and disables raw HTML. On reconnect it reconciles the snapshot and resumes the server-sent event stream from its last sequence number.

- **Appearance:** follow the system theme or choose light or dark in Settings.
- **Commands:** type `/` in the composer for shortcuts to New chat, Plan, Timing, Context, Rename and Settings.
- **Images:** attach PNG, JPEG, GIF, WebP or SVG (up to three per message, 1 MiB each). The original bytes live in SQLite and an authenticated media route serves them. The browser displays SVG in image context. Images are not added automatically to model requests. The model can inspect them on demand with the attachment tool.
- **Tool output:** activity rows show a plain-language summary. Expand a tool call for pretty-printed JSON or plain text in **Readable**. Switch to **Raw** to inspect the recorded arguments, result and error. The paginated session event log is under **Settings → Diagnostics**.
- **Plan:** open the checklist from the chat header. It docks to the right of chat on desktop and stacks above it on narrow screens. The agent’s changes appear live; each conversation keeps its own plan in SQLite.
- **Timing:** the chart icon in the header (or `/timing`) replaces chat with a per-turn Gantt chart of model calls, tools, approvals and compactions. Model calls are split into waiting for the first token and streaming, using the SDK's own duration and time-to-first-token. Arrows link each tool to the call that requested it and the call that received its result. Hover over bars, row labels, arrows, turn headings and idle gaps for token counts, cache use, request sizes, delays and outcomes; tool arguments, results and error messages are never sent to the timing view. Overlapping waits share a translucent band; the turn total counts their common time once. Each linked tool resumes on its own answer. Unrelated tools can finish inside the band. Requests without a recorded tool call ID remain unattributed. Scroll up to load older turns; running turns update live. Turns recorded before this release have no approval answer times, and their model calls are placed to end at the recorded response.
- **Message references:** use the **Copy** icon for text. Select the **Reference** icon or the timestamp to insert a reference into the composer. The composer and transcript show a short turn ID, such as `#e1b4c29a` (seven ID characters plus `a` for answer or `u` for user). Stored references retain the full chat ID, turn ID and role. Select the reference to jump to the message in the same chat. Answers written before timestamp support have no recorded completion time.
- **Named sessions:** use the pencil in the header to rename a session and update its `@name` without changing its ID. When Luna is available, select **Summarise recent messages with Luna** to send excerpts from up to 20 recent settled prompt turns. The host reuses a summary for the same session, summary model and redacted excerpts until it is evicted or the server restarts. Enter an optional unique `@name` before the first send. Names are case-insensitive.
- **Multiplayer:** The sending timeline shows each message and its destination; the receiving timeline shows the sender's name. A `reply_chat` reply appears in both timelines and queues one turn for the original sender. That turn can act on the result but cannot send another chat request or reply.

Application tools are visible to every application session:

- `list_sessions`, `observe_session`: session inspection, attachment metadata and a bounded timeline page. Pass `next_after` as `after_seq` for the next page. To search older history, call `observe_session` with `query` (for example `backup checksum`) and optionally `session_id` or `limit`. SQLite FTS5 returns ranked excerpts with turn IDs and event sequence numbers. All search words must match. Prompts, final answers, errors and non-streaming event text are searchable; streaming fragments and attachment bytes are excluded. Existing timelines are indexed on first startup.
- `plan`: read (`read`), replace (`update`) or atomically edit (`patch`) the current conversation’s plan. Patches use 1-based indices and support `add`, `update` and `remove`. At most one item can be `in_progress`.
- `read_attachment`: inspect a stored attachment by chat ID (or `@handle`), turn ID and ordinal. The SDK returns raster images as image results and SVG as bounded source text.
- `attach_file`: share a workspace file on the current turn without an approval prompt. Each turn can share up to five files, with a combined limit of 256 MiB. A single file can use the full limit. PNG, JPEG, GIF and WebP can appear inline when they are at most 1 MiB, 4096 pixels per side and 8 megapixels. The app copies files into SQLite in chunks. The authenticated `/media/shared/` route streams downloads, supports single HTTP byte ranges, and does not load the complete file into memory. Allow space for both the stored copy and SQLite's WAL during ingestion.
- `create_session`, `chat`: create independent sessions and send deduplicated messages to an `@name` or session ID. An agent handling an incoming request can use `reply_chat` once to respond to its sender; it cannot start another `chat` request from that turn.
- `list_models`, `set_model`: model discovery and selection.
- `context`, `compact`: native context inspection and queued compaction.

These tools refer only to this application's database. A stable `request_id` deduplicates a submitted prompt or compaction request; reusing it with different content fails.

The host automatically approves individual native read requests. Other native permission requests wait for a decision in the UI. **Approve for this session** remembers matching requests in that chat until restart. Settings can disable prompts globally and save that choice across restarts. Copilot can run commands it classifies as safe without asking the host; a plain `printf` required no approval in the browser test.

An agent can use `set_session_description` to update its own short description. Descriptions appear in `list_sessions` and in the sidebar tooltip. Users can also edit them in **Rename session**. Use up to 280 characters; leave the field empty to clear it.

### Delegation

Use Copilot’s native subagents for work that does not need another session’s history. Point `MS_DURABLE_AGENTS` at trusted directories containing Copilot `.agent.md` definitions to add native specialists. The SDK manages their execution.

To reuse a named session’s history, open **Rename session** on the target and enable **Allow other agents to delegate tasks to this session**. In a user-started turn, `delegate_to_sessions` accepts `tasks: [{session_id, prompt}, …]` and a `request_id`. It runs up to four distinct idle targets concurrently and returns their final answers in input order to the caller’s current turn. No `reply_chat` is needed. Use a new request ID per batch; retry the same tasks with the same ID. Normal tool approvals still apply.

The default wait is 120 seconds (`timeout_seconds`: 1–600). Busy, disabled, failed and timed-out targets are reported individually. On timeout or cancellation, queued work is cancelled; work that already started can finish in its own timeline. Delegated turns cannot use app `chat`, `reply_chat` or `delegate_to_sessions`. Queued delegations are not replayed after a restart.

## Azure Foundry and Azure OpenAI

Use the SDK's bring-your-own-key (BYOK) connection with an API key or Entra credentials. [examples/providers.json](../examples/providers.json) maps a selection ID to an Azure deployment:

```sh
export MS_DURABLE_PROVIDERS_FILE=/secure/path/providers.json
export AZURE_OPENAI_API_KEY=... # supply through your secret manager
make run
```

The sample appears as `foundry/chat` in the model picker. `wire_model` is the deployment sent to Azure; `model_id` identifies the base model for Copilot behaviour and capabilities. Set token limits and capability overrides to match the deployed model. Model selection persists as the provider-qualified ID, not as credentials.

For native Azure routing:

- `type: "azure"`, with no `azure.api_version`, uses the versionless `/openai/v1/` route.
- With `azure.api_version`, the runtime uses the deployment-scoped Azure route and the `api-version` query parameter.
- `wire_api` selects native `completions` or `responses` transport. The synthetic routing tests cover **chat completions**; the live Azure Responses route has not been tested.

Use a model inference endpoint. Azure AI Projects (`/api/projects/...`) and Agent Service endpoints are unsupported. For an OpenAI-compatible Foundry endpoint, set `type: "openai"` and give its full API base URL. This route has not been live-tested.

Authentication options in a provider profile:

| Field | Behaviour |
| --- | --- |
| `api_key_env` | Read an API key from the server environment |
| `bearer_token_env` | Read a static bearer token from the server environment |
| `auth: "entra"` | Use async `DefaultAzureCredential` through the native per-request bearer-token callback |
| `scope` | Optional Entra scope; defaults to `https://cognitiveservices.azure.com/.default` |

Entra uses the normal Azure Identity credential chain, including configured service-principal, managed-identity or Azure CLI credentials. The callback obtains a token for each native request; Azure Identity handles token caching. Static bearer tokens do not refresh automatically. Configure environment-variable names, profiles and endpoints on the server.

## MCP and skills

The MCP file accepts `mcpServers` or `servers`. HTTP bearer credentials can reference a server environment variable:

```json
{
  "mcpServers": {
    "memento": {
      "url": "https://your-memento-server/mcp",
      "auth": "bearer",
      "bearerTokenEnv": "MEMENTO_TOKEN",
      "tools": ["*"]
    }
  }
}
```

The URL is illustrative. Use your existing Memento endpoint and credential. A missing bearer environment variable fails configuration. Native HTTP and stdio MCP definitions are forwarded to Copilot. For stdio, configure a command, arguments and any required child environment explicitly; JSON string values are not shell-expanded.

Set `MS_DURABLE_SKILLS` to directories containing `name/SKILL.md`. Skills and `AGENTS.md` are loaded by the native runtime. Broad configuration discovery and repository hooks are disabled; explicitly configured skills and MCP remain enabled.

## Persistence and failure handling

`data/app.sqlite` contains the session catalogue, input queue, projected events, approvals and native `/session-state` files. SQLite uses WAL and full synchronous writes. Native transcript writes and application turn transitions are separate transactions, even though they share a database.

On restart, running inputs become `interrupted`, pending approvals expire and queued inputs resume, except queued delegations. Native resume disables pending-work continuation and automatic transcript recovery. Interrupted inputs are **not** automatically resent. A process failure between a native side effect and a database update is ambiguous; persisted history does not provide exactly-once external effects.

Cancellation stops the local worker, cancels queued inputs and requests native abort where a session is attached. An in-flight session creation can finish registering a blank session, but its cancelled worker cannot send a prompt. A failed event projection stops native execution and quarantines the session until recovery. Do not resend an interrupted turn without checking its transcript and external effects.

Back up the state directory after a graceful shutdown, or use SQLite's online backup API. Do not copy only the live main database while omitting its WAL. Keep credentials, database backups and runtime files private; native transcripts and tool outputs can contain sensitive workspace content.

## Compaction and prompt-prefix caching

Automatic infinite-session compaction is enabled through the native SDK. Manual compaction is a durable queued operation using `session.rpc.history.compact`; it does not race an active user turn. The context dialog reports native measurements and attribution. Opening it before the first turn does not create a native session.

Copilot normally adds a changing checkpoint list to its system prompt after compaction. A targeted SDK section transform replaces only `<session_context>` with a stable pointer to `/session-state/checkpoints/index.md` and `/session-state/files/`. Other native prompt sections, guardrails, checkpoint files and conversation summaries are preserved.

Synthetic endpoint tests compare actual outbound system messages and tool definitions before and after ordinary turns, reopen and compaction. Unchanged turns retain the preceding message prefix; compaction deliberately replaces the conversation suffix. Provider/model changes, instructions, skills, MCP definitions, SDK versions or elapsed cache expiry can still invalidate caching. These tests do not prove an Azure cache hit or a billing reduction.

## Security boundaries

All authenticated browsers share the same sessions. Native shell tools run as the server user; neither the approval UI nor the native filesystem adapter is a sandbox.

The filesystem adapter limits ordinary requests to the workspace, configured skills and SQLite session state. It rejects configured credential and state paths and destructive operations on their ancestors. These checks do not protect against a hostile process racing symlinks.

The host redacts known provider, MCP and web credentials from its events, errors and application-tool results. The model catalogue excludes credentials. The native runtime receives a reduced environment and a private home directory. Use OS or container isolation as described in the [README](../README.md#state-and-safety); redaction cannot substitute for isolation.

## Container publishing

The [Publish container workflow](../.github/workflows/container.yml) builds and smoke-tests the image before pushing to GHCR. Push a `v*` version tag, or run the workflow manually on `main` with an optional existing version tag. A manual run without a tag publishes `sha-<commit>` using the first 12 characters of the commit ID.

## Verification

The [Fluent web design plan](fluent-web-plan.md) records the visual rules, implementation order and browser acceptance checks.

```sh
make check           # lint and offline tests
make test-native     # actual Copilot executable + local synthetic Azure server
make test-ui         # Chromium against a fixture API; no model calls
make test-browser    # Chromium + actual Copilot model; consumes model allowance
```

Both browser targets need Playwright Chromium at `PLAYWRIGHT_BROWSERS_PATH`. The fixture suite tests rendering, approvals, context and the mobile drawer in both themes and saves screenshots to the run directory. The live browser test needs a Copilot credential; use `MS_DURABLE_MODEL` to select its model. Native tests use no real credentials or remote models, but the SDK and runtime may need downloading.

Run `MS_DURABLE_TIMELINE_PROFILE=verify make test-ui` for the opt-in timeline profile. It streams 24 updates into one turn in a 400-turn fixture, measures Chromium scripting time and 12 viewport resizes, and checks that switching back renders cached history before the server responds. On Chromium 148.0.7778.96, the 24 updates used 1,124 ms of scripting at `4a8e45c` and 16–17 ms after limiting live renders to changed turns. Resize layout time was 45 ms before and 43–44 ms after.

Opt-in skill/Memento and restart probes use an isolated directory:

```sh
MS_DURABLE_PROBE_DIR="$WORKSPACE_TMP_BASE/ms-durable/runs/probe" \
  MS_DURABLE_MCP_FILE=/secure/path/mcp.json make live-probe
MS_DURABLE_PROBE_DIR="$WORKSPACE_TMP_BASE/ms-durable/runs/probe" \
  MS_DURABLE_MCP_FILE=/secure/path/mcp.json make live-resume
```

Set `WORKSPACE_TMP_BASE` before running the probe commands. The Makefile keeps test output and pip, Python bytecode, Ruff, Bun and Copilot extraction caches under the project’s `ms-durable/` temporary directory. Keep persistent state elsewhere. See [AGENTS.md](../AGENTS.md) for path defaults and fallbacks.
