"""Isolation mode transports (SYSTEM-DESIGN §9): the local API over an OS-permissioned channel.

On Windows: named pipes whose access list (DACL) admits only the accounts named in the configuration, created with
FILE_FLAG_FIRST_PIPE_INSTANCE (a process that grabbed the name first makes the daemon refuse to start, instead of
being talked to) and PIPE_REJECT_REMOTE_CLIENTS. On Linux/macOS: Unix sockets in a private directory that others
may only traverse (0711), the owner socket 0600; a directory another account could have prepared is refused. Every
connection is identified by the operating system: the account of the process at the other end (Windows: the
client's security context at identification level -> user SID; Unix: SO_PEERCRED / getpeereid). The same HTTP API runs
over the channel, so nothing above the transport changes.

Two channels: `owner` (the owner and the director, who run as the owner's account) and `builder` (builders, under
their own account). A builder token is accepted only on the builder channel, and every other token only on the
owner channel; the peer account must be one the channel admits.
"""
import io
import os
import socket
import stat
import sys
import threading

if os.name == "nt":
    import ctypes
    from ctypes import wintypes as wt

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    PIPE_ACCESS_DUPLEX = 0x3
    FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
    PIPE_REJECT_REMOTE_CLIENTS = 0x8
    PIPE_UNLIMITED_INSTANCES = 255
    INVALID_HANDLE = wt.HANDLE(-1).value
    ERROR_PIPE_CONNECTED = 535
    ERROR_BROKEN_PIPE = 109
    ERROR_PIPE_BUSY = 231
    ERROR_ACCESS_DENIED = 5
    GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
    OPEN_EXISTING = 3

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("nLength", wt.DWORD), ("lpSecurityDescriptor", wt.LPVOID), ("bInheritHandle", wt.BOOL)]

    k32.CreateNamedPipeW.restype = wt.HANDLE
    k32.CreateNamedPipeW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD,
                                     ctypes.POINTER(SECURITY_ATTRIBUTES)]
    k32.CreateFileW.restype = wt.HANDLE
    k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.LPVOID, wt.DWORD, wt.DWORD, wt.HANDLE]
    k32.ConnectNamedPipe.argtypes = [wt.HANDLE, wt.LPVOID]
    k32.DisconnectNamedPipe.argtypes = [wt.HANDLE]
    k32.FlushFileBuffers.argtypes = [wt.HANDLE]
    k32.CloseHandle.argtypes = [wt.HANDLE]
    k32.ReadFile.argtypes = [wt.HANDLE, wt.LPVOID, wt.DWORD, ctypes.POINTER(wt.DWORD), wt.LPVOID]
    k32.WriteFile.argtypes = [wt.HANDLE, wt.LPCVOID, wt.DWORD, ctypes.POINTER(wt.DWORD), wt.LPVOID]
    k32.GetNamedPipeClientProcessId.argtypes = [wt.HANDLE, ctypes.POINTER(wt.ULONG)]
    k32.OpenProcess.restype = wt.HANDLE
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k32.WaitNamedPipeW.argtypes = [wt.LPCWSTR, wt.DWORD]
    k32.GetCurrentProcess.restype = wt.HANDLE
    adv.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
    adv.GetTokenInformation.argtypes = [wt.HANDLE, ctypes.c_int, wt.LPVOID, wt.DWORD, ctypes.POINTER(wt.DWORD)]
    adv.ConvertSidToStringSidW.argtypes = [wt.LPVOID, ctypes.POINTER(wt.LPWSTR)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wt.LPCWSTR, wt.DWORD,
                                                                         ctypes.POINTER(wt.LPVOID), wt.LPVOID]
    k32.LocalFree.argtypes = [wt.HLOCAL]
    k32.GetCurrentThread.restype = wt.HANDLE
    adv.ImpersonateNamedPipeClient.argtypes = [wt.HANDLE]
    adv.OpenThreadToken.argtypes = [wt.HANDLE, wt.DWORD, wt.BOOL, ctypes.POINTER(wt.HANDLE)]
    # What a client may do with a pipe: read, write, attributes, wait. Not FILE_CREATE_PIPE_INSTANCE (0x4: a
    # client holding it could stand up its own instance of the daemon's pipe and receive other clients' tokens),
    # not WRITE_DAC, WRITE_OWNER or DELETE (S5 review C8).
    CLIENT_RIGHTS = 0x1 | 0x2 | 0x80 | 0x100 | 0x20000 | 0x100000
    CLIENT_OPEN = 0x1 | 0x2 | 0x80 | 0x100 | 0x100000  # what a client asks for when it opens the pipe
    SECURITY_SQOS_PRESENT, SECURITY_IDENTIFICATION = 0x00100000, 0x00010000


