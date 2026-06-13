
import os

import pytest

from ultron.app.triage import build_durable_triage_app, build_durable_triage_app_for_tests
from ultron.evaluation.harness import PairedTask
from ultron.evolution.variation import VariationPrimitive
from ultron.ledger.side_effect_ledger import LedgerEntry, SideEffectKind
from ultron.module.blobs import BlobKind, PromptPack, ToolPolicyBlob
from ultron.persistence.db import Database
from ultron.persistence.sqlite_stores import SqliteActivePointerStore, SqliteBlobStore, SqliteModuleRegistry, SqliteSideEffectLedger
from ultron.persistence.unit_of_work import PromotionUnitOfWork
from ultron.registry.store import ModuleLifecycle
from ultron.run.manifest import RunManifest
from ultron.run.signer import EnvKeyProvider, FixtureKeyProvider, ManifestSigner
from ultron.module.model import PersistencePolicy
from ultron.composition.manifest import ModuleSetManifest


class FailingLedger(SqliteSideEffectLedger):
    def _append_in_tx(self, cur, entry):
        super()._append_in_tx(cur, entry)
        raise RuntimeError('injected ledger failure')


def _snapshot(app, module_hash):
    return (app.registry.get(module_hash).lifecycle, app.pointer_store.get(app.pointer_key), len(app.ledger.promotable_entries()))


def _assert_snapshot(app, module_hash, snapshot):
    assert app.registry.get(module_hash).lifecycle is snapshot[0]
    assert app.pointer_store.get(app.pointer_key) == snapshot[1]
    assert len(app.ledger.promotable_entries()) == snapshot[2]


def _tasks(n=12):
    return [PairedTask(task_id=f't{i}', baseline_metric=1.0, candidate_metric=1.2) for i in range(n)]


def test_restart_durability_survives_full_triage_flow(tmp_path):
    db_path = tmp_path / 'triage.sqlite'
    app = build_durable_triage_app_for_tests(str(db_path))
    baseline = app.seed_baseline()
    app.start_run('default-user', 'code-triage', 'request')
    canary = app.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {'prompt_pack_hash': 'candidate-good-durable'})
    candidate_hash = canary['candidate'].content_hash
    decision = app.evaluate_and_decide(candidate_hash, _tasks(), canary['canary_id'])
    version = app.current_pointer_version()
    app.approve_promotion(candidate_hash, version)
    promoted_version, promoted_hashes = app.pointer_store.get(app.pointer_key)
    app2 = build_durable_triage_app_for_tests(str(db_path))
    assert app2.pointer_store.get(app2.pointer_key) == (promoted_version, promoted_hashes)
    assert app2.registry.get(baseline.content_hash).module.content_hash == baseline.content_hash
    assert app2.registry.get(candidate_hash).lifecycle is ModuleLifecycle.SURVIVOR
    assert app2.blob_store.get(BlobKind.PROMPT_PACK, baseline.prompt_pack_hash).content_hash() == baseline.prompt_pack_hash
    assert app2.evaluated_candidates.get(candidate_hash)['report'].candidate_hash == candidate_hash
    assert app2.ledger.entries_for_run(decision['report'].frozen_versions_hash)
    feedback = app.submit_feedback(decision['report'].frozen_versions_hash, rating=5, comment='durable')
    app3 = build_durable_triage_app_for_tests(str(db_path))
    assert app3.feedback_channel.events_for_candidate(candidate_hash)[0].event_id == feedback.event_id



def test_sqlite_pointer_cas_concurrency(tmp_path):
    path = tmp_path / 'cas.sqlite'
    a = SqliteActivePointerStore(Database(path))
    b = SqliteActivePointerStore(Database(path))
    assert a.swap(('u', 'wf'), 0, ['h1']) == 1
    with pytest.raises(ValueError, match='stale'):
        b.swap(('u', 'wf'), 0, ['h2'])
    assert b.swap(('u', 'wf'), 1, ['h2']) == 2


def test_file_database_uses_wal_and_rejects_future_schema(tmp_path):
    path = tmp_path / 'wal.sqlite'
    db = Database(path)
    assert db.conn.execute('PRAGMA journal_mode').fetchone()[0].lower() == 'wal'
    assert db.conn.execute('SELECT version FROM schema_meta WHERE id = 1').fetchone()[0] == 1
    db.conn.execute('UPDATE schema_meta SET version = 999 WHERE id = 1')
    db.conn.close()
    with pytest.raises(RuntimeError, match='newer than supported'):
        Database(path)


