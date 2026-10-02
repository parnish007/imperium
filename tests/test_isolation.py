"""Isolation mode: the API over OS-permissioned channels with the peer identified by the operating system.

Windows: named pipes with an access list; Linux and macOS: Unix sockets with a peer-uid check. The tests run as
one account, so both channels admit it; they check the binding of tokens to channels, the refusal of TCP for
anything but the read-only dashboard, the per-connection peer check, and the refusal to share an endpoint someone
else prepared first.
"""
import http.server
import io
import json
import os
import shutil
import unittest

from fake_opencode import FakeOpenCode
from helpers import TempHome
from imperium import cli, client, tokens, transport

WINDOWS = os.name == "nt"


def run(home, *args, env=None):
    out, err = io.StringIO(), io.StringIO()
    rc = cli.main(["--home", home, "--json"] + list(args), out=out, err=err, env=env or {})
    return rc, (json.loads(out.getvalue()) if out.getvalue().strip() else None)


class TestChannels(unittest.TestCase):
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
        self.assertTrue(body["isolation"])
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
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)["mcp"]["imperium"]
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


class _Hello(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"hi")

    def log_message(self, *a):
        pass


REQ = b"GET / HTTP/1.0\r\nHost: x\r\n\r\n"


class TestPeerCheck(unittest.TestCase):
    def endpoint(self):
        if WINDOWS:
            return r"\\.\pipe\imperium-test-" + os.urandom(4).hex()
        d = os.path.join("/tmp", "imperium-t-" + os.urandom(4).hex())
        self.addCleanup(shutil.rmtree, d, True)
        return os.path.join(d, "x.sock")

    def server(self, name, allowed, channel="owner"):
        cls = transport.PipeServer if WINDOWS else transport.UnixServer
        srv = cls(name, allowed, _Hello, None, channel)
        srv.start()
        self.addCleanup(srv.stop)
        return srv

    def test_an_admitted_account_is_served(self):
        name = self.endpoint()
        self.server(name, [transport.current_identity()])
        status, body = transport.request(name, REQ, timeout_ms=3000)
        self.assertEqual((status, body), (200, b"hi"))

    def test_a_peer_account_the_channel_does_not_admit_is_refused(self):
        # The daemon's own account may open the endpoint (it is us); the per-connection check must still refuse us
        # because the channel admits only another account. (Seeing the operating system itself refuse the open
        # needs a second real account: that is step 7 of docs/ISOLATION.md, not this suite.)
        name = self.endpoint()
        self.server(name, ["S-1-5-32-546" if WINDOWS else "99999"])
        with self.assertRaises(Exception):
            transport.request(name, REQ, timeout_ms=2000)

    def test_refuses_an_endpoint_someone_else_prepared(self):
        name = self.endpoint()
        me = transport.current_identity()
        if WINDOWS:
            self.server(name, [me])  # the first instance holds the name
        else:
            os.makedirs(os.path.dirname(name))
            os.chmod(os.path.dirname(name), 0o777)  # writable by others: could hold a planted socket
        with self.assertRaises(transport.TransportError):
            (transport.PipeServer if WINDOWS else transport.UnixServer)(name, [me], _Hello, None, "b")


@unittest.skipUnless(WINDOWS, "access lists are the Windows mechanism")
class TestAccessList(unittest.TestCase):
    def test_sddl_is_protected_and_lists_only_the_given_accounts(self):
        d = transport.sddl(["S-1-5-21-1-2-3-1001"])
        self.assertTrue(d.startswith("D:P(A;;GA;;;SY)"))
        self.assertNotIn("WD", d)  # never Everyone
        ace = [a for a in d.split("(") if a.endswith("S-1-5-21-1-2-3-1001)")]
        self.assertEqual(len(ace), 1)
        rights = int(ace[0].split(";")[2], 16)  # an explicit mask: never GA for a client account
        self.assertEqual(rights & 0x4, 0)  # FILE_CREATE_PIPE_INSTANCE: could stand up a fake daemon pipe
        self.assertEqual(rights & (0x40000 | 0x80000 | 0x10000), 0)  # WRITE_DAC, WRITE_OWNER, DELETE
        self.assertEqual(rights & 0x3, 0x3)  # read and write data

    def test_a_client_cannot_create_an_instance_of_the_pipe(self):
        # The test runs as the daemon's own account, so check what a client *asks for*: the rights a client opens
        # the pipe with must not include creating instances (the access list then refuses that to builders).
        self.assertEqual(transport.CLIENT_OPEN & 0x4, 0)
        self.assertEqual(transport.CLIENT_OPEN & ~transport.CLIENT_RIGHTS, 0)


@unittest.skipIf(WINDOWS, "Unix sockets")
class TestUnixSocket(unittest.TestCase):
    def test_owner_socket_is_private_and_directory_not_listable(self):
        d = os.path.join("/tmp", "imperium-t-" + os.urandom(4).hex())
        self.addCleanup(shutil.rmtree, d, True)
        srv = transport.UnixServer(os.path.join(d, "owner.sock"), [transport.current_identity()], _Hello, None,
                                   "owner")
        self.addCleanup(srv.stop)
        self.assertEqual(os.stat(d).st_mode & 0o777, 0o711)
        self.assertEqual(os.stat(srv.name).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
