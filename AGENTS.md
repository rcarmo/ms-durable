# ms-durable

Python 3.11+, asyncio, FastAPI, aiosqlite; Microsoft Copilot SDK 2.0.1.

Copilot owns inference, agent execution, model switching, transcript recovery, coding tools, AGENTS.md discovery, skills and MCP. Do not reimplement these. Host code owns HTTP authentication, a durable input queue, session catalogue, permission UI, event projection and the SQLite sessionFs adapter. All SDK operations use the public Python APIs.

Use `make install`, `make check`, `make run`. Dependencies are pinned in requirements.lock. No JavaScript build is required. Read README.md for safety/recovery limits before changing the lifecycle. VERIFICATION.md records exact test scope and untested paths.

## Disposable files

Makefile chooses explicit WORKSPACE_TMP_BASE first. In CI (excluding empty, 0 and false), use RUNNER_TEMP, inherited TMPDIR or /tmp; locally prefer writable /workspace/tmp, otherwise the generic fallback. Append ms-durable in every case. Snapshot inherited TMPDIR before exporting. Subdirectories: cache/pip, cache/pycache, cache/ruff, cache/bun, cache/copilot, build, tests, logs, runs, venv. Make exports the matching pip, Python, Ruff, Bun and native-runtime cache variables and sets pytest's base directory. Playwright uses installed shared browsers at /workspace/bin/pw-browsers (override PLAYWRIGHT_BROWSERS_PATH elsewhere). Linked worktrees set MS_DURABLE_WORKTREE=<slug>, using ms-durable/worktrees/<slug> under the same temporary base. Do not clean unrelated paths or a live virtual environment.

Persistent SQLite databases, credentials and config live in ./data or MS_DURABLE_DATA, outside disposable paths. Never commit them. Existing Pi authentication files are read-only; never write or refresh them in place. Never expose tokens to HTML, logs, model prompts or event exports. Tests use synthetic credentials and isolated data.

## Agent authority

Introspection tools operate on this application's sessions only. Session IDs are not authentication. Read shared sessions only after the user grants access to this application. Native tools run as the server user, not in a filesystem sandbox. Permission approval is not process isolation. Never restart this server from one of its own active tool calls.
