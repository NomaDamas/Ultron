# Ultron

Ultron is a modular self-evolving harness ecology that preserves the upstream `hermes-agent` core and attaches workflow modules through a proven adapter capability contract.

Milestone 0 defines the Hermes pin, adapter capability contract, module surface contract, and static compatibility spike used to prove which upstream attach surfaces are safe for Ultron modules.

## Preserved-core rule

Ultron must not mutate vendored Hermes source, global Hermes memory or skills, Hermes tool implementations, terminal backends, cron/gateway/MCP configuration, credentials, or upstream runtime internals. Ultron references and adapts Hermes through declared seams only.

## Run tests

```bash
python -m venv .venv
. .venv/bin/activate
pip install pyyaml 'pydantic>=2' pytest
python -m pytest tests/ -q
```
