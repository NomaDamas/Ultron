# Ultron

Ultron is a modular self-evolving harness ecology built around a preserved `hermes-agent` core and a generative UI triage app. It keeps upstream Hermes behind explicit seams while Ultron owns module registration, composition, evolution, evaluation, persistence, auth, observability, and rollback safety.

## Architecture

- `module/`: typed harness modules, blobs, surface contracts, privacy and fitness metadata.
- `registry/`: append-safe module registry, lifecycle state, active pointer CAS.
- `composition/`: resolver and module-set manifest construction.
- `run/`: signed `RunManifest` provenance, including actor attribution for caused transitions.
- `ledger/`: side-effect ledger, canary-scoped rollback, quarantine, actor audit.
- `evolution/`: variation, selection, active-set planning, atrophy/prune/restore controls.
- `feedback/`: consented feedback capture and aggregation.
- `evaluation/`: benchmark harness and benchmark-provenance-gated promotion evidence.
- `ui/` and `app/`: server-owned generative UI runtime plus FastAPI triage surface.
- `persistence/`: in-memory stores for the MVP path and SQLite durable stores/unit-of-work for restart-safe promotion/prune/restore.
- `auth/`: local principal, scoped privileges, expiring sessions, CSRF defense in depth.
- `obs/`: deterministic structured telemetry counters exposed at `/api/metrics`.
- `hermes/`: pinned Hermes metadata, adapter capability contract, static spike, and vendor integrity verification.

## Status

G001-G007 are implemented as the baseline modular harness, module registry/resolver, rollback/manifest safety, variation/selection/evaluation loops, feedback channel, generative UI runtime, and FastAPI triage MVP.

GAP1-GAP7 hardening is represented in tests and code: adapter contract and live-adapter fail-closed checks; blob-backed module integrity; durable SQLite persistence and signed run manifests; benchmark evidence gates; feedback/privacy controls; generative UI validation; platform auth/actor audit/observability/vendor-integrity/docs.

## Real vs seam

Real in this sandbox: typed module contracts, registry/resolver, signed run manifests, side-effect ledger, rollback quarantine, benchmark provenance gates, SQLite durable path, in-memory MVP path, scoped session auth, CSRF/pointer/policy/evidence defense in depth, actor audit, structured telemetry, and fail-closed vendor integrity verification when a vendor tree is present.

Seams in this sandbox: Hermes execution is represented by `DeterministicFakeHermesAdapter`, and module/UI generation use deterministic fake generators. There is no live Hermes process or live model provider in the sandbox. Live adapters/generators must not return fake/stub providers and fail closed when they do. The upstream Hermes core is preserved; topology orchestration, cron, gateway, and MCP surfaces remain deferred non-MVP integrations.

## Promotion and rollback model

Promotion requires benchmark-runner provenance, trajectory IDs, promotable evidence labels, selector approval, current pointer version, CSRF, authenticated session, and the required principal scope. Promotion, prune, restore, and rollback are ledgered; durable unit-of-work transitions record the actor subject and roll back atomically on failure.

## Run tests

```bash
python -m venv .venv
. .venv/bin/activate
pip install pyyaml 'pydantic>=2' pytest fastapi httpx uvicorn
.venv/bin/python -m pytest tests/ -q
```

## Run the server

```bash
.venv/bin/python -m ultron.app.server
```

Open `http://127.0.0.1:8717/`. `GET /` issues an expiring local session and CSRF token. `POST /api/action` accepts typed actions. `GET /api/metrics` returns declared telemetry counters only, with no secrets.

## Non-goals and MVP non-scope

Ultron does not mutate vendored Hermes source, global Hermes memory or skills, terminal backends, credentials, cron/gateway/MCP configuration, or upstream runtime internals. Multi-user identity providers, remote tenants, live model operation, production key management, distributed sessions, and topology orchestration are outside this MVP; the local default principal is a single-user development boundary, not an enterprise auth system.
