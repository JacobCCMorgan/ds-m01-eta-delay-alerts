"""Own one temporary PostgreSQL 17 cluster for native integration checks.

Only a fresh directory outside the checkout is accepted. Ambient PostgreSQL
connection settings are ignored; this module never accepts an existing DSN or
controls a system service. Connection credentials are private and ephemeral.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import socket
import subprocess


TOOLS = ('postgres', 'initdb', 'pg_ctl', 'psql')
ROLE = 'p10_validation'


def _clock():
    return datetime.now(timezone.utc).isoformat()


def _environment():
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('PG', 'POSTGRES')) and k != 'DATABASE_URL'}
    env.update(LC_ALL='C', LANG='C', TZ='UTC')
    return env


def _runtime(pg_bin, sharedir):
    supplied = pg_bin or os.environ.get('P10_PG_BIN')
    if supplied is None:
        executable = shutil.which('postgres')
        if executable is None:
            raise ValueError('PostgreSQL 17 is required; set P10_PG_BIN to its bin directory')
        supplied = Path(executable).parent
    directory = Path(supplied).expanduser().resolve()
    versions, hashes = {}, {}
    for name in TOOLS:
        path = directory / name
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError('Missing PostgreSQL executable: ' + name)
        output = subprocess.check_output([str(path), '--version'], text=True,
                                         env=_environment(), timeout=15).strip()
        match = re.search(r'\(PostgreSQL\) (\d+)\.(\d+)(?:\D|$)', output)
        if match is None or int(match.group(1)) != 17:
            raise ValueError('The native verification runtime requires PostgreSQL 17.x')
        versions[name] = match.group(1) + '.' + match.group(2)
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if len(set(versions.values())) != 1:
        raise ValueError('PostgreSQL server and client executable versions differ')
    share = sharedir or os.environ.get('P10_PG_SHAREDIR')
    if share is not None:
        share = Path(share).expanduser().resolve()
        if not (share / 'postgres.bki').is_file():
            raise ValueError('P10_PG_SHAREDIR must contain PostgreSQL bootstrap data')
    return directory, share, versions['postgres'], hashes


def _port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


class Cluster:
    """Private connection plus sanitized metadata, updated after context exit."""
    def __init__(self, directory, metadata):
        self.directory = directory
        self.metadata = metadata
        self._dsn = None

    @property
    def dsn(self):
        if self._dsn is None:
            raise RuntimeError('The owned PostgreSQL cluster is not running')
        return self._dsn

    def __repr__(self):
        return '<P10 owned PostgreSQL cluster: ' + self.metadata['status'] + '>'


def _logged(directory, name, args, env, password, input=None, check=True, timeout=45):
    """Log command output, never the connection string or environment values."""
    try:
        result = subprocess.run(args, input=input, text=True, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        output = result.stdout
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ''
        if isinstance(output, bytes):
            output = output.decode('utf-8', errors='replace')
        (directory / name).write_text(output.replace(password, '[redacted]').replace(str(directory), '<P10_CLUSTER>'))
        raise RuntimeError('PostgreSQL command timed out; retained log: ' + name) from exc
    (directory / name).write_text(output.replace(password, '[redacted]').replace(str(directory), '<P10_CLUSTER>'))
    if check and result.returncode != 0:
        raise RuntimeError('PostgreSQL command failed; retained log: ' + name)
    return result


def _owned_pid(data):
    file = data / 'postmaster.pid'
    if not file.exists():
        return None
    lines = file.read_text().splitlines()
    if len(lines) < 2 or not lines[0].isdigit() or int(lines[0]) <= 0 or Path(lines[1]).resolve() != data:
        raise ValueError('PostgreSQL process ownership does not match the private data directory')
    return int(lines[0])


@contextmanager
def ownedcluster(output_external_dir, pg_bin=None, sharedir=None):
    """Yield a private cluster; preserve logs and stop its exact process on exit.

    ``output_external_dir`` must be a fresh directory outside this checkout.
    Use ``cluster.dsn`` only inside the context. ``cluster.metadata`` remains
    available afterward and is also written to ``lifecycle.json``. Failure logs
    and database files are retained for diagnosis; no existing files are removed.
    """
    requested = Path(output_external_dir).expanduser()
    directory = requested.resolve()
    checkout = Path(__file__).resolve().parents[2]
    if requested.exists() or requested.is_symlink():
        raise ValueError('Cluster directory already exists; preserve it and choose a fresh directory')
    if directory.is_relative_to(checkout) or checkout.is_relative_to(directory):
        raise ValueError('The private cluster must be outside the checkout')
    bins, share, version, hashes = _runtime(pg_bin, sharedir)
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    os.chmod(directory, 0o700)
    data, password_file = directory / 'data', directory / 'bootstrap-password'
    password = secrets.token_urlsafe(32)
    metadata = {'schema_version': 1, 'status': 'initializing', 'engine': 'PostgreSQL',
                'server_version': version, 'server_major': 17, 'binary_sha256': hashes,
                'created_at_utc': _clock(), 'started_at_utc': None, 'ready_at_utc': None, 'stopped_at_utc': None,
                'authentication': 'scram-sha-256', 'ambient_connection_environment_ignored': True,
                'private_cluster_only': True, 'external_service_touched': False,
                'readiness_verified': False, 'owned_data_directory_confirmed': False,
                'graceful_shutdown': False, 'listener_closed': False,
                'credentials_retained': False, 'scope': 'Temporary local native PostgreSQL integration; no operational service acceptance.'}
    cluster = Cluster(directory, metadata)
    env = _environment()
    start_attempted = entered = False
    failure = None
    port = None
    try:
        with os.fdopen(os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
            stream.write(password + '\n')
        args = [str(bins / 'initdb'), '-D', str(data), '-U', ROLE, '--no-locale', '--encoding=UTF8',
                '--auth-local=reject', '--auth-host=scram-sha-256', '--pwfile=' + str(password_file)]
        if share is not None:
            args += ['-L', str(share)]
        try:
            _logged(directory, 'initdb.log', args, env, password)
        finally:
            password_file.unlink(missing_ok=True)
        port = _port()
        metadata['endpoint'] = {'host': '127.0.0.1', 'port': port}
        options = shlex.join(['-h', '127.0.0.1', '-p', str(port), '-c', 'unix_socket_directories=',
                              '-c', 'shared_buffers=16MB', '-c', 'max_connections=20', '-c', 'timezone=UTC'])
        start_attempted = True
        metadata['started_at_utc'] = _clock()
        _logged(directory, 'start.log', [str(bins / 'pg_ctl'), '-D', str(data), '-l', str(directory / 'server.log'),
                '-o', options, '-w', '-t', '30', 'start'], env, password)
        pid = _owned_pid(data)
        if pid is None:
            raise ValueError('PostgreSQL did not retain its owned process identity')
        metadata['postmaster_pid'] = pid
        readiness_env = dict(env, PGPASSWORD=password, PGCONNECT_TIMEOUT='5')
        sql = """SELECT json_build_object('server_version_num',current_setting('server_version_num')::integer,
            'data_directory',current_setting('data_directory'),'listen_addresses',current_setting('listen_addresses'),
            'unix_socket_directories',current_setting('unix_socket_directories'),'port',inet_server_port(),
            'encoding',current_setting('server_encoding'),'timezone',current_setting('TimeZone'),
            'database',current_database(),'role',current_user);"""
        result = _logged(directory, 'readiness.log', [str(bins / 'psql'), '-X', '-q', '-A', '-t', '-v', 'ON_ERROR_STOP=1',
                         '-h', '127.0.0.1', '-p', str(port), '-U', ROLE, '-d', 'postgres'], readiness_env, password, input=sql)
        observed = json.loads(result.stdout.strip())
        expected_version = 170000 + int(version.split('.')[1])
        if (observed['server_version_num'] != expected_version or Path(observed['data_directory']).resolve() != data
                or observed['listen_addresses'] != '127.0.0.1' or observed['unix_socket_directories'] != ''
                or observed['port'] != port or observed['encoding'] != 'UTF8' or observed['timezone'] != 'UTC'
                or observed['database'] != 'postgres' or observed['role'] != ROLE):
            raise ValueError('Native PostgreSQL readiness differs from the owned runtime')
        metadata.update(status='running', readiness_verified=True, owned_data_directory_confirmed=True,
                        server_version_num=observed['server_version_num'], ready_at_utc=_clock())
        # Pin the endpoint and session defaults. The verifier CLI additionally
        # strips ambient libpq settings such as PGSERVICE/PGSERVICEFILE, which
        # cannot be disabled by an empty service parameter in a connection DSN.
        cluster._dsn = (f'host=127.0.0.1 hostaddr=127.0.0.1 port={port} dbname=postgres user={ROLE} '
                        f'password={password} connect_timeout=5 sslmode=disable '
                        "options='-c timezone=UTC' target_session_attrs=any channel_binding=disable "
                        'application_name=p10_owned_verification')
        entered = True
        yield cluster
    except BaseException as exc:
        failure = exc
        metadata.update(status='failed_body' if entered else 'failed_start', failure_type=type(exc).__name__)
        raise
    finally:
        cleanup_error = None
        try:
            password_file.unlink(missing_ok=True)
            pid = _owned_pid(data) if start_attempted else None
            if pid is not None:
                _logged(directory, 'stop.log', [str(bins / 'pg_ctl'), '-D', str(data), '-m', 'fast', '-w', '-t', '30', 'stop'], env, password)
                if (data / 'postmaster.pid').exists():
                    raise RuntimeError('Owned PostgreSQL process did not shut down')
                metadata['graceful_shutdown'] = True
            elif entered:
                raise RuntimeError('Owned PostgreSQL process disappeared before controlled shutdown')
            if entered or pid is not None:
                with socket.socket() as client:
                    client.settimeout(1)
                    metadata['listener_closed'] = client.connect_ex(('127.0.0.1', port)) != 0
                if not metadata['listener_closed']:
                    raise RuntimeError('The owned PostgreSQL endpoint remains reachable after shutdown')
            metadata['stopped_at_utc'] = _clock()
            if failure is None:
                metadata['status'] = 'passed_owned_cluster_lifecycle'
        except BaseException as exc:
            cleanup_error = exc
            metadata.update(status='failed_cleanup', cleanup_failure_type=type(exc).__name__)
        finally:
            cluster._dsn = None
            log = directory / 'server.log'
            if log.exists():
                log.write_text(log.read_text(errors='replace').replace(password, '[redacted]').replace(str(directory), '<P10_CLUSTER>'))
            metadata['log_sha256'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.glob('*.log'))}
            (directory / 'lifecycle.json').write_text(json.dumps(metadata, sort_keys=True, indent=2) + '\n')
        if cleanup_error is not None:
            raise RuntimeError('Native PostgreSQL cleanup failed; inspect the retained lifecycle and logs') from cleanup_error
