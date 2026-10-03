"""Real containment checks. CI supplies IMPERIUM_TEST_IMAGE as a local immutable image ID.

Without Docker these are explicitly skipped; unit tests of argv construction are not called containment proof.
"""
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

from imperium import check_runner, config, verify


@unittest.skipUnless(os.environ.get('IMPERIUM_TEST_IMAGE'), 'requires the container-verification CI job')
class TestContainerVerification(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.snapshot = self.root/'snapshot'
        self.snapshot.mkdir()
        self.cfg = config.defaults()['verification']
        self.cfg['image'] = os.environ['IMPERIUM_TEST_IMAGE']

    def run_check(self, code, *, timeout=20, cancel=None):
        (self.snapshot/'check.py').write_text(code)
        return check_runner.run(self.cfg, True, ['python3', 'check.py'], str(self.snapshot), str(self.snapshot),
                                [], timeout, str(self.root/'output.log'), {}, cancel=cancel)

    def assertPassed(self, result):
        text = Path(result['output_path']).read_text() if result['output_path'] else ''
        self.assertFalse(result['execution']['error'], (result, text))
        self.assertEqual(result['exit_code'], 0, (result, text))

    def test_candidate_has_no_host_credentials_root_write_network_or_writable_input(self):
        secret = self.root/'owner-token'
        secret.write_text('sentinel')
        code = f'''import os, socket
from pathlib import Path
assert os.geteuid() == 65534
assert not Path({str(secret)!r}).exists()
assert not Path('/var/run/docker.sock').exists()
for path in ('/input/escape', '/root/escape', '/runner/escape'):
    try:
        Path(path).write_text('bad')
    except OSError:
        pass
    else:
        raise AssertionError(path + ' is writable')
Path('local-output').write_text('allowed')
try:
    socket.create_connection(('198.51.100.1', 9), timeout=0.2)
except OSError:
    pass
else:
    raise AssertionError('unexpected external network access')
'''
        self.assertPassed(self.run_check(code))
        self.assertEqual(secret.read_text(), 'sentinel')
        self.assertFalse((self.snapshot/'local-output').exists())

    def test_held_out_checker_imports_candidate_without_host_access(self):
        marker = self.root/'owner-marker'
        marker.write_text('unchanged')
        (self.snapshot/'candidate.py').write_text(f'''from pathlib import Path
try:
    Path({str(marker)!r}).write_text('changed')
except OSError:
    pass
else:
    raise AssertionError('candidate could reach host')
''')
        held = self.root/'held.py'
        held.write_text("import sys\nsys.path.insert(0, '.')\nimport candidate\n")
        result = check_runner.run(self.cfg, True, ['python3', str(held)], str(self.snapshot), str(self.snapshot),
                                  [], 20, str(self.root/'out.log'), {str(held): verify.file_hash(held)})
        self.assertPassed(result)
        self.assertEqual(marker.read_text(), 'unchanged')

    def test_timeout_is_an_execution_error(self):
        result = self.run_check('import time\ntime.sleep(30)\n', timeout=1)
        self.assertTrue(result['timed_out'])
        self.assertTrue(result['execution']['error'])

    def test_cancellation_does_not_run_candidate(self):
        cancel = threading.Event()
        cancel.set()
        result = self.run_check('raise AssertionError("must not run")\n', cancel=cancel)
        self.assertEqual(result['execution']['error'], 'cancelled')

    def test_running_check_is_cancelled_before_its_timeout(self):
        cancel = threading.Event()
        timer = threading.Timer(2, cancel.set)
        timer.start()
        try:
            result = self.run_check('import time\ntime.sleep(30)\n', timeout=20, cancel=cancel)
        finally:
            timer.cancel()
        self.assertEqual(result['execution']['error'], 'cancelled')
        self.assertFalse(result['timed_out'])
        self.assertLess(result['duration'], 10)

    def test_missing_command_is_setup_failure_not_baseline_evidence(self):
        result = check_runner.run(self.cfg, True, ['imperium-nonexistent-command'], str(self.snapshot),
                                  str(self.snapshot), [], 20, str(self.root/'out.log'), {})
        self.assertEqual(result['exit_code'], 125)
        self.assertEqual(result['execution']['error'], 'container_or_command_setup_failed')
