# Ultron

**An agent that creates the harness that creates itself in various environments and adapts.**

Ultron is a modular, self-evolving **harness ecology** built around a *preserved* [`hermes-agent`](https://github.com/NousResearch/hermes-agent) core. You just chat. The agent builds the tools you need, renders them as **generative UI inside the chat**, and the more you use it the more your harness is **whittled to your workflow** — automatically, safely, and reversibly.

---

## Philosophy

- **Preserve the core.** Upstream Hermes stays untouched behind explicit adapter seams. Ultron never mutates Hermes source, memory, skills, tools, or config — it only *attaches* to it.
- **Modules are harnesses.** A centralized agent creates modules (prompt packs, tool policies, UI panels, safety/budget) like tool-calls. Those modules *are* your harness.
- **Use shapes the harness.** Your usage, request history, and feedback become the selection pressure. Modules that help survive; modules that don't atrophy. Like a stone shaped by a river.
- **Generative UI is the surface.** Tools and results appear as typed, server-validated UI components rendered *inline in the chat* — you see your work happen, you don't operate a dashboard.
- **Safety is non-negotiable.** Self-modification is bounded (one change at a time), evidence-gated, reversible (no-poisoning rollback), audited, and privacy-redacted by construction.

## What you experience

- **A command surface (`/`).** A command bar drives a **bounded generative-UI canvas** (not a scrolling transcript): ask for what you want and the agent builds/tunes a tool, streaming the result back as **generative-UI cards** (plan, risk, tests, tool result, evidence, safety). Toggle **Replace (A)** vs **Accumulate (B)** to transform the canvas or keep a capped workspace; pin cards you want to keep. Attach an **image** to drive the multimodal (VLM) path. 👍/👎 feedback quietly shapes your harness over time.
- **A settings dashboard (`/dashboard`), separate.** Only for observing/operating: the evolution ecology (modules by lifecycle, lineage, fitness), runs & evidence, the audit ledger, safety state, metrics, and redacted personalization signal. The user's chat is never cluttered with this.
- **JARVIS / Iron-Man HUD aesthetic.** Deep space blue (`#0a0e1a`), cyan glow (`#00d4ff`), holographic glass panels, reticle/scanline accents, an animated Ultron orb — smooth, CSP-safe animations with `prefers-reduced-motion` support.

---

## Quickstart (first-time user)

> The server binds to your own machine's `localhost`. Run it locally; it is not a hosted service.

```bash
git clone https://github.com/NomaDamas/Ultron.git
cd Ultron            # if it cloned into a nested folder, cd into the one containing run.sh
./run.sh             # creates .venv, installs deps, starts the server
```

Then:

1. Open **http://localhost:8799** — the chat console.
2. Type a real request, e.g. *"build a tool to triage flaky tests in my repo and propose fixes."*
3. Watch the agent build a tool and render the result as **inline generative-UI cards** (run summary, plan/risk/tests, harness-evolution, evidence, safety).
4. Give 👍/👎 feedback — this is the signal that personalizes your harness over time.
5. Open **http://localhost:8799/dashboard** to watch the evolution ecology, runs, ledger, safety, and metrics.

Notes:
- **No API key needed for the default (demo) mode** — it runs deterministically and fully. Live model/Hermes is opt-in (see *Going live*); without keys/deps it fails closed and never fakes results.
- macOS/zsh: if `python` is "command not found", that's expected — `run.sh` uses `python3` + the venv binary directly. (`PYTHON=python3.11 ./run.sh` to pin.)
- After restart, hard-refresh the browser (Cmd+Shift+R); the server log should show `GET /static/chat.css 200`.

Verify it works:
```bash
.venv/bin/python -m pytest -q        # expect: 389 passed, 3 skipped
```

---

## How it works (the loop)

```
request → resolver composes your active module-set → signed RunManifest
        → Hermes adapter runs the work (fake by default; real when configured)
        → result validated + redacted into an InlineGenUiEnvelope → rendered inline in chat
        → your feedback + usage → non-raw PersonalizationSummary
        → evolution loop: ONE bounded variation → benchmark selection
          → promote (only on real benchmark evidence) / rollback (no-poisoning) / reversible atrophy
```

## Architecture

- `hermes/`: pinned Hermes metadata, adapter capability contract, static spike, vendor integrity; real + deterministic-fake adapters (fail-closed live seam).
- `module/`: typed harness modules, content-addressed blobs, surface contracts, privacy + fitness metadata.
- `registry/` + `composition/`: immutable module registry, lifecycle, atomic active-pointer CAS, deterministic resolver + module-set manifest.
- `run/` + `ledger/`: signed `RunManifest` provenance (actor-attributed), side-effect ledger, canary rollback, quarantine, audit.
- `evolution/`: variation, selection, active-set planning, atrophy/prune/restore controls.
- `feedback/` + `evaluation/`: consented feedback aggregation; benchmark harness + benchmark-provenance-gated promotion.
- `persistence/`: in-memory MVP path + SQLite durable stores/unit-of-work (restart-safe, atomic, fail-closed signer).
- `ui/` + `app/`: server-owned generative UI runtime (typed components + bounded animation), chat console, separate dashboard, read-only no-secret endpoints.
- `auth/` + `obs/`: scoped session principal, CSRF, actor audit; deterministic telemetry at `/api/metrics`.

## Safety model

- **Server-validated generative UI** — typed component registry + discriminated prop schemas; privileged/model-defined actions and unknown components rejected; strict CSP, no inline scripts/styles/eval.
- **Evidence-gated promotion** — a module is promoted only with real benchmark-runner provenance (trajectory IDs, promotable evidence label, selector approval), current pointer version, session + CSRF + scope.
- **No-poisoning rollback** — canaries run isolated; rollback is reversible and provably cannot leak into later runs.
- **Privacy by construction** — raw request text, feedback comments, and secrets are redacted everywhere (responses, errors, read-only endpoints, ids); personalization uses non-raw summaries only.
- **Audit** — every pointer/lifecycle/quarantine mutation records an actor.
- **One change at a time** — variation is a single bounded primitive; permission expansion requires human approval.

## Real vs seam (in this sandbox)

Real: typed module contracts, registry/resolver, signed manifests, ledger, rollback quarantine, benchmark provenance gates, SQLite durable path, scoped auth/CSRF/actor audit, telemetry, vendor integrity, redaction.

Seam: Hermes execution uses `DeterministicFakeHermesAdapter`; UI/module generation use deterministic fakes. No live Hermes process or model in the sandbox. Live adapters/generators fail closed and must not return fake/stub providers.

## Configuration & going live (real Hermes + LLM + VLM)

Ultron is configured **without code edits** — via `.env`, the committed `ultron config` CLI, or the write-only **Model settings** panel on `/dashboard`. Secrets are stored server-side in an out-of-repo store (`${ULTRON_CONFIG_DIR:-~/.config/ultron}/secrets.json`, owner-only); reads only ever expose status + a redacted `SecretRef` (fingerprint/last4/source). Precedence: **runtime secret store > process env > `.env`**.

Copy `.env.example` → `.env`, or use the CLI:

```bash
python -m ultron.config set llm.api_key --stdin     # secret, no echo (also: ultron config ...)
python -m ultron.config set llm.model gpt-4o-mini    # non-secret value
python -m ultron.config set vlm.api_key --stdin      # vision/multimodal key
python -m ultron.config status                       # redacted view, never prints secrets
```

To run live (LLM **and** VLM are both attachable; OpenAI-compatible):

```bash
pip install hermes-agent
export ULTRON_ADAPTER=pinned-hermes
export ULTRON_UI_GENERATOR=model        # model-driven generative UI
export ULTRON_MODULE_SYNTH=model
export ULTRON_VLM=model                  # vision/multimodal path
export ULTRON_LLM_BASE_URL=https://api.openai.com/v1   # any OpenAI-compatible endpoint
export ULTRON_LLM_API_KEY=...
export ULTRON_LLM_MODEL=gpt-4o-mini
export ULTRON_VLM_BASE_URL=https://api.openai.com/v1
export ULTRON_VLM_API_KEY=...
export ULTRON_VLM_MODEL=gpt-4o
./run.sh
```

The pinned Hermes path lazily imports `hermes-agent`, runs under an isolated HOME/workspace per request, and never writes global Hermes state. Missing deps/keys fail closed with live-unavailable errors — Ultron never substitutes stub/fake results for a selected live path. Image input is bounded (1 image, 4 MiB, PNG/JPEG/WebP, ≤4096px/16MP, EXIF stripped); raw bytes never leave request scope and the VLM observation is redacted context only — final UI still passes server validation.

## Status & roadmap

- Baseline (G001-G007) + hardening (GAP1-GAP7, plus GAP8 live wiring) + the chat-only generative-UI iteration (Stories 1-6) + the model-driven multimodal iteration (LLM+VLM provider layer, `.env`/secret-store/settings/CLI config, model-driven UiSpec/synthesis, bounded image input, command-bar A/B canvas, hardening) are implemented and gated. **461 tests pass / 3 skipped** (skips need live creds).
- Remaining future work is tracked in [GitHub Issues](https://github.com/NomaDamas/Ultron/issues): live Hermes/model validation, topology/subagent orchestration, multi-tenant/team, ops connectors, durable raw personalization, voice/orb, expanded GenUI registry, explicit pinning, first live benchmark fixture.

## Non-goals / MVP non-scope

Ultron does not mutate vendored Hermes source, global memory/skills, terminal backends, credentials, or cron/gateway/MCP config. Multi-user identity, remote tenants, production key management, distributed sessions, and topology orchestration are out of this MVP; the local default principal is a single-user dev boundary, not enterprise auth.
