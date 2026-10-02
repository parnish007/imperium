"""Isolation mode transports (SYSTEM-DESIGN §9): the local API over an OS-permissioned channel.

On Windows: named pipes whose access list (DACL) admits only the accounts named in the configuration, created with
FILE_FLAG_FIRST_PIPE_INSTANCE (a process that grabbed the name first makes the daemon refuse to start, instead of
being talked to) and PIPE_REJECT_REMOTE_CLIENTS. On Linux/macOS: Unix sockets in a directory only the allowed users
can reach. Every connection is identified by the operating system: the account of the process at the other end
(Windows: GetNamedPipeClientProcessId -> process token -> user SID; Unix: SO_PEERCRED / getpeereid). The same
HTTP API runs over the channel, so nothing above the transport changes.

Two channels: `owner` (the owner and the director, who run as the owner's account) and `builder` (builders, under
their own account). A builder token is accepted only on the builder channel, and every other token only on the
owner channel; the peer account must be one the channel admits.
"""
import io
import os
import socket
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
    pid = wt.ULONG()
    if not k32.GetNamedPipeClientProcessId(handle, ctypes.byref(pid)):
        raise TransportError("cannot identify the process at the other end of the pipe")
    hp = k32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not hp:
        raise TransportError(f"cannot open the peer process {pid.value} ({ctypes.get_last_error()})")
    try:
        return _process_sid(hp)
    finally:
        k32.CloseHandle(hp)


def pipe_name(home, channel):
    """One name per runtime directory and channel, so two installations never share a pipe."""
    import hashlib
    tag = hashlib.sha256(os.path.abspath(home).lower().encode()).hexdigest()[:12]
    return rf"\\.\pipe\imperium-{tag}-{channel}"


def sddl(allowed_sids):
    """Protected DACL: full access for SYSTEM, the daemon's own account, and the allowed accounts; nobody else."""
    me = current_identity()
    sids = []
    for s in [me] + list(allowed_sids):
        if s not in sids:
            sids.append(s)
    return "D:P(A;;GA;;;SY)" + "".join(f"(A;;GA;;;{s})" for s in sids)


class _PipeIO(io.RawIOBase):
    def __init__(self, handle):
        self.h = handle

    def readable(self):
        return True

    def writable(self):
        return True

    def readinto(self, b):
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

    def __init__(self, handle):
        self.h = handle
        self.raw = _PipeIO(handle)

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
        try:
            try:
                sid = peer_sid(h)
            except TransportError:
                return
            if sid not in self.allowed:
                return
            self.handler_class(_PipeSock(h), ("pipe", self.channel, sid), self.server)
        except Exception:  # one bad client must not stop the server
            pass
        finally:
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
    h = k32.CreateFileW(name, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
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
