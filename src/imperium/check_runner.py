"""Explicit verification backends. Docker fails closed; it never falls back to host execution.

Only disposable snapshot and runner directories are mounted, both read-only. Candidate writes go to bounded
tmpfs. The runtime, credentials, live workspace, Docker socket, host PID namespace and network are not exposed.
The Docker daemon and owner-supplied image remain trusted; this is not a VM boundary against kernel exploits.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile

from . import verify

PIN = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9._:/-]*@)?sha256:[0-9a-f]{64}\Z")


def failure(message, backend):
    return {"exit_code": None, "timed_out": False, "duration": 0.0, "output_sha256": None,
            "output_path": None, "executable": None, "executable_sha256": None, "env_names": [],
            "execution": {"backend": backend, "error": message}}


def docker_command(executable, image, name, snapshot, runner, cfg):
    # --mount uses CSV syntax. Refuse unusual host paths rather than interpreting mount options from a name.
    if any(c in str(p) for p in (snapshot, runner) for c in (',', '\n', '\r')):
        raise verify.VerifyError("verification staging paths cannot contain commas or line breaks")
    return [executable, 'create', '--name', name, '--pull=never', '--network=none', '--read-only',
            '--user=65534:65534', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--init',
            '--no-healthcheck', '--log-driver=none', f"--pids-limit={cfg['pids_limit']}",
            f"--memory={cfg['memory_mb']}m", f"--memory-swap={cfg['memory_mb']}m", f"--cpus={cfg['cpus']}",
            '--tmpfs', f"/work:rw,nosuid,nodev,size={cfg['writable_mb']}m,mode=1777",
            '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777',
            '--mount', f'type=bind,src={snapshot},dst=/input,readonly',
            '--mount', f'type=bind,src={runner},dst=/runner,readonly',
            '--workdir=/tmp', '--entrypoint=python3', image, '-I', '/runner/bootstrap.py']


def run(cfg, isolation, argv, snapshot, cwd, env_names, timeout, out_path, depends, redactions=(), cancel=None):
    backend = cfg['backend']
    if backend == 'unsafe-local':
        if isolation:
            return failure('unsafe-local is forbidden in isolation mode', backend)
        return verify.run_check(argv, cwd, env_names, timeout, out_path, redactions, cancel=cancel)
    if backend != 'docker':
        return failure('unknown verification backend', backend)
    if not PIN.fullmatch(cfg['image']):
        return failure('configure verification.image with a locally installed sha256-pinned Linux image', backend)
    executable = shutil.which('docker')
    if not executable:
        return failure('Docker is not installed; host execution is disabled', backend)
    if set(env_names) - set(cfg['env_allowlist']):
        return failure('requested environment names are not in verification.env_allowlist', backend)
    # Use the same owner-controlled Docker endpoint/config for every lifecycle operation. These variables
    # configure only the trusted host client; only payload.env is injected into the candidate container.
    client_env = dict(os.environ)
    try:
        # Inspect once and run that immutable image ID, never a mutable tag. No network pull is performed.
        inspected = subprocess.run([executable, 'image', 'inspect', cfg['image']], capture_output=True,
                                   timeout=15, check=True, env=client_env)
        info = json.loads(inspected.stdout)[0]
        image = info['Id']
        if info.get('Os') != 'linux' or not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
            return failure('verification requires a Linux image with an immutable image ID', backend)
        # Image-declared volumes would add uncontrolled writable mounts.
        if (info.get('Config') or {}).get('Volumes'):
            return failure('verification image must not declare VOLUME mounts', backend)
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
        return failure('cannot inspect the pinned local verification image', backend)
    name = 'imperium-check-' + secrets.token_hex(16)
    with tempfile.TemporaryDirectory(prefix='imperium-runner-') as runner:
        os.chmod(runner, 0o755)
        shutil.copyfile(Path(__file__).with_name('check_bootstrap.py'), Path(runner)/'bootstrap.py')
        mapped = {}
        external = [p for p in depends if os.path.isabs(p)]
        # Preserve relative paths on each host drive, mounting only explicitly trusted bytes.
        roots = {}
        for path in external:
            drive = os.path.splitdrive(path)[0]
            parents = [os.path.dirname(p) for p in external if os.path.splitdrive(p)[0] == drive]
            roots[drive] = os.path.commonpath(parents)
        for index, path in enumerate(external):
            drive = os.path.splitdrive(path)[0]
            group = list(roots).index(drive)
            dest = Path(runner)/'heldout'/str(group)/os.path.relpath(path, roots[drive])
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                data = Path(path).read_bytes()
            except OSError:
                return failure('held-out dependency cannot be read', backend)
            if 'sha256:' + hashlib.sha256(data).hexdigest() != depends[path]:
                return failure('held-out dependency changed before execution', backend)
            dest.write_bytes(data)
            dest.chmod(0o444)
            mapped[path] = '/runner/' + dest.relative_to(runner).as_posix()
        payload = {'argv': [mapped.get(a, a) for a in argv],
                   'working_dir': Path(os.path.relpath(cwd, snapshot)).as_posix(),
                   'env': {k: os.environ[k] for k in env_names if k in os.environ}}
        (Path(runner)/'payload.json').write_text(json.dumps(payload), encoding='utf-8')
        for root, _, files in os.walk(runner):
            os.chmod(root, 0o755)
            for file in files:
                (Path(root)/file).chmod(0o444)
        # Snapshot data is disposable, never the live workspace. The unprivileged container user needs read
        # access; bind mounts bypass parent host-directory permissions once mounted by Docker.
        for root, dirs, files in os.walk(snapshot):
            os.chmod(root, 0o755)
            for file in files:
                p = Path(root)/file
                p.chmod(0o755 if p.stat().st_mode & 0o100 else 0o644)
        result = failure('container execution interrupted', backend)
        try:
            command = docker_command(executable, image, name, os.path.abspath(snapshot), runner, cfg)
            # Creating cannot execute candidate code. Starting only after creation has completed avoids a
            # timed-out `docker run` creating a running orphan after cleanup has already checked for it.
            subprocess.run(command, capture_output=True, timeout=15, check=True, env=client_env)
            result = verify.run_check([executable, 'start', '--attach', name], runner, [], timeout, out_path,
                                      redactions, cancel=cancel, environment=client_env)
            error = result['execution']['error']
            if result['exit_code'] in (125, 126, 127):
                error = error or 'container_or_command_setup_failed'
            # Inspect the container as well: an OOM kill is an infrastructure failure, never a test assertion.
            state = subprocess.run([executable, 'inspect', '--format={{json .State}}', name],
                                   capture_output=True, timeout=15, check=True, env=client_env)
            state = json.loads(state.stdout)
            if state.get('OOMKilled') or state.get('Error') or state.get('Running'):
                error = error or 'container_execution_failed'
            result['execution'] = {'backend': backend, 'image': image, 'error': error,
                                   'output_bytes': result['execution'].get('output_bytes'),
                                   'argv': payload['argv'], 'working_dir': payload['working_dir'],
                                   'docker_client_sha256': result['executable_sha256']}
            result['executable'], result['executable_sha256'] = payload['argv'][0], None
            result['env_names'] = sorted((k, hashlib.sha256(v.encode()).hexdigest()[:12])
                                         for k, v in payload['env'].items())
        except (OSError, ValueError, subprocess.SubprocessError, verify.VerifyError) as e:
            result = failure(f'container runner failed: {type(e).__name__}', backend)
        finally:
            try:
                removed = subprocess.run([executable, 'rm', '-f', name], capture_output=True, timeout=15,
                                         env=client_env)
                if removed.returncode and b'No such container' not in removed.stderr:
                    result['execution']['error'] = 'container_cleanup_failed'
            except (OSError, subprocess.SubprocessError):
                result['execution']['error'] = 'container_cleanup_failed'
        return result
