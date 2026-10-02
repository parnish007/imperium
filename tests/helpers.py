"""Shared test helpers: a temporary Imperium home with a daemon running in a thread."""
import os
import tempfile
import threading

from imperium import cli, client, daemon


class TempHome:
    def __init__(self, config_text=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        self.config_text = config_text
        self.d = None
        self.thread = None

    def init(self):
        rc = cli.main(["--home", self.home, "--json", "init"], out=_Null(), err=_Null())
        assert rc == 0, rc
        if self.config_text is not None:
            with open(os.path.join(self.home, "imperium.toml"), "w", encoding="utf-8") as f:
                f.write(self.config_text)
        return self

    def start(self):
        self.d = daemon.Daemon(self.home)
        self.d.start()
        self.thread = threading.Thread(target=self.d.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        if self.d:
            self.d.shutdown()
            self.thread.join(5)
            self.d = None

    def client(self, as_owner=True, env=None):
        return client.Client(self.home, as_owner=as_owner, env=env if env is not None else {})

    def cleanup(self):
        self.stop()
        self.tmp.cleanup()


class _Null:
    def write(self, s):
        return len(s)

    def flush(self):
        pass
