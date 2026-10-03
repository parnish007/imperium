"""Regression tests for the review, with no network services or model credentials required.

Each test asserts an observable guarantee: untrusted code never runs, a baseline setup failure never verifies,
wrong-session evidence never advances delivery, progress prevents false alarms, and a slow builder is isolated.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from imperium import acp, builders, check_runner, config, daemon, engine, feeds, liveness, rounds, snapshot, verify
from imperium.store import Store, meta_get, meta_set


class VerificationFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root/'workspace'
        self.ws.mkdir()
        (self.ws/'value.txt').write_text('old')
        (self.ws/'check.py').write_text("from pathlib import Path\nraise SystemExit(0 if Path('value.txt').read_text() == 'new' else 1)\n")
        for args in (['init', '-q'], ['add', '.'], ['-c', 'user.name=test', '-c', 'user.email=test@example.invalid',
                                                 'commit', '-qm', 'base']):
            subprocess.run(['git', '-C', str(self.ws), *args], check=True, capture_output=True)
        self.base = snapshot.take(str(self.ws), 'refs/imperium/probe/base')
        self.store = Store(str(self.root/'state.db'))
        self.addCleanup(self.store.close)
        cfg = config.defaults()
        cfg['verification']['backend'] = 'unsafe-local'  # these fixtures contain only our own test programs
        self.d = SimpleNamespace(home=str(self.root), store=self.store, cfg=cfg,
                                 engine=SimpleNamespace(clock=time.time, secrets=lambda rows: []))
        with self.store.tx() as conn:
            builders.add(conn, name='b', endpoint='http://127.0.0.1:1', session_id='s', directory=str(self.ws))
            r, _ = rounds.open_round(conn, builder='b', objective='value is new', client_key='test',
                                     principal='owner', now=time.time())
            self.rid = r['id']
            conn.execute("UPDATE rounds SET state='CLAIMED_READY',claim_state='ready',claim_generation=1,"
                         "objective_met=1,objective_generation=1,base_commit=?,base_tree=?,verify_job='test' WHERE id=?",
                         (self.base['commit'], self.base['tree'], self.rid))
            conn.execute('INSERT INTO checkpoints(builder,data,updated) VALUES(?,?,?)',
                         ('b', json.dumps({'status': 'idle', 'open': {}}), 'now'))
        self.v = verify.Verifier(self.d)

    def define(self, **overrides):
        args = dict(cid='check', scope='builder:b', argv=[sys.executable, 'check.py'], working_dir='.', env=[],
                    timeout=5, must_fail_on_base=True, depends=verify.hash_depends(str(self.ws), ['check.py']),
                    required=True, principal='owner')
        args.update(overrides)
        with self.store.tx() as conn:
            rounds.define_check(conn, **args)

    def run_verify(self):
        (self.ws/'value.txt').write_text('new')
        self.v.run(self.rid, 'test')
        with self.store.read() as conn:
            return rounds.get(conn, self.rid), rounds.runs(conn, self.rid)


class TestVerification(VerificationFixture):
    def test_changed_checker_never_executes_and_no_other_check_runs(self):
        self.define()
        marker = self.root/'outside-snapshot'
        (self.ws/'check.py').write_text('from pathlib import Path\nPath('+repr(str(marker))+').write_text("ran")\n')
        r, runs = self.run_verify()
        self.assertFalse(marker.exists())
        self.assertTrue(r['untrusted'])
        self.assertEqual(runs, [])
        self.assertNotEqual(r['state'], 'VERIFIED')
        self.assertIsNone(r['verify_job'])

    def test_valid_candidate_and_expected_baseline_failure_verify(self):
        self.define()
        r, runs = self.run_verify()
        self.assertEqual(r['state'], 'VERIFIED')
        self.assertEqual([(x['target'], x['exit_code']) for x in runs], [('candidate', 0), ('base', 1)])
        self.assertTrue(all(x['execution']['backend'] == 'unsafe-local' for x in runs))

    def test_infrastructure_failure_does_not_satisfy_baseline(self):
        self.define()
        real = self.v._run_on
        for code, timeout, error in ((None, False, 'start_failed'), (2, False, None), (1, True, 'timeout'),
                                     (-9, False, None), (1, False, 'output_limit'), (125, False, None)):
            with self.subTest(code=code, timeout=timeout, error=error):
                def run(*args):
                    if args[-2] == 'base':
                        return {'exit_code': code, 'timed_out': timeout, 'execution': {'error': error}}
                    return real(*args)
                with patch.object(self.v, '_run_on', side_effect=run):
                    r, _ = self.run_verify()
                self.assertNotEqual(r['state'], 'VERIFIED')
                self.assertIsNone(r['checks_ok_generation'])

    def test_explicit_baseline_code_is_used(self):
        (self.ws/'check.py').write_text("from pathlib import Path\nraise SystemExit(0 if Path('value.txt').read_text() == 'new' else 3)\n")
        # Commit the trusted checker before the base snapshot as in normal onboarding.
        self.base = snapshot.take(str(self.ws), 'refs/imperium/probe/base3')
        with self.store.tx() as conn:
            conn.execute('UPDATE rounds SET base_commit=? WHERE id=?', (self.base['commit'], self.rid))
        self.define(base_failure_codes=[3])
        r, _ = self.run_verify()
        self.assertEqual(r['state'], 'VERIFIED')

    def test_default_backend_refuses_host_execution_without_image(self):
        self.define()
        self.d.cfg['verification'] = config.defaults()['verification']
        with patch('imperium.verify.subprocess.Popen', side_effect=AssertionError('host checker executed')):
            # Snapshot uses subprocess too, so exercise the backend boundary directly here.
            result = check_runner.run(self.d.cfg['verification'], False, [sys.executable, 'check.py'],
                                      str(self.ws), str(self.ws), [], 5, str(self.root/'out.log'), {})
        self.assertIsNone(result['exit_code'])
        self.assertTrue(result['execution']['error'])

    def test_isolation_refuses_unsafe_local_even_if_config_is_injected(self):
        result = check_runner.run(self.d.cfg['verification'], True, [sys.executable, 'check.py'],
                                  str(self.ws), str(self.ws), [], 5, str(self.root/'out.log'), {})
        self.assertIsNone(result['exit_code'])
        self.assertIn('forbidden', result['execution']['error'])

    def test_cancelled_verification_never_publishes_success(self):
        self.define()
        self.v.abort_all()
        r, runs = self.run_verify()
        self.assertNotEqual(r['state'], 'VERIFIED')
        self.assertEqual(runs, [])


class TestACP(unittest.TestCase):
    def setUp(self):
        b = {'name': 'b', 'endpoint': acp.endpoint_for(['unused']), 'directory': '/tmp', 'session_id': 'session'}
        self.c = acp.Connection(b, '/tmp')
        self.c.proc = SimpleNamespace(poll=lambda: None)
        self.c.ready = True
        self.sent = []
        self.c._write = self.sent.append
        self.c.prompt_async('session', 'msg', [{'type': 'text', 'text': '[imperium msg=a1 builder=b]\nwork'}])

    def update(self, session='session', kind='agent_message_chunk'):
        self.c._update({'sessionId': session, 'update': {'sessionUpdate': kind,
                                                       'content': {'type': 'text', 'text': 'progress'}}})

    def test_wrong_or_missing_session_cannot_prove_delivery_or_progress(self):
        for sid in ('wrong', None):
            self.update(sid)
        self.assertEqual(self.c.progress, 0)
        self.assertNotIn('msg', self.c.seen)
        self.assertEqual(self.c._drain(), [])
        self.update()
        self.assertIn('msg', self.c.seen)

    def test_wrong_session_permission_is_cancelled_and_never_presented(self):
        self.c._permission('ask', {'sessionId': 'wrong', 'toolCall': {'kind': 'execute'}})
        self.assertFalse(self.c.perms)
        self.assertEqual(self.sent[-1]['result']['outcome']['outcome'], 'cancelled')

    def test_progress_suppresses_stall_but_real_silence_triggers_it(self):
        h = {}
        for now in (0, 50, 101, 180):
            self.update()
            _, cp = next(self.c.poll(None))
            self.assertIsNone(cp['busy_children'])
            self.assertIsNone(liveness.check_stall(h, 'WORKING', cp, now, 100, 1800))
        self.assertEqual(liveness.check_stall(h, 'WORKING', cp, 281, 100, 1800)[0], 'SUSPECTED_STALL')

    def test_non_turn_noise_is_not_progress(self):
        self.update(kind='usage_update')
        self.assertEqual(self.c.progress, 0)
        self.assertNotIn('msg', self.c.seen)

    def test_cancel_requires_prompt_response_and_never_fabricates_a_started_turn(self):
        self.assertEqual(self.c.cancel(), 'requested')
        self.assertEqual(self.sent[-1]['method'], 'session/cancel')
        self.assertFalse(self.c.cancel_confirmed)
        self.c._handle({'jsonrpc': '2.0', 'id': 'msg', 'result': {'stopReason': 'cancelled'}})
        self.assertTrue(self.c.cancel_confirmed)
        self.assertNotIn('msg', self.c.seen)
        self.assertNotIn('TURN_STARTED', [e['type'] for e in self.c._drain()])


class TestScheduling(unittest.TestCase):
    def test_slow_builder_does_not_block_healthy_builder_or_overlap_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, 'state.db'))
            cfg = config.defaults()
            cfg['opencode']['max_workers'] = 2
            with store.tx() as conn:
                for name in ('slow', 'healthy'):
                    builders.add(conn, name=name, endpoint='http://127.0.0.1:1', session_id=name,
                                 directory=os.path.join(tmp, name))
            d = SimpleNamespace(cfg=cfg, store=store, quarantine=lambda: False)
            e = engine.Engine(d)
            release, started, healthy = threading.Event(), threading.Event(), threading.Event()
            calls = []
            def poll(b, *_):
                calls.append(b['name'])
                if b['name'] == 'slow':
                    started.set()
                    release.wait(5)
                else:
                    healthy.set()
            e._poll = poll
            try:
                e.run_once(wait_for_polls=False)
                self.assertTrue(started.wait(2))
                self.assertTrue(healthy.wait(2))
                healthy.clear()
                e.run_once(wait_for_polls=False)
                self.assertTrue(healthy.wait(2))
                self.assertEqual(calls.count('slow'), 1)
            finally:
                release.set()
                e.stop()
                store.close()


class TestACPFailures(unittest.TestCase):
    def connection(self, tmp):
        c = acp.Connection({'name': 'probe', 'endpoint': acp.endpoint_for([sys.executable, '-c',
                            'import time; time.sleep(30)']), 'directory': tmp, 'session_id': 'new:probe'}, tmp)
        self.addCleanup(c.close)
        return c

    def test_initialize_timeout_retires_process_so_next_poll_can_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self.connection(tmp)
            with patch.object(acp, 'STARTUP_TIMEOUT', 0.05):
                with self.assertRaises(acp.opencode.OCUnreachable):
                    c.start()
            self.assertFalse(c.alive)
            self.assertFalse(c.ready)

    def test_blocked_stdin_write_is_bounded_and_process_is_retired(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self.connection(tmp)
            c.proc = subprocess.Popen(c.argv, cwd=tmp, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, bufsize=0,
                                      **({'start_new_session': True} if os.name != 'nt' else {}))
            with patch.object(acp, 'WRITE_TIMEOUT', 0.1):
                with self.assertRaises(acp.opencode.OCUnreachable):
                    c._write({'payload': 'x' * 1_000_000})
            c.proc.wait(timeout=5)
            self.assertIs(c.write_broken, c.proc)
            with self.assertRaises(acp.opencode.OCUnreachable):
                c._write({'method': 'session/cancel'})


class TestUpgrade(unittest.TestCase):
    def test_live_legacy_evidence_is_invalidated_without_rewriting_historical_decisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, 'state.db')
            with patch.object(Store, 'SCHEMA_VERSION', 7):
                old = Store(db)
                try:
                    with old.tx() as conn:
                        for name, state in (('live', 'VERIFIED'), ('past', 'ACCEPTED')):
                            builders.add(conn, name=name, endpoint='http://127.0.0.1:1', session_id=name,
                                         directory=os.path.join(tmp, name))
                            r, _ = rounds.open_round(conn, builder=name, objective='upgrade', client_key=name,
                                                     principal='owner', now=time.time())
                            conn.execute('UPDATE rounds SET state=?,checks_ok_generation=1,verify_job=? WHERE id=?',
                                         (state, 'old-job' if name == 'live' else None, r['id']))
                finally:
                    old.close()
            upgraded = Store(db)
            try:
                with upgraded.read() as conn:
                    live = conn.execute("SELECT * FROM rounds WHERE builder='live'").fetchone()
                    past = conn.execute("SELECT * FROM rounds WHERE builder='past'").fetchone()
                    self.assertEqual(live['state'], 'CLAIMED_READY')
                    self.assertIsNone(live['checks_ok_generation'])
                    self.assertIsNone(live['verify_job'])
                    self.assertEqual((past['state'], past['checks_ok_generation']), ('ACCEPTED', 1))
                    self.assertEqual(meta_get(conn, 'verification_policy_upgrade_pending'), '1')
                    self.assertEqual(meta_get(conn, 'schema_version'), '8')
            finally:
                upgraded.close()


class TestRunnerPolicy(unittest.TestCase):
    def test_container_options_limit_privileges_and_only_mount_disposable_inputs(self):
        cfg = config.defaults()['verification']
        cmd = check_runner.docker_command('docker', 'sha256:'+'a'*64, 'probe', '/snapshot', '/runner-stage', cfg)
        for flag in ('--network=none', '--read-only', '--user=65534:65534', '--cap-drop=ALL',
                     '--security-opt=no-new-privileges', '--pull=never'):
            self.assertIn(flag, cmd)
        mounts = [cmd[i+1] for i, arg in enumerate(cmd) if arg == '--mount']
        self.assertEqual(len(mounts), 2)
        self.assertTrue(all(m.endswith(',readonly') for m in mounts))
        with self.assertRaises(verify.VerifyError):
            check_runner.docker_command('docker', 'image', 'probe', '/x,readonly=false', '/runner', cfg)

    def test_output_is_bounded_and_never_counts_as_a_test_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = verify.run_check([sys.executable, '-c', 'import sys; sys.stdout.write("x"*2000000)'],
                                      tmp, [], 5, os.path.join(tmp, 'output.log'))
            self.assertEqual(result['execution']['error'], 'output_limit')
            self.assertLessEqual(os.path.getsize(result['output_path']), verify.OUTPUT_CAP + 100)

    def test_configuration_rejects_isolation_with_host_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'imperium.toml').write_text('[verification]\nbackend="unsafe-local"\n'
                                                '[isolation]\nowner_accounts=["1000"]\n')
            with self.assertRaises(config.ConfigError):
                config.load(tmp)


class TestAbort(VerificationFixture):
    def setUp(self):
        super().setUp()
        self.d.quarantine = lambda: False
        self.e = engine.Engine(self.d)
        self.d.engine = self.e
        self.d.verifier = self.v
        self.addCleanup(self.e.stop)

    def row(self):
        with self.store.read() as conn:
            return dict(conn.execute("SELECT * FROM cancellations WHERE builder='b'").fetchone())

    def test_pause_does_not_claim_to_cancel_and_abort_records_outcomes(self):
        daemon.r_stop_all(self.d, 'director:test', {}, {})
        with self.store.read() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM cancellations').fetchone()[0], 0)
        daemon.r_abort_all(self.d, 'director:test', {}, {})
        self.assertEqual(self.row()['state'], 'pending')
        with self.assertRaises(feeds.Refused):
            daemon.r_resume_all(self.d, 'owner', {}, {})
        b = {'name': 'b', 'adapter': 'opencode-http', 'session_id': 's'}
        sent = []
        client = SimpleNamespace(abort=lambda sid: sent.append(sid), status_map=lambda: {})
        self.e._abort(b, client)
        self.assertEqual(self.row()['state'], 'acknowledged')
        self.e._abort(b, client)
        self.assertEqual(self.row()['state'], 'idle_observed')
        self.assertEqual(sent, ['s'])
        daemon.r_resume_all(self.d, 'owner', {}, {})

    def test_lost_abort_response_is_uncertain_and_not_silently_retried(self):
        from imperium.opencode import OCUnreachable
        daemon.r_abort_all(self.d, 'director:test', {}, {})
        b = {'name': 'b', 'adapter': 'opencode-http', 'session_id': 's'}
        with patch('imperium.opencode.OpenCodeClient.abort', side_effect=OCUnreachable('lost')) as abort:
            from imperium.opencode import OpenCodeClient
            client = OpenCodeClient('http://127.0.0.1:1')
            self.e._abort(b, client)
            self.assertEqual(self.row()['state'], 'uncertain')
            self.e._abort(b, client)
            self.assertEqual(abort.call_count, 1)

    def test_unconfirmed_abort_expires_without_claiming_success(self):
        daemon.r_abort_all(self.d, 'director:test', {}, {})
        b = {'name': 'b', 'adapter': 'opencode-http', 'session_id': 's'}
        client = SimpleNamespace(abort=lambda _: None, status_map=lambda: {'s': {'type': 'busy'}})
        self.e._abort(b, client)
        self.e.clock = lambda: self.row()['updated'] + 31
        self.e._abort(b, client)
        self.assertEqual(self.row()['state'], 'uncertain')

    def test_abort_is_attempted_even_if_reading_was_halted(self):
        daemon.r_abort_all(self.d, 'director:test', {}, {})
        self.e.health['b'] = {'halted': True, 'next': time.monotonic() + 100, 'failures': 0}
        with self.store.read() as conn:
            b = builders.get(conn, 'b')
        with patch('imperium.opencode.OpenCodeClient.abort') as abort:
            self.e._poll(b, [], True)
        abort.assert_called_once_with('s')
        self.assertEqual(self.row()['state'], 'acknowledged')


if __name__ == '__main__':
    unittest.main()
