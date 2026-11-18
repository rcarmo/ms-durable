#!/usr/bin/env bash
set -euo pipefail
engine=$1 image=$2 source=$3
name=ms-durable-smoke-$$
cleanup() {
  status=$?
  if (( status != 0 )); then "$engine" logs "$name" >&2 || true; fi
  "$engine" rm -fv "$name" >/dev/null 2>&1 || true
}
trap cleanup EXIT
# BYOK starts the real SDK without a GitHub login. No external inference is used.
"$engine" run -d --name "$name" --network none \
  -e MS_DURABLE_TOKEN=container-smoke -e MS_DURABLE_PROVIDERS_FILE=/providers.json \
  -e AZURE_OPENAI_API_KEY=synthetic-smoke-only \
  -v "$source/examples/providers.json:/providers.json:ro" "$image" >/dev/null
"$engine" exec -i "$name" python - <<'PY'
import json
import os
import time
from urllib.request import Request, urlopen

assert os.getuid() != 0
base = 'http://127.0.0.1:8015'
headers = {'Authorization': 'Bearer container-smoke', 'Content-Type': 'application/json'}
def get(path, data=None):
    with urlopen(Request(base + path, data=data, headers=headers), timeout=2) as response:
        return response.read()

for _ in range(60):
    try:
        assert json.loads(get('/api/health')) == {'status': 'ok'}
        break
    except OSError:
        time.sleep(0.5)
else:
    raise SystemExit('Container did not become ready')
assert b'MarkdownIt' in get('/vendor/markdown-it.min.js') or b'markdownit' in get('/vendor/markdown-it.min.js')
assert b'MS Durable' in get('/') or b'<!doctype html>' in get('/').lower()
config = json.loads(get('/api/config'))
model = config['models'][0]['id']
session = json.loads(get('/api/sessions', json.dumps({'title': 'Smoke', 'model': model}).encode()))
assert json.loads(get('/api/sessions/' + session['id']))['session']['title'] == 'Smoke'
print('Offline container smoke passed: native runtime startup, browser assets and SQLite API')
PY
