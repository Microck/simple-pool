# simple-pool

sticky failover router for OpenAI-compatible chat-completions backends. tries providers in config order, sticks with the first healthy one, and skips failed backends for a cooldown period.

---

## features

- deterministic ordered fallback -- backends tried from current preferred index
- real-response health -- no synthetic pings; 2xx = healthy, 429/403/408/5xx/timeout = unhealthy
- configurable cooldown -- failed backends skipped for a period, persisted to disk across restarts
- sticky preference -- successful backend remembered, used first next time
- env var expansion -- secrets via `${VAR}`, never in config files
- optional bearer auth on the local endpoint
- exposes `/v1/models` and `/v1/chat/completions`
- 503 when all backends are unavailable

---

## architecture

```
client request
      |
      v
+-------------+     ordered      +-----------+
| simple-pool | ---------------> | backend 1 | -- 2xx --> respond + stick
+-------------+                  +-----------+
      |                               |
      | (fail/timeout/429)            v cooldown
      |                          +-----------+
      +------------------------> | backend 2 | -- 2xx --> respond + stick
                                 +-----------+
                                      |
                                      v (all fail)
                                 503 aggregate unavailable
```

---

## install

```bash
pip install .
pip install ".[yaml]"       # yaml config support
pip install ".[dev]"        # pytest + ruff
```

---

## configuration

YAML or JSON. secrets use `${ENV_VAR}` expansion:

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

---

## usage

```bash
export PRIMARY_API_KEY="sk-..."
export FALLBACK_API_KEY="sk-..."
export SIMPLE_POOL_AUTH_TOKEN="my-local-secret"

simple-pool examples/config.yaml --log-level DEBUG
```

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer my-local-secret" \
  -H "Content-Type: application/json" \
  -d '{"model":"my-model","messages":[{"role":"user","content":"hello"}]}'
```

---

## behavior

| event | effect |
|-------|--------|
| backend returns 2xx | mark preferred, clear cooldown |
| backend returns 429 (quota/limit) | cooldown, try next |
| backend returns 403, 408, 5xx | cooldown, try next |
| timeout or connection refused | cooldown, try next |
| all backends on cooldown | 503 with reason |
| non-failure error (e.g. 400) | pass through as-is, no cooldown |

state persisted to `state_path` (default `.simple-pool-state.json`).

---

## security

- binds `127.0.0.1` by default
- secrets expanded from env at startup, never written to disk
- state file contains only backend names and cooldown timestamps
- upstream API keys never included in client error responses

---

## development

```bash
pip install ".[dev,yaml]"
pytest -v
ruff check src/ tests/
ruff format --check src/ tests/
```

---

## license

MIT

The test suite includes real loopback HTTP journeys through the pool and two synthetic upstreams: failover, sticky routing, model overrides, response integrity, invalid JSON shapes, and negative request lengths. It never calls a model provider or uses real credentials. State regressions verify malformed-file recovery, retry after a failed atomic save, and avoiding unchanged disk writes on repeated successful requests. Browser automation is not used for this HTTP-only service.
