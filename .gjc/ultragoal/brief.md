Build Ultron: a modular self-evolving harness ecology on a PRESERVED upstream NousResearch/hermes-agent core, surfaced via a generative web UI. Stack Python 3.11+, work in the ultron package (now rooted at /Ultron), git-tracked. Preserve the Hermes core; modules attach only through adapter-proven surfaces. (Durable ledger restored after a sandbox environment reset; deliverables verified present with 194 tests passing.)

@goal: Bootstrap repo and Milestone 0 adapter contract
Repo scaffold + adapter_capability_contract.yaml (hermes pin ee1a744) + ModuleSurfaceContract preserved-core prohibitions + static compatibility spike.

@goal: HarnessModule contract and model
ultron.module.contract + ultron.module.model (HarnessModule content-addressed identity excluding runtime fitness).

@goal: Module registry and composition resolver
Immutable content-addressed ModuleRegistry + deterministic CompositionResolver + atomic ActivePointerStore (CAS).

@goal: RunManifest, side-effect ledger and no-poisoning rollback
Signed RunManifest + append-only SideEffectLedger + RollbackController with provable no-poisoning + fail-closed pointer revert.

@goal: Evolution loop with selection and reversible atrophy
Bounded one-primitive variation (human-approval gate), evidence-gated selection, reversible atrophy with diversity floor + critical-seed approval, stability controls.

@goal: Feedback channel and evaluation harness
Frozen typed privacy envelope (model events cannot verify), paired-benchmark evaluation harness single-sourced to the Selector.

@goal: Generative UI runtime and triage MVP end to end
Server-owned generative UI runtime (typed ActionCommand, privileged gating: authz+CSRF+pointer-version+policy+evidence, CSP/no-inline) + triage MVP wiring the full variation->selection->retention->atrophy loop with rollback+no-poisoning, FastAPI server.