def test_atomic_promotion_rollback_on_stale_cas(tmp_path):
    app = build_durable_triage_app_for_tests(str(tmp_path / 'atomic.sqlite'))
    app.seed_baseline()
    canary = app.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {'prompt_pack_hash': 'candidate-atomic'})
    h = canary['candidate'].content_hash
    before_lifecycle = app.registry.get(h).lifecycle
    before_pointer = app.pointer_store.get(app.pointer_key)
    before_ledger = len(app.ledger.promotable_entries())
    uow = PromotionUnitOfWork(app.db, app.registry, app.pointer_store, app.ledger)
    with pytest.raises(ValueError, match='stale'):
        uow.promote(h, 999, before_pointer[1] + [h], 'evidence-stale', 'tester')
    assert app.registry.get(h).lifecycle is before_lifecycle
    assert app.pointer_store.get(app.pointer_key) == before_pointer
    assert len(app.ledger.promotable_entries()) == before_ledger
    new_version = uow.promote(h, before_pointer[0], before_pointer[1] + [h], 'evidence-ok', 'tester')
    assert new_version == before_pointer[0] + 1
    assert app.registry.get(h).lifecycle is ModuleLifecycle.SURVIVOR
    assert len(app.ledger.entries_for_run('evidence-ok')) == 1


def test_durable_builder_requires_explicit_env_or_signer(tmp_path, monkeypatch):
    monkeypatch.delenv('ULTRON_RUN_MANIFEST_SIGNING_SECRET', raising=False)
    with pytest.raises(RuntimeError, match='missing run manifest signing secret'):
        build_durable_triage_app(str(tmp_path / 'prod.sqlite'))
    monkeypatch.setenv('ULTRON_RUN_MANIFEST_SIGNING_SECRET', 'prod-secret')
    app = build_durable_triage_app(str(tmp_path / 'prod.sqlite'), key_id='prod-key')
    run = app.start_run('default-user', 'code-triage', 'request')
    assert run['run_manifest'].key_id == 'prod-key'
    assert run['run_manifest'].verify(signer=app.manifest_signer) is True


def test_atomic_promotion_prune_restore_rollback_on_ledger_failure(tmp_path):
    app = build_durable_triage_app_for_tests(str(tmp_path / 'midfail.sqlite'))
    app.seed_baseline()
    canary = app.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {'prompt_pack_hash': 'candidate-midfail'})
    h = canary['candidate'].content_hash
    before = _snapshot(app, h)
    failing = FailingLedger(app.db)
    uow = PromotionUnitOfWork(app.db, app.registry, app.pointer_store, failing)
    with pytest.raises(RuntimeError, match='injected ledger failure'):
        uow.promote(h, before[1][0], before[1][1] + [h], 'evidence-promote-fail', 'tester')
    _assert_snapshot(app, h, before)

    good = PromotionUnitOfWork(app.db, app.registry, app.pointer_store, app.ledger)
    good.promote(h, before[1][0], before[1][1] + [h], 'evidence-promote-ok', 'tester')
    promoted = _snapshot(app, h)
    failing = FailingLedger(app.db)
    uow = PromotionUnitOfWork(app.db, app.registry, app.pointer_store, failing)
    with pytest.raises(RuntimeError, match='injected ledger failure'):
        uow.prune(h, promoted[1][0], [x for x in promoted[1][1] if x != h], 'evidence-prune-fail', 'tester')
    _assert_snapshot(app, h, promoted)

    good.prune(h, promoted[1][0], [x for x in promoted[1][1] if x != h], 'evidence-prune-ok', 'tester')
    pruned = _snapshot(app, h)
    failing = FailingLedger(app.db)
    uow = PromotionUnitOfWork(app.db, app.registry, app.pointer_store, failing)
    with pytest.raises(RuntimeError, match='injected ledger failure'):
        uow.restore(h, pruned[1][0], pruned[1][1] + [h], 'evidence-restore-fail', 'tester')
    _assert_snapshot(app, h, pruned)


def test_durable_atrophy_restore_uses_uow_and_ledgers_both_transitions(tmp_path):
    app = build_durable_triage_app_for_tests(str(tmp_path / 'atrophy.sqlite'))
    app.seed_baseline()
    canary = app.propose_and_canary(VariationPrimitive.PROMPT_SLOT_EDIT, {'prompt_pack_hash': 'candidate-atrophy'})
    h = canary['candidate'].content_hash
    app.evaluate_and_decide(h, _tasks(), canary['canary_id'])
    app.approve_promotion(h, app.current_pointer_version())
    before_count = len(app.ledger.promotable_entries())
    app.atrophy_and_restore(h)
    entries = app.ledger.promotable_entries()[before_count:]
    assert [entry.payload['action'] for entry in entries] == ['prune', 'restore']
    assert all(entry.kind is SideEffectKind.POINTER_TRANSITION for entry in entries)
    assert app.registry.get(h).lifecycle is ModuleLifecycle.SURVIVOR
    assert h in app.pointer_store.get(app.pointer_key)[1]