class TransportError(RuntimeError):
    pass


def current_identity():
    """The account this process runs as: a SID string on Windows, a uid on Unix."""
    if os.name == "nt":
        return _process_sid(k32.GetCurrentProcess(), own=True)
    return str(os.getuid())


# --- Windows -------------------------------------------------------------------------------------------------

def _process_sid(hproc, own=False):
    tok = wt.HANDLE()
    if not adv.OpenProcessToken(hproc, 0x0008, ctypes.byref(tok)):  # TOKEN_QUERY
        raise TransportError(f"OpenProcessToken failed ({ctypes.get_last_error()})")
    return _token_sid(tok)


def _token_sid(tok):
    """The user SID of a token, as a string; closes the token."""
    try:
        need = wt.DWORD()
        adv.GetTokenInformation(tok, 1, None, 0, ctypes.byref(need))  # TokenUser
        buf = ctypes.create_string_buffer(need.value)
        if not adv.GetTokenInformation(tok, 1, buf, need, ctypes.byref(need)):
            raise TransportError(f"GetTokenInformation failed ({ctypes.get_last_error()})")
        psid = ctypes.cast(buf, ctypes.POINTER(wt.LPVOID))[0]
        s = wt.LPWSTR()
        if not adv.ConvertSidToStringSidW(psid, ctypes.byref(s)):
            raise TransportError("ConvertSidToStringSidW failed")
        try:
            return s.value
        finally:
            k32.LocalFree(ctypes.cast(s, wt.HLOCAL))
    finally:
        k32.CloseHandle(tok)


def peer_sid(handle):
    """The account at the other end of the pipe. Taken from the client's own security context by impersonating
    it at identification level, which needs no access to the client's process: opening another account's process
    token needs a privilege an ordinary account lacks (S5 review C9). Call only after reading from the pipe (the
    context is that of the last message read). Impersonation is always reverted; if that ever fails the daemon
    stops rather than go on acting as the client."""
    if not adv.ImpersonateNamedPipeClient(handle):
        raise TransportError(f"cannot identify the pipe client ({ctypes.get_last_error()})")
    tok = wt.HANDLE()
    try:
        ok = adv.OpenThreadToken(k32.GetCurrentThread(), 0x0008, True, ctypes.byref(tok))  # TOKEN_QUERY, as self
        err = ctypes.get_last_error()
    finally:
        if not adv.RevertToSelf():
            os._exit(70)
    if not ok:
        raise TransportError(f"cannot read the pipe client's identity ({err})")
    return _token_sid(tok)


def pipe_name(home, channel):
    """One name per runtime directory and channel, so two installations never share a pipe."""
    import hashlib
    tag = hashlib.sha256(os.path.abspath(home).lower().encode()).hexdigest()[:12]
    return rf"\\.\pipe\imperium-{tag}-{channel}"


def sddl(allowed_sids):
    """Protected DACL: full access for SYSTEM and the daemon's own account; read and write, and nothing more, for
    the allowed accounts; nobody else."""
    me = current_identity()
    others = []
    for sid in allowed_sids:
        if sid != me and sid not in others:
            others.append(sid)
    return (f"D:P(A;;GA;;;SY)(A;;GA;;;{me})"
            + "".join(f"(A;;0x{CLIENT_RIGHTS:x};;;{sid})" for sid in others))


class _PipeIO(io.RawIOBase):
    def __init__(self, handle, first=b""):
        self.h = handle
        self.first = first  # bytes already read (to identify the client) and not yet consumed

    def readable(self):
        return True

    def writable(self):
        return True

    def read_some(self, size=65536):
        buf = bytearray(size)
        n = self.readinto(buf)
        return bytes(buf[:n])

    def readinto(self, b):
        if self.first:
            n = min(len(b), len(self.first))
            b[:n] = self.first[:n]
            self.first = self.first[n:]
            return n
        n = wt.DWORD()
        buf = (ctypes.c_char * len(b)).from_buffer(b)
        if not k32.ReadFile(self.h, buf, len(b), ctypes.byref(n), None):
            err = ctypes.get_last_error()
            if err in (ERROR_BROKEN_PIPE, 232, 233):  # broken, being closed, no process
                return 0
            if err != 234:  # ERROR_MORE_DATA: a partial read is fine in byte mode
                raise OSError(err, "ReadFile failed")
        return n.value

    def write(self, b):
        data = bytes(b)
        done = 0
        while done < len(data):
            n = wt.DWORD()
            chunk = data[done:done + 65536]
            if not k32.WriteFile(self.h, chunk, len(chunk), ctypes.byref(n), None):
                raise OSError(ctypes.get_last_error(), "WriteFile failed")
            done += n.value
        return len(data)


