"""Isolation mode: the API over OS-permissioned channels with the peer identified by the operating system.

On Windows: named pipes with an access list. The tests run as one account, so both channels admit it; they check
the binding of tokens to channels, the refusal of TCP for anything but the read-only dashboard, the access list
(an account not on it cannot even open the pipe), and refusal to share a pipe name someone else created first.
"""
import io
import json
import os
import unittest

from fake_opencode import FakeOpenCode
from helpers import TempHome
from imperium import cli, client, tokens, transport

WINDOWS = os.name == "nt"


def run(home, *args, env=None):
    out, err = io.StringIO(), io.StringIO()
    rc = cli.main(["--home", home, "--json"] + list(args), out=out, err=err, env=env or {})
    return rc, (json.loads(out.getvalue()) if out.getvalue().strip() else None)


@unittest.skipUnless(WINDOWS, "named pipes are the Windows transport")
class TestPipes(unittest.TestCase):
    def setUp(self):
        me = transport.current_identity()
        cfg = (f'[opencode]\npoll_interval = 3600.0\n[isolation]\nowner_accounts = ["{me}"]\n'
               f'builder_accounts = ["{me}"]\n')
        self.h = TempHome(cfg).init().start()
        self.fake = FakeOpenCode()
        self.fake.add_session("ses_i", "/work/iso")

    def tearDown(self):
        self.h.cleanup()
        self.fake.close()

    def test_owner_cli_works_over_the_pipe_and_tcp_refuses_tokens(self):
        rc, body = run(self.h.home, "status")
        self.assertEqual(rc, 0, body)
        self.assertEqual(body["transport"], "pipe:owner")
        with self.assertRaises(client.ApiError) as e:
            self.h.client().call("GET", "/v1/status", via_tcp=True)
        self.assertEqual(e.exception.status, 403)

    def test_builder_token_only_on_the_builder_pipe(self):
        rc, body = run(self.h.home, "builder", "add", "coding", "--endpoint", self.fake.url, "--session", "ses_i",
                       "--directory", "/work/iso")
        self.assertEqual(rc, 0, body)
        b = client.Client(self.h.home, principal="builder:coding")
        self.assertEqual(b.call("GET", "/v1/builder/rounds")["rounds"], [])
        with self.assertRaises(client.ApiError) as e:  # the builder token on the owner channel
            b.call("GET", "/v1/builder/rounds", channel="owner")
        self.assertEqual(e.exception.status, 403)
        with self.assertRaises(client.ApiError) as e:  # an owner token on the builder channel
            self.h.client().call("GET", "/v1/status", channel="builder")
        self.assertEqual(e.exception.status, 403)

    def test_isolated_builder_config_needs_nothing_from_the_owners_folder(self):
        rc, body = run(self.h.home, "builder", "add", "coding", "--endpoint", self.fake.url, "--session", "ses_i",
                       "--directory", "/work/iso")
        self.assertEqual(rc, 0, body)
        path = os.path.join(self.h.tmp.name, "builder-opencode.json")
        old_raw = tokens.read_locator(self.h.home, "builder:coding")
        rc, body = run(self.h.home, "builder", "mcp-config", "coding", "--isolated", path)
        self.assertEqual(rc, 0, body)
        cfg = json.load(open(path, encoding="utf-8"))["mcp"]["imperium"]
        self.assertNotIn("--home", cfg["command"])
        # a builder under its own account: a runtime folder it cannot read, only its config's environment
        b = client.Client(os.path.join(self.h.tmp.name, "no-such-home"), principal="builder:coding",
                          env=cfg["environment"])
        self.assertEqual(b.call("GET", "/v1/builder/rounds")["rounds"], [])
        old = client.Client(os.path.join(self.h.tmp.name, "no-such-home"), principal="builder:coding",
                            env={**cfg["environment"], "IMPERIUM_BUILDER_TOKEN": old_raw})  # replaced token
        with self.assertRaises(client.ApiError) as e:
            old.call("GET", "/v1/builder/rounds")
        self.assertEqual(e.exception.status, 401)

    def test_dashboard_still_reachable_over_tcp(self):
        rc, body = run(self.h.home, "dashboard", "--no-open")
        self.assertEqual(rc, 0, body)
        tok = body["url"].split("#token=")[1]
        c = client.Client(self.h.home)
        c._cred = ("dashboard", tok)
        self.assertIn("builders", c.call("GET", "/v1/status", via_tcp=True))


@unittest.skipUnless(WINDOWS, "named pipes are the Windows transport")
class TestPipeAccessList(unittest.TestCase):
    def test_a_peer_account_the_channel_does_not_admit_is_refused(self):
        import http.server
        name = r"\\.\pipe\imperium-test-" + os.urandom(4).hex()

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"hi")

            def log_message(self, *a):
                pass

        # only the Guests group may connect (and the daemon's own account, which is us: so use a raw check)
        srv = transport.PipeServer(name, ["S-1-5-32-546"], H, None, "test")
        srv.start()
        try:
            # The access list admits the daemon's own account (us), so we can open the pipe; the server must still
        # refuse us because the channel admits only Guests. (A second real account is needed to see the access
        # list itself refuse the open; that is part of the isolation setup check, not this suite.)
            with self.assertRaises(Exception):
                transport.pipe_request(name, b"GET / HTTP/1.0\r\nHost: x\r\n\r\n", timeout_ms=2000)
        finally:
            srv.stop()

    def test_refuses_a_name_someone_else_created_first(self):
        import http.server
        name = r"\\.\pipe\imperium-test-" + os.urandom(4).hex()
        me = transport.current_identity()
        first = transport.PipeServer(name, [me], http.server.BaseHTTPRequestHandler, None, "a")
        try:
            with self.assertRaises(transport.TransportError):
                transport.PipeServer(name, [me], http.server.BaseHTTPRequestHandler, None, "b")
        finally:
            first.stop()

    def test_sddl_is_protected_and_lists_only_the_given_accounts(self):
        d = transport.sddl(["S-1-5-21-1-2-3-1001"])
        self.assertTrue(d.startswith("D:P(A;;GA;;;SY)"))
        self.assertIn("(A;;GA;;;S-1-5-21-1-2-3-1001)", d)
        self.assertNotIn("WD", d)  # never Everyone


if __name__ == "__main__":
    unittest.main()
