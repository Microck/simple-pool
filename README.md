# simple-pool

Sticky failover router for OpenAI-compatible chat-completions backends. Routes requests through an ordered list of providers, measures availability from real inference responses, and sticks with the first healthy backend until it fails.

## features

- **deterministic ordered fallback** -- backends are tried in config order from the current preferred index
- **real-response health measurement** -- no synthetic health checks; availability is determined by actual request outcomes (2xx = healthy, 429 quota/403/408/5xx/timeout = unhealthy)
- **configurable cooldown** -- failed backends are skipped for a cooldown period, persisted to disk so restarts remember recent failures
- **sticky preference** -- on success, the router remembers which backend worked and starts there next time
- **environment variable expansion** -- secrets live in env vars (`${VAR}`), never in config files
- **optional auth** -- protect the local endpoint with a bearer token
- **OpenAI-compatible** -- exposes `/v1/models` and `/v1/chat/completions`
- **503 on aggregate failure** -- explicit error when all backends are unavailable

## architecture

```
client request
      |
      v
+-------------+     ordered      +-----------+
| simple-pool | ───────────────> | backend 1 | ── 2xx ──> respond + stick
+-------------+                  +-----------+
      |                               |
      | (fail/timeout/429)            v cooldown
      |                          +-----------+
      +────────────────────────> | backend 2 | ── 2xx ──> respond + stick
                                 +-----------+
                                      |
                                      v (all fail)
                                 503 aggregate unavailable
```

## installation

```bash
pip install .

# with yaml config support
pip install ".[yaml]"

# dev dependencies (pytest + ruff)
pip install ".[dev]"
```

## configuration

Create a config file (YAML or JSON). Secrets use `${ENV_VAR}` expansion:

```yaml
host: "127.0.0.1"
port: 8000
model: "my-model"
request_timeout: 120
cooldown: 1200
auth_token: "${SIMPLE_POOL_AUTH_TOKEN}"
state_path: ".simple-pool-state.json"

failure_codes: [403, 408, 429, 500, 502, 503, 504]

backends:
  - name: "primary"
    base_url: "https://api.primary.example.com/v1"
    api_key: "${PRIMARY_API_KEY}"
    model: "gpt-4o"

  - name: "fallback"
    base_url: "https://api.fallback.example.com/v1"
    api_key: "${FALLBACK_API_KEY}"
    model: "gpt-4o"
```

See `examples/config.yaml` for a full annotated example.

## usage

```bash
# set secrets
export PRIMARY_API_KEY="sk-..."
export FALLBACK_API_KEY="sk-..."
export SIMPLE_POOL_AUTH_TOKEN="my-local-secret"

# run
python -m simple_pool examples/config.yaml

# or via entry point
simple-pool examples/config.yaml --log-level DEBUG
```

Then point your client at the pool:

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer my-local-secret" \
  -H "Content-Type: application/json" \
  -d '{"model":"my-model","messages":[{"role":"user","content":"hello"}]}'
```

## operational behavior

| event | effect |
|-------|--------|
| backend returns 2xx | mark as preferred, clear cooldown |
| backend returns 429 with quota/limit body | cooldown, try next |
| backend returns 403, 408, 5xx | cooldown, try next |
| backend times out or connection refused | cooldown, try next |
| all backends on cooldown | return 503 with reason |
| non-failure error (e.g. 400 from upstream) | return as-is, no cooldown |

State is persisted to `state_path` (default `.simple-pool-state.json`) so cooldowns survive restarts.

## systemd

See `examples/simple-pool.service`. Place secrets in `/etc/simple-pool/env`:

```
PRIMARY_API_KEY=sk-...
FALLBACK_API_KEY=sk-...
SIMPLE_POOL_AUTH_TOKEN=local-secret
```

## security notes

- bind to `127.0.0.1` by default -- only expose on a network interface if you have upstream auth or a reverse proxy
- secrets are expanded from environment variables at startup, never written to disk
- the state file contains only backend names and cooldown timestamps, no credentials
- `auth_token` is optional but recommended; without it the pool accepts unauthenticated requests
- upstream API keys are never included in error responses to clients

## development

```bash
pip install ".[dev,yaml]"
pytest -v
ruff check src/ tests/
ruff format --check src/ tests/
```

## license

MIT
