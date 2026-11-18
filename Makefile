SHELL := /bin/bash
# Snapshot inherited TMPDIR before exporting project-scoped paths.
INHERITED_TMPDIR := $(TMPDIR)
CI_ENABLED := $(filter-out 0 false,$(strip $(CI)))
GENERIC_TMP := $(or $(RUNNER_TEMP),$(INHERITED_TMPDIR),/tmp)
ifeq ($(strip $(WORKSPACE_TMP_BASE)),)
ifneq ($(CI_ENABLED),)
TMP_BASE := $(GENERIC_TMP)
else
TMP_BASE := $(shell test -d /workspace/tmp -a -w /workspace/tmp && echo /workspace/tmp || echo $(GENERIC_TMP))
endif
else
TMP_BASE := $(WORKSPACE_TMP_BASE)
endif
PROJECT_TMP := $(TMP_BASE)/ms-durable$(if $(strip $(MS_DURABLE_WORKTREE)),/worktrees/$(MS_DURABLE_WORKTREE))
export TMPDIR := $(PROJECT_TMP)/runs
export PIP_CACHE_DIR := $(PROJECT_TMP)/cache/pip
export PYTHONPYCACHEPREFIX := $(PROJECT_TMP)/cache/pycache
export RUFF_CACHE_DIR := $(PROJECT_TMP)/cache/ruff
export BUN_INSTALL_CACHE_DIR := $(PROJECT_TMP)/cache/bun
export COPILOT_CLI_EXTRACT_DIR := $(PROJECT_TMP)/cache/copilot
export PLAYWRIGHT_BROWSERS_PATH ?= $(shell test -d /workspace/bin/pw-browsers && echo /workspace/bin/pw-browsers || echo $(PROJECT_TMP)/cache/pw-browsers)
VENV ?= $(PROJECT_TMP)/venv
PY := $(VENV)/bin/python
.PHONY: install lint format test test-native test-ui test-browser live-probe live-resume check run clean lock
install:
	mkdir -p $(PROJECT_TMP)/{cache,build,tests,logs,runs}
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -r requirements.lock
	bun install --frozen-lockfile
lint:
	$(VENV)/bin/ruff check .
format:
	$(VENV)/bin/ruff format .
test:
	mkdir -p $(PROJECT_TMP)/tests $(TMPDIR)
	$(PY) -m pytest -q --basetemp=$(PROJECT_TMP)/tests/pytest-tmp -o cache_dir=$(PROJECT_TMP)/tests/pytest
test-native: export MS_DURABLE_NATIVE_TESTS=1
test-native: test
live-probe:
	$(PY) tests/live_probe.py
live-resume:
	$(PY) tests/live_resume.py
test-ui:
	MS_DURABLE_TEST_TMP=$(PROJECT_TMP) bun tests/ui.ts
test-browser:
	MS_DURABLE_TEST_PYTHON=$(PY) MS_DURABLE_TEST_TMP=$(PROJECT_TMP) bun tests/browser.ts
lock:
	$(VENV)/bin/pip freeze --exclude-editable > requirements.lock
check: lint test
run:
	$(PY) -m agent
clean:
	@echo 'Disposable outputs: $(PROJECT_TMP). Persistent state in ./data is never cleaned.'