class _PipeSock:
    """Enough of a socket for http.server and http.client."""

    def __init__(self, handle, first=b""):
        self.h = handle
        self.raw = _PipeIO(handle, first)

    def makefile(self, mode="rb", buffering=None, **kw):
        if "r" in mode:
            return io.BufferedReader(self.raw, 65536)
        return io.BufferedWriter(self.raw, 65536)

    def sendall(self, data):
        self.raw.write(data)

    def settimeout(self, t):
        pass

    def setsockopt(self, *a):
        pass

    def close(self):
        pass


class PipeServer:
    """Serve `handler_class` (an http.server handler) on a named pipe admitting only `allowed_sids`."""

    def __init__(self, name, allowed_sids, handler_class, server, channel):
        self.name, self.allowed = name, set(allowed_sids)
        self.handler_class, self.server, self.channel = handler_class, server, channel
        sa = SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
        sd = wt.LPVOID()
        if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl(self.allowed), 1, ctypes.byref(sd),
                                                                       None):
            raise TransportError(f"bad security descriptor ({ctypes.get_last_error()})")
        sa.lpSecurityDescriptor = sd
        sa.bInheritHandle = False
        self.sa = sa
        self.stop_event = threading.Event()
        self.first = self._instance(first=True)  # fails if anyone else already created this name
        self.thread = None

    def _instance(self, first=False):
        flags = PIPE_ACCESS_DUPLEX | (FILE_FLAG_FIRST_PIPE_INSTANCE if first else 0)
        h = k32.CreateNamedPipeW(self.name, flags, PIPE_REJECT_REMOTE_CLIENTS, PIPE_UNLIMITED_INSTANCES, 65536,
                                 65536, 0, ctypes.byref(self.sa))
        if h == INVALID_HANDLE or h is None:
            err = ctypes.get_last_error()
            if first and err == ERROR_ACCESS_DENIED:
                raise TransportError(f"{self.name} already exists: another process created it first (refusing to "
                                     "start rather than share it)")
            raise TransportError(f"CreateNamedPipe failed ({err})")
        return h

    def start(self):
        self.thread = threading.Thread(target=self._loop, name=f"imperium-pipe-{self.channel}", daemon=True)
        self.thread.start()

    def _loop(self):
        h = self.first
        while not self.stop_event.is_set():
            ok = k32.ConnectNamedPipe(h, None)
            if not ok and ctypes.get_last_error() != ERROR_PIPE_CONNECTED:
                k32.CloseHandle(h)
                h = self._instance()
                continue
            if self.stop_event.is_set():
                k32.CloseHandle(h)
                return
            threading.Thread(target=self._serve, args=(h,), daemon=True).start()
            h = self._instance()

    def _serve(self, h):
        served = False
        try:
            first = _PipeIO(h).read_some()  # the client's identity is that of the last message read
            if not first:
                return
            try:
                sid = peer_sid(h)
            except TransportError:
                return
            if sid not in self.allowed:
                return
            served = True
            self.handler_class(_PipeSock(h, first), ("pipe", self.channel, sid), self.server)
        except Exception:  # one bad client must not stop the server
            pass
        finally:
            if served:  # DisconnectNamedPipe discards what the client has not read yet: wait until it has
                k32.FlushFileBuffers(h)
            k32.DisconnectNamedPipe(h)
            k32.CloseHandle(h)

    def stop(self):
        self.stop_event.set()
        try:  # wake the blocked ConnectNamedPipe
            h = k32.CreateFileW(self.name, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
            if h != INVALID_HANDLE:
                k32.CloseHandle(h)
        except OSError:
            pass


def pipe_request(name, raw_request, timeout_ms=5000):
    """Send one HTTP request over the pipe; returns (status, body bytes)."""
    import http.client
    if not k32.WaitNamedPipeW(name, timeout_ms):
        raise ConnectionError(f"{name} is not available ({ctypes.get_last_error()})")
    # only the rights the access list grants clients; identification level, so the server can learn who we are
    # but cannot act as us
    h = k32.CreateFileW(name, CLIENT_OPEN, 0, None, OPEN_EXISTING, SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION,
                        None)
    if h == INVALID_HANDLE or h is None:
        raise ConnectionError(f"cannot open {name} ({ctypes.get_last_error()}): is this account allowed?")
    try:
        sock = _PipeSock(h)
        sock.sendall(raw_request)
        resp = http.client.HTTPResponse(sock)
        resp.begin()
        return resp.status, resp.read()
    finally:
        k32.CloseHandle(h)


# --- Unix ----------------------------------------------------------------------------------------------------

def peer_uid(sock):
    if sys.platform.startswith("linux"):
        import struct
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return str(struct.unpack("3i", creds)[1])
    import ctypes as c
    libc = c.CDLL(None)
    uid, gid = c.c_uint(), c.c_uint()
    if libc.getpeereid(sock.fileno(), c.byref(uid), c.byref(gid)) != 0:
        raise TransportError("getpeereid failed")
    return str(uid.value)


def socket_dir(home):
    """A directory for the sockets outside the runtime folder (builders under another account must reach the
    builder socket but nothing in the runtime folder). Short, because socket paths are limited to ~100 bytes."""
    import hashlib
    import tempfile
    tag = hashlib.sha256(os.path.abspath(home).encode()).hexdigest()[:12]
    base = "/tmp" if os.path.isdir("/tmp") else tempfile.gettempdir()
    return os.path.join(base, f"imperium-{os.getuid()}-{tag}")


def _own_dir(path):
    """Create the socket directory, or reuse it only if it is ours, a real directory, and nobody else can write
    to it; anything else may have been prepared by another account to intercept the connection."""
    try:
        os.mkdir(path, 0o711)
    except FileExistsError:
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
            raise TransportError(f"{path} exists and is not a private directory of this account: refusing to start "
                                 "rather than share it") from None
    os.chmod(path, 0o711)  # others may reach a socket by name, not list or create anything


class UnixServer:
    """Serve `handler_class` on a Unix socket admitting only the uids in `allowed` (checked per connection)."""

    def __init__(self, path, allowed, handler_class, server, channel):
        self.name, self.allowed = path, {str(a) for a in allowed}
        self.handler_class, self.server, self.channel = handler_class, server, channel
        _own_dir(os.path.dirname(path))
        try:
            os.unlink(path)  # a stale socket from an earlier run, in our own private directory
        except FileNotFoundError:
            pass
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        os.chmod(path, 0o666 if channel == "builder" else 0o600)
        self.sock.listen(64)
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._loop, name=f"imperium-sock-{self.channel}", daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            try:
                uid = peer_uid(conn)
            except (OSError, TransportError):
                return
            if uid not in self.allowed:
                return
            self.handler_class(conn, ("pipe", self.channel, uid), self.server)
        except Exception:  # one bad client must not stop the server
            pass
        finally:
            conn.close()

    def stop(self):
        self.stop_event.set()
        try:
            self.sock.close()
            os.unlink(self.name)
        except OSError:
            pass


def unix_request(path, raw_request, timeout_ms=5000):
    import http.client
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout_ms / 1000)
    try:
        sock.connect(path)
    except OSError as e:
        sock.close()
        raise ConnectionError(f"cannot connect to {path} ({e}): is this account allowed?") from None
    try:
        sock.sendall(raw_request)
        resp = http.client.HTTPResponse(sock)
        resp.begin()
        return resp.status, resp.read()
    finally:
        sock.close()


# --- either ---------------------------------------------------------------------------------------------------

def endpoint(home, channel):
    if os.name == "nt":
        return pipe_name(home, channel)
    return os.path.join(socket_dir(home), channel + ".sock")


def make_server(home, channel, allowed, handler_class, server):
    cls = PipeServer if os.name == "nt" else UnixServer
    return cls(endpoint(home, channel), allowed, handler_class, server, channel)


def request(name, raw_request, timeout_ms=5000):
    if os.name == "nt":
        return pipe_request(name, raw_request, timeout_ms)
    return unix_request(name, raw_request, timeout_ms)