def test_append_only_quarantine_survives_restart(tmp_path):
    path = tmp_path / 'ledger.sqlite'
    ledger = SqliteSideEffectLedger(Database(path))
    e1 = LedgerEntry(run_id='r', module_set_hash='s', canary_id='c', kind=SideEffectKind.ADAPTER_STATE)
    e2 = LedgerEntry(run_id='r', module_set_hash='s', canary_id='c', kind=SideEffectKind.TELEMETRY)
    ledger.append(e1); ledger.append(e2)
    assert ledger.mark_quarantined('c') == [e1.entry_id, e2.entry_id]
    assert [row[0] for row in Database(path).conn.execute('SELECT quarantined FROM ledger WHERE canary_id = ?', ('c',)).fetchall()] == [0, 0]
    assert Database(path).conn.execute('SELECT COUNT(*) FROM ledger_quarantine_events WHERE canary_id = ?', ('c',)).fetchone()[0] == 1
    ledger2 = SqliteSideEffectLedger(Database(path))
    entries = ledger2.entries_for_canary('c')
    assert [e.entry_id for e in entries] == [e1.entry_id, e2.entry_id]
    assert all(e.quarantined for e in entries)


def test_immutability_and_blob_type_enforcement(tmp_path):
    db = Database(tmp_path / 'immut.sqlite')
    blobs = SqliteBlobStore(db)
    registry = SqliteModuleRegistry(db, blobs)
    h = blobs.put(BlobKind.PROMPT_PACK, PromptPack(slots={'a': 'b'}))
    assert blobs.put(BlobKind.PROMPT_PACK, PromptPack(slots={'a': 'b'})) == h
    assert blobs.get(BlobKind.PROMPT_PACK, h) is not blobs.get(BlobKind.PROMPT_PACK, h)
    with pytest.raises(TypeError):
        blobs.put(BlobKind.PROMPT_PACK, ToolPolicyBlob(tools=['x']))
    app = build_durable_triage_app_for_tests(str(tmp_path / 'reg.sqlite'))
    module = app.seed_baseline()
    assert app.registry.register(module, ModuleLifecycle.SURVIVOR, 'user').module.content_hash == module.content_hash
    bad = module.model_copy(update={'name': 'collision', 'content_hash': module.content_hash})
    with pytest.raises(ValueError):
        app.registry.register(bad, ModuleLifecycle.SURVIVOR, 'user')


def _run_manifest():
    m = ModuleSetManifest(user_scope='u', workflow_fingerprint='wf', request_class='r', ordered_module_hashes=['h'], resolved_prompt_order=['p'], resolved_tool_allowlist=['read'], resolved_ui_panels=[], disabled_modules=[], conflicts=[], safety_policy={}, budget_policy={}, rationale='r').finalized()
    return RunManifest.from_manifest_set(m, run_id='r', session_id='s', active_module_set_id='a', hermes_version='h', adapter_version='a', contract_version='c', model_snapshot={'provider':'p'}, side_effect_ledger_id='l', created_at=1.0, timestamp_source='server', persistence_mode=PersistencePolicy.ISOLATED)


def test_manifest_signer_fail_closed_and_roundtrip(monkeypatch):
    monkeypatch.delenv('ULTRON_RUN_MANIFEST_SIGNING_SECRET', raising=False)
    with pytest.raises(RuntimeError):
        ManifestSigner.from_provider('prod', EnvKeyProvider())
    signer = ManifestSigner.from_provider('fixture', FixtureKeyProvider({'fixture': 'secret'}))
    payload = {'b': 2, 'a': 1}
    sig = signer.sign(payload)
    assert signer.verify(payload, sig, 'fixture') is True
    assert signer.verify({'a': 1, 'b': 3}, sig, 'fixture') is False
    assert signer.verify(payload, sig, 'wrong') is False
    signed = _run_manifest().sign(signer=signer)
    assert signed.key_id == 'fixture'
    assert signed.verify(signer=signer) is True
    assert signed.model_copy(update={'run_id': 'tampered'}).verify(signer=signer) is False
