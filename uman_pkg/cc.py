# SPDX-License-Identifier: GPL-2.0+
# Copyright 2026 Canonical Ltd
# Written by Simon Glass <simon.glass@canonical.com>

"""Claude Code container management

This module handles the 'claude-code' subcommand which creates and manages LXC
containers for running Claude Code.
"""

import os
import random
import socket as socket_mod
import string
import subprocess
import threading
import time

# pylint: disable=import-error
from u_boot_pylib import tools
from u_boot_pylib import tout

from uman_pkg import settings
from uman_pkg.util import exec_cmd


# Container home directory
UBUNTU_HOME = '/home/ubuntu'

# Project mount point inside the container
PROJECT_DEST = f'{UBUNTU_HOME}/project'

# Socket filename for editor proxy (in project directory)
EDITOR_SOCK = '.uman-editor.sock'

# Fixed mount point for PulseAudio socket inside the container
PULSE_DEST = '/tmp/pulse-native'

# ALSA config that routes through PulseAudio (suppresses hardware errors)
ALSA_PULSE_CONF = '/etc/alsa-pulse.conf'

# Default packages to install in containers
DEFAULT_PACKAGES = ('build-essential gdb-multiarch gh glab'
                     ' libasound2-plugins libsox-fmt-pulse pylint sox xclip')


def get_log_path(name):
    """Construct a timestamped log file path for a container session

    Creates the directory structure if it doesn't exist.

    Args:
        name (str): Container name

    Returns:
        str: Path like ~/files/dev/uman-logs/<name>/<year>/<mon>/log-...log
    """
    now = time.localtime()
    log_dir = os.path.expanduser(
        f'~/files/dev/uman-logs/{name}/{now.tm_year}'
        f'/{time.strftime("%b", now)}')
    os.makedirs(log_dir, exist_ok=True)
    fname = time.strftime('log-%y.%m%b.%d-%H%M%S.log', now).lower()
    return os.path.join(log_dir, fname)


def get_uman_dir():
    """Get the uman installation directory

    Returns:
        str: Absolute path to the uman package parent directory
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_essential_mounts(project_src):
    """Get the list of hardcoded essential mounts

    Args:
        project_src (str): Absolute path to the project source directory

    Returns:
        list of tuple: (name, source, dest) triples
    """
    home = os.path.expanduser('~')
    uman_dir = get_uman_dir()
    mounts = [
        ('datadir', project_src, PROJECT_DEST),
        ('claudejson', os.path.join(home, '.claude.json'),
         f'{UBUNTU_HOME}/.claude.json'),
        ('claudedir', os.path.join(home, '.claude'),
         f'{UBUNTU_HOME}/.claude'),
        ('hostbin', os.path.join(home, 'bin'), f'{UBUNTU_HOME}/bin'),
        ('uman', uman_dir, uman_dir),
        ('uboottools', os.path.realpath(os.path.expanduser(
            os.environ.get('UBOOT_TOOLS', '~/u/tools'))),
         f'{UBUNTU_HOME}/u/tools'),
    ]
    patman_dir = os.path.join(home, 'dev', 'patman')
    if os.path.isdir(patman_dir):
        mounts.append(('patman', patman_dir, f'{UBUNTU_HOME}/dev/patman'))

    # X11 socket for clipboard access (image paste in Claude Code)
    x11_dir = '/tmp/.X11-unix'
    if os.path.isdir(x11_dir):
        mounts.append(('x11', x11_dir, x11_dir))

    # PulseAudio socket for voice input (/voice in Claude Code)
    pulse_sock = f'/run/user/{os.getuid()}/pulse/native'
    if os.path.exists(pulse_sock):
        mounts.append(('pulse', pulse_sock, PULSE_DEST))

    for fname, mname in [('.gitconfig', 'gitconfig'),
                          ('.buildman', 'buildman'),
                          ('.buildman-toolchains', 'toolchains')]:
        path = os.path.join(home, fname)
        if os.path.exists(path):
            mounts.append((mname, path, f'{UBUNTU_HOME}/{fname}'))
    return mounts


def get_config_mounts():
    """Parse [claude-code] mounts from ~/.uman config

    Mount format: name:source:dest (one per line in the mounts value)

    Returns:
        list of tuple: (name, source, dest) triples
    """
    cfg = settings.get_all()
    if not cfg.has_section('claude-code'):
        return []

    raw = cfg.get('claude-code', 'mounts', fallback='')
    mounts = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(':')
        if len(parts) != 3:
            tout.warning(f'Ignoring malformed mount: {line}')
            continue
        name, source, dest = parts
        source = os.path.expandvars(os.path.expanduser(source))
        mounts.append((name, source, dest))
    return mounts


def get_cli_mounts(mount_args):
    """Parse -m/--mount command-line arguments into mount triples

    Supports HOST:DEST or just HOST (mounted at the same path).

    Args:
        mount_args (list of str): Mount arguments from command line

    Returns:
        list of tuple: (name, src, dst) triples
    """
    if not mount_args:
        return []

    home = os.path.expanduser('~')
    mounts = []
    for i, arg in enumerate(mount_args):
        parts = arg.split(':')
        if len(parts) == 2:
            src, dst = parts
        elif len(parts) == 1:
            src = dst = parts[0]
        else:
            tout.warning(f'Ignoring malformed mount: {arg}')
            continue
        src = os.path.expandvars(os.path.expanduser(src))
        src = os.path.realpath(src)
        # Expand ~ to the container home, not the host home
        dst = os.path.expandvars(dst)
        if dst.startswith('~'):
            dst = UBUNTU_HOME + dst[1:]
        elif dst.startswith(home):
            dst = UBUNTU_HOME + dst[len(home):]
        # Use the leaf directory as the device name, with a suffix if needed
        leaf = os.path.basename(dst) or f'cli{i}'
        name = leaf
        suffix = 2
        while any(m[0] == name for m in mounts):
            name = f'{leaf}{suffix}'
            suffix += 1
        mounts.append((name, src, dst))
    return mounts


def get_git_symlink_mount(project_src):
    """Handle .git symlink by creating a mount for the real target

    If .git is a symlink, the container needs access to the real target
    so that git operations work correctly.

    Args:
        project_src (str): Absolute path to the project source directory

    Returns:
        tuple or None: (name, source, dest) if .git is a symlink, else None
    """
    git_path = os.path.join(project_src, '.git')
    if not os.path.islink(git_path):
        return None

    git_link = os.readlink(git_path)
    git_real = os.path.realpath(git_path)

    # Resolve the same relative symlink from the container mount point
    git_container = os.path.normpath(
        os.path.join(PROJECT_DEST, git_link))
    return ('dotgit', git_real, git_container)


def gen_name(base):
    """Generate a random container name

    Args:
        base (str): Ubuntu base image name (e.g. 'noble')

    Returns:
        str: Container name like 'ubuntu-noble-a1b2'
    """
    suffix = ''.join(random.choices(string.hexdigits[:16], k=4))
    return f'ubuntu-{base}-{suffix}'


def container_exists(name):
    """Check whether an LXC container exists

    Args:
        name (str): Container name

    Returns:
        bool: True if the container exists
    """
    result = exec_cmd(['lxc', 'info', name], dry_run=False)
    return result is not None and result.return_code == 0


def container_status(name):
    """Get the status of an LXC container

    Args:
        name (str): Container name

    Returns:
        str or None: Status string (e.g. 'RUNNING', 'STOPPED') or None
    """
    result = exec_cmd(['lxc', 'info', name], dry_run=False)
    if not result or result.return_code:
        return None
    for line in result.stdout.splitlines():
        if line.startswith('Status: '):
            return line.split(': ', 1)[1].strip().upper()
    return None


def lxc(*args, dry_run=False):
    """Run an lxc command

    Args:
        *args: Arguments to pass to lxc
        dry_run (bool): If True, just show command

    Returns:
        CommandResult or None
    """
    return exec_cmd(['lxc'] + list(args), dry_run)


def lxc_exec(name, cmd, dry_run=False, user=None):
    """Run a command inside the container

    Args:
        name (str): Container name
        cmd (str): Shell command to execute
        dry_run (bool): If True, just show command
        user (str or None): User to run as (default: root)

    Returns:
        CommandResult or None
    """
    lxc_cmd = ['lxc', 'exec', name, '--']
    if user:
        lxc_cmd += ['sudo', '-u', user, 'bash', '-c', cmd]
    else:
        lxc_cmd += ['bash', '-c', cmd]
    return exec_cmd(lxc_cmd, dry_run)


def create_container(name, base, dry_run=False):
    """Create a new LXC container with uid/gid mapping

    Args:
        name (str): Container name
        base (str): Ubuntu base image name
        dry_run (bool): If True, just show commands
    """
    lxc('init', '-q', f'ubuntu:{base}', name, dry_run=dry_run)

    uid = str(os.getuid())
    gid = str(os.getgid())
    idmap = f'uid {uid} 1000\ngid {gid} 1000'
    if dry_run:
        tout.notice(
            f'printf {idmap!r} | lxc config set -q {name} raw.idmap -')
        return
    import subprocess  # pylint: disable=import-outside-toplevel
    proc = subprocess.run(
        ['lxc', 'config', 'set', '-q', name, 'raw.idmap', '-'],
        input=idmap.encode(), check=False, capture_output=True)
    if proc.returncode:
        tout.error(f'Failed to set idmap: '
                   f'{proc.stderr.decode("utf-8", errors="replace")}')


def is_privileged(name):
    """Check whether a container has privileged mode enabled

    Args:
        name (str): Container name

    Returns:
        bool: True if security.privileged is set to true
    """
    result = exec_cmd(
        ['lxc', 'config', 'get', name, 'security.privileged'],
        dry_run=False)
    return result is not None and result.stdout.strip() == 'true'


def has_mount(name, mount_name):
    """Check whether a container already has a named device

    Args:
        name (str): Container name
        mount_name (str): Device name

    Returns:
        bool: True if the device exists
    """
    result = exec_cmd(
        ['lxc', 'config', 'device', 'get', name, mount_name, 'source'],
        dry_run=False)
    return result is not None and result.return_code == 0


def add_mount(name, mount_name, source, path, dry_run=False, shift=False):
    """Add a disk device to the container if not already present

    Args:
        name (str): Container name
        mount_name (str): Device name
        source (str): Host path
        path (str): Container path
        dry_run (bool): If True, just show command
        shift (bool): If True, use idmapped mount for uid/gid
            translation so container root can access host-owned files

    Returns:
        bool: True on success (including no-op when already mounted),
            False if the source path is missing or the lxc command failed
    """
    if not dry_run and has_mount(name, mount_name):
        if shift:
            lxc('config', 'device', 'set', name, mount_name,
                'shift', 'true')
        return True
    if not dry_run and not os.path.exists(source):
        tout.error(f'Source path does not exist: {source}')
        return False
    args = [f'source={source}', f'path={path}']
    if shift:
        args.append('shift=true')
    result = lxc('config', 'device', 'add', '-q', name, mount_name, 'disk',
                 *args, dry_run=dry_run)
    if result is None:
        return True  # dry_run
    if result.return_code:
        tout.error(f'Failed to add mount {mount_name!r}: '
                   f'{(result.stderr or result.stdout).strip()}')
        return False
    return True


def get_skip_path(name):
    """Get the per-container skipped-mounts file path"""
    return os.path.join(os.path.expanduser('~'), '.claude', 'cc', name,
                        'skipped-mounts')


def get_skipped_mounts(name):
    """Get the set of mount names that should not be auto-added

    Args:
        name (str): Container name

    Returns:
        set: Mount names previously removed via `cc -u`
    """
    path = get_skip_path(name)
    if not os.path.exists(path):
        return set()
    with open(path, encoding='utf-8') as f:
        return {line.strip() for line in f if line.strip()}


def mark_skipped(name, mount_name):
    """Add a mount name to the per-container skip list"""
    path = get_skip_path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    skipped = get_skipped_mounts(name)
    skipped.add(mount_name)
    with open(path, 'w', encoding='utf-8') as f:
        for mname in sorted(skipped):
            f.write(f'{mname}\n')


def unmark_skipped(name, mount_name):
    """Remove a mount name from the per-container skip list"""
    skipped = get_skipped_mounts(name)
    if mount_name not in skipped:
        return
    skipped.discard(mount_name)
    path = get_skip_path(name)
    with open(path, 'w', encoding='utf-8') as f:
        for mname in sorted(skipped):
            f.write(f'{mname}\n')


def remove_mount(name, mount_name, dry_run=False):
    """Remove a disk device from a container

    Args:
        name (str): Container name
        mount_name (str): Device name
        dry_run (bool): If True, just show command

    Returns:
        bool: True if removed successfully
    """
    if not dry_run and not has_mount(name, mount_name):
        tout.error(f'No device {mount_name!r} on container {name}')
        return False
    lxc('config', 'device', 'remove', name, mount_name, dry_run=dry_run)
    if not dry_run:
        mark_skipped(name, mount_name)
    return True


def wait_for_user(name, dry_run=False, timeout=60):
    """Wait until the ubuntu user exists in the container

    Args:
        name (str): Container name
        dry_run (bool): If True, just show command
        timeout (int): Seconds to wait before giving up

    Returns:
        bool: True if the user appeared (or dry-run), False on timeout
    """
    if dry_run:
        tout.notice('# wait for ubuntu user')
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = exec_cmd(
            ['lxc', 'exec', name, '--', 'id', '-u', 'ubuntu'],
            dry_run=False)
        if result and result.return_code == 0:
            return True
        time.sleep(0.5)
    tout.error(
        f"Timed out waiting for 'ubuntu' user in container '{name}'")
    return False


def setup_container(name, dry_run=False):
    """Set up the container after first boot

    Fix permissions, install terminfo, create host user symlink.

    Args:
        name (str): Container name
        dry_run (bool): If True, just show commands
    """
    lxc_exec(name, 'chown ubuntu:ubuntu /home/ubuntu', dry_run=dry_run)

    # Suppress the Ubuntu sudo hint message
    lxc_exec(name, 'touch /home/ubuntu/.sudo_as_admin_successful',
             dry_run=dry_run, user='ubuntu')

    # Install terminfo from host
    if not dry_run:
        import subprocess  # pylint: disable=import-outside-toplevel
        infocmp = subprocess.run(['infocmp', '-x'], capture_output=True,
                                 check=False)
        if infocmp.returncode == 0:
            tic = subprocess.run(
                ['lxc', 'exec', name, '--', 'tic', '-x', '-'],
                input=infocmp.stdout, capture_output=True, check=False)
            if tic.returncode:
                tout.warning('Could not install terminfo')
    else:
        tout.notice(f'infocmp -x | lxc exec {name} -- tic -x -')

    # Set timezone to match host
    tz_file = '/etc/timezone'
    if os.path.exists(tz_file):
        tzone = tools.read_file(tz_file, binary=False).strip()
        lxc_exec(name,
                 f'ln -sf /usr/share/zoneinfo/{tzone} /etc/localtime && '
                 f'echo {tzone} > /etc/timezone',
                 dry_run=dry_run)

    # Symlink host user home to ubuntu home
    user = os.environ.get('USER', 'ubuntu')
    lxc_exec(name,
             f'test -e /home/{user} || ln -s /home/ubuntu /home/{user}',
             dry_run=dry_run)


def install_tools(name, packages=None, dry_run=False):
    """Install build tools if not already present

    Args:
        name (str): Container name
        packages (str or None): Space-separated package names
        dry_run (bool): If True, just show command
    """
    if not packages:
        packages = DEFAULT_PACKAGES
    cmd = (f'command -v gcc >/dev/null 2>&1 || '
           f'(apt-get update -qq && apt-get install -yqq {packages})')
    lxc_exec(name, cmd, dry_run=dry_run)


def install_claude(name, dry_run=False):
    """Install Claude Code if not already present

    Args:
        name (str): Container name
        dry_run (bool): If True, just show command
    """
    cmd = ('export PATH="$HOME/.local/bin:$HOME/bin:$PATH" && '
           'command -v claude >/dev/null 2>&1 || '
           '(echo "Installing claude..." && '
           'curl -fsSL https://claude.ai/install.sh | bash)')
    lxc_exec(name, cmd, dry_run=dry_run, user='ubuntu')


def setup_uman(name, uboot_tools=None, dry_run=False):
    """Set up uman aliases and environment inside the container

    Writes ~/.uman_env with PATH, UBOOT_TOOLS, the um() wrapper and
    eval aliases, then sources it from ~/.bashrc and ~/.profile so it
    is available in interactive, login and non-interactive shells.

    Args:
        name (str): Container name
        uboot_tools (str or None): Path to U-Boot tools inside container
        dry_run (bool): If True, just show commands
    """
    if not uboot_tools:
        uboot_tools = f'{UBUNTU_HOME}/u/tools'

    # Run setup aliases into ~/.local/bin (container-local, not the
    # host-mounted ~/bin whose symlinks use host-specific paths)
    uman_dir = get_uman_dir()
    um_path = os.path.join(uman_dir, 'um')
    uman_bin = os.path.join(uman_dir, 'uman_pkg', 'uman')
    patman = f'{UBUNTU_HOME}/dev/patman/tools/patman/patman'
    lxc_exec(name,
             f'mkdir -p ~/.local/bin && '
             f'ln -sf {uman_bin} {um_path} && '
             f'ln -sf {uman_bin} ~/.local/bin/um && '
             f'test -e {patman} && ln -sf {patman} ~/.local/bin/patman '
             f'|| true',
             dry_run=dry_run, user='ubuntu')
    setup_cmd = (
        f'export PATH="$HOME/.local/bin:$HOME/bin:$PATH" && '
        f'export UBOOT_TOOLS="{uboot_tools}" && '
        f'{um_path} -q setup aliases -d ~/.local/bin -f')
    lxc_exec(name, setup_cmd, dry_run=dry_run, user='ubuntu')

    # Write editor proxy script
    editor_script = (
        '#!/usr/bin/env python3\n'
        'import json, os, socket, sys\n'
        'path = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1'
        ' else sys.exit(1)\n'
        f'sock_path = "{PROJECT_DEST}/{EDITOR_SOCK}"\n'
        's = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n'
        'try:\n'
        '    s.connect(sock_path)\n'
        'except OSError:\n'
        '    sys.exit("editor proxy not running")\n'
        f'if path.startswith("{PROJECT_DEST}/"):\n'
        '    s.sendall((path + "\\n").encode())\n'
        '    resp = s.recv(4096).decode().strip()\n'
        'else:\n'
        '    content = open(path).read() if os.path.exists(path) else ""\n'
        '    ext = os.path.splitext(path)[1]\n'
        '    msg = json.dumps({"content": content, "ext": ext}) + "\\n"\n'
        '    s.sendall(msg.encode())\n'
        '    resp_raw = b""\n'
        '    while True:\n'
        '        chunk = s.recv(65536)\n'
        '        if not chunk:\n'
        '            break\n'
        '        resp_raw += chunk\n'
        '    resp_data = json.loads(resp_raw.decode())\n'
        '    if resp_data.get("error"):\n'
        '        print(resp_data["error"], file=sys.stderr)\n'
        '        sys.exit(1)\n'
        '    open(path, "w").write(resp_data["content"])\n'
        '    resp = "done"\n'
        's.close()\n'
        'if resp != "done":\n'
        '    print(resp, file=sys.stderr)\n'
        '    sys.exit(1)\n')
    editor_path = f'{UBUNTU_HOME}/.local/bin/uman-editor'
    write_editor = (
        f"cat > {editor_path} <<'EDEOF'\n{editor_script}EDEOF\n"
        f"chmod +x {editor_path}")
    lxc_exec(name, write_editor, dry_run=dry_run, user='ubuntu')

    # Write ALSA config that routes through PulseAudio
    alsa_conf = 'pcm.!default { type pulse }\nctl.!default { type pulse }\n'
    lxc_exec(name,
             f"cat > {ALSA_PULSE_CONF} <<'ALSAEOF'\n{alsa_conf}ALSAEOF",
             dry_run=dry_run)

    # Write ~/.uman_env with the full environment block
    display = os.environ.get('DISPLAY', ':0')
    env_block = (
        '# uman setup — sourced by ~/.bashrc, ~/.profile and BASH_ENV\n'
        '[ "$_UMAN_ENV_LOADED" = 1 ] && return\n'
        '_UMAN_ENV_LOADED=1\n'
        'export PATH="$HOME/bin:$HOME/.local/bin:$PATH"\n'
        f'export UBOOT_TOOLS="{uboot_tools}"\n'
        f'export DISPLAY="{display}"\n'
        f'export EDITOR="{editor_path}"\n'
        f'export PULSE_SERVER="unix:{PULSE_DEST}"\n'
        'export AUDIODRIVER=pulseaudio\n'
        f'export ALSA_CONFIG_PATH="{ALSA_PULSE_CONF}"\n'
        'export PULSE_LATENCY_MSEC=50\n'
        'um() { b="$b" USRC="$USRC" command um "$@"; }\n'
        'eval "$(um git -a)"\n'
        'export BASH_ENV=~/.uman_env\n')

    write_cmd = f"cat > ~/.uman_env <<'ENVEOF'\n{env_block}ENVEOF"
    lxc_exec(name, write_cmd, dry_run=dry_run, user='ubuntu')

    # Source from ~/.bashrc (interactive non-login shells)
    source_line = '[ -f ~/.uman_env ] && . ~/.uman_env'
    for rcfile in ('~/.bashrc', '~/.profile'):
        add_cmd = (
            f"grep -q '.uman_env' {rcfile} 2>/dev/null || "
            f"echo '{source_line}' >> {rcfile}")
        lxc_exec(name, add_cmd, dry_run=dry_run, user='ubuntu')


def editor_listen(sock_path, project_src, host_editor, ready=None):
    """Listen for editor requests from the container

    Runs in a daemon thread. Accepts connections on a Unix socket.
    For project paths, translates to host paths and opens the editor.
    For non-project paths (e.g. /tmp), receives file content as JSON,
    writes a temp file on the host, opens the editor, and sends back
    the edited content.

    Args:
        sock_path (str): Path to the Unix socket file
        project_src (str): Host-side project directory
        host_editor (str): Host editor command
        ready (threading.Event or None): Set when socket is bound
    """
    import json
    import tempfile

    sock = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
    sock.bind(sock_path)
    # Make socket world-writable so container user can connect
    os.chmod(sock_path, 0o777)
    sock.listen(1)
    sock.settimeout(2)
    if ready:
        ready.set()
    while True:
        try:
            conn, _ = sock.accept()
        except socket_mod.timeout:
            continue
        except OSError:
            break
        try:
            data = conn.recv(4096).decode().strip()
            if not data:
                continue

            # Translate container path to host path
            if data.startswith(PROJECT_DEST + '/'):
                rel = data[len(PROJECT_DEST) + 1:]
                host_path = os.path.join(project_src, rel)
                subprocess.run([host_editor, host_path], check=False)
                conn.sendall(b'done\n')
            elif data.startswith(PROJECT_DEST):
                subprocess.run([host_editor, project_src], check=False)
                conn.sendall(b'done\n')
            elif data.startswith('{'):
                msg = json.loads(data)
                ext = msg.get('ext', '.txt')
                with tempfile.NamedTemporaryFile(
                        mode='w', suffix=ext, delete=False) as tmp:
                    tmp.write(msg['content'])
                    tmp_path = tmp.name
                try:
                    subprocess.run([host_editor, tmp_path], check=False)
                    with open(tmp_path) as fh:
                        edited = fh.read()
                    resp = json.dumps({'content': edited})
                    conn.sendall(resp.encode())
                finally:
                    os.unlink(tmp_path)
            else:
                conn.sendall(b'error: path outside project\n')
        except OSError:
            pass
        finally:
            conn.close()


def start_editor_proxy(project_src, dry_run=False):
    """Start the editor proxy listener in a background thread

    Args:
        project_src (str): Host-side project directory
        dry_run (bool): If True, just show what would happen

    Returns:
        str: Path to the socket file
    """
    sock_path = os.path.join(project_src, EDITOR_SOCK)
    if dry_run:
        tout.notice(f'# editor proxy: {sock_path}')
        return sock_path

    # Remove stale socket
    if os.path.exists(sock_path):
        os.unlink(sock_path)

    host_editor = os.environ.get('EDITOR', 'vi')
    ready = threading.Event()
    thread = threading.Thread(target=editor_listen,
                              args=(sock_path, project_src, host_editor,
                                    ready),
                              daemon=True)
    thread.start()
    ready.wait()
    return sock_path


def launch_shell(name, shell_command=None, dry_run=False, log_file=None):
    """Open an interactive shell or run a command in the container

    Args:
        name (str): Container name
        shell_command (str or None): Command to run, or None for
            interactive shell
        dry_run (bool): If True, just show command
        log_file (str or None): Path to log file for session recording
    """
    shell_cmd = shell_command or 'exec bash'
    cmd = ['lxc', 'exec', name, '--', 'sudo', '-iu', 'ubuntu',
           'bash', '-ic', f'cd {PROJECT_DEST} && {shell_cmd}']
    exec_cmd(cmd, dry_run, capture=False, log_file=log_file)


def launch_claude(name, cont=False, dry_run=False, log_file=None):
    """Launch Claude Code in the container

    Args:
        name (str): Container name
        cont (bool): If True, continue the most recent conversation
        dry_run (bool): If True, just show command
        log_file (str or None): Path to log file for session recording
    """
    flag = ' --continue' if cont else ''
    cmd = ['lxc', 'exec', name, '--', 'sudo', '-iu', 'ubuntu', 'bash', '-ic',
           f'cd {PROJECT_DEST} && claude --dangerously-skip-permissions'
           f'{flag}']
    exec_cmd(cmd, dry_run, capture=False, log_file=log_file)


def stop_container(name, dry_run=False):
    """Stop a running container

    Args:
        name (str): Container name
        dry_run (bool): If True, just show command
    """
    lxc('stop', name, dry_run=dry_run)


def delete_container(name, dry_run=False):
    """Force-delete a container

    Args:
        name (str): Container name
        dry_run (bool): If True, just show command
    """
    lxc('delete', '-f', name, dry_run=dry_run)


def rename_container(old, new, dry_run=False):
    """Rename (move) a container, stopping it first if needed

    Args:
        old (str): Current container name
        new (str): New container name
        dry_run (bool): If True, just show commands
    """
    status = container_status(old)
    if status == 'RUNNING':
        tout.notice(f'Stopping container: {old}')
        lxc('stop', old, dry_run=dry_run)
    lxc('move', old, new, dry_run=dry_run)


def get_project(name):
    """Get the project source path for a container

    Args:
        name (str): Container name

    Returns:
        str: Project source path, or '' if not found
    """
    result = exec_cmd(
        ['lxc', 'config', 'device', 'get', name, 'datadir', 'source'],
        dry_run=False)
    if result and result.return_code == 0:
        return result.stdout.strip()
    return ''


def list_containers():
    """List uman containers (those with a datadir device)

    Returns:
        list of tuple: (name, status, project, privileged) 4-tuples
    """
    result = exec_cmd(['lxc', 'list', '--format', 'csv', '-c', 'ns'],
                       dry_run=False)
    if not result or result.return_code:
        return []
    containers = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(',')
        if len(parts) >= 2:
            project = get_project(parts[0])
            if project:
                priv = is_privileged(parts[0])
                containers.append((parts[0], parts[1], project, priv))
    return containers


def add_all_mounts(name, project_src, mount_args=None, output=False,
                   no_output=False, dry_run=False):
    """Add all mounts (essential, git symlink, config, CLI) to a container

    Skips any devices that already exist or are on the per-container
    skip list (mounts the user explicitly removed via `cc -u`).

    Args:
        name (str): Container name
        project_src (str): Absolute path to the project source directory
        mount_args (list of str): Mount arguments from -m flag
        output (bool): If True, mount /tmp/b into the container
        no_output (bool): If True, remove /tmp/b mount
        dry_run (bool): If True, just show commands
    """
    skipped = get_skipped_mounts(name) if not dry_run else set()

    def maybe_add(mname, source, dest, **kw):
        if mname in skipped:
            return
        add_mount(name, mname, source, dest, dry_run, **kw)

    for mname, source, dest in get_essential_mounts(project_src):
        maybe_add(mname, source, dest)

    # Per-container projects dir so --continue is scoped correctly
    home = os.path.expanduser('~')
    proj_dir = os.path.join(home, '.claude', 'cc', name)
    os.makedirs(proj_dir, exist_ok=True)
    maybe_add('claudeproj', proj_dir, f'{UBUNTU_HOME}/.claude/projects')

    git_mount = get_git_symlink_mount(project_src)
    if git_mount:
        maybe_add(*git_mount)

    # Mount /tmp/b if requested, or remove if -O
    if output:
        tmp_dir = '/tmp/b'
        os.makedirs(tmp_dir, exist_ok=True)
        new_tmpb = not dry_run and not has_mount(name, 'tmpb')
        if not dry_run:
            unmark_skipped(name, 'tmpb')
        add_mount(name, 'tmpb', tmp_dir, '/tmp/b', dry_run)
        if new_tmpb and container_status(name) == 'RUNNING':
            tout.notice(
                f'Added /tmp/b mount; activate with: uman cc -R {name}')
    elif no_output:
        if not dry_run and has_mount(name, 'tmpb'):
            remove_mount(name, 'tmpb')
            tout.notice('Removed /tmp/b mount')

    pbuilder = '/var/cache/pbuilder'
    if os.path.isdir(pbuilder):
        maybe_add('pbuilder', pbuilder, pbuilder, shift=True)

    for mname, source, dest in get_config_mounts():
        maybe_add(mname, source, dest)

    # Explicit -m args override the skip list (re-mounting clears it)
    for mname, source, dest in get_cli_mounts(mount_args):
        if not dry_run:
            unmark_skipped(name, mname)
        add_mount(name, mname, source, dest, dry_run)


def ensure_running(name, existed, dry_run=False):
    """Start the container if it is not already running

    Args:
        name (str): Container name
        existed (bool): Whether the container existed before this run
        dry_run (bool): If True, just show commands

    Returns:
        bool: True if the container is (or would be) running, False if
            'lxc start' failed
    """
    if dry_run:
        lxc('start', name, dry_run=dry_run)
        return True

    if not existed:
        result = lxc('start', name)
    else:
        status = container_status(name)
        if status == 'RUNNING':
            return True
        tout.notice(f'Starting container (was {status})')
        result = lxc('start', name)

    if result and result.return_code:
        msg = (result.stderr or result.stdout or '').strip()
        tout.error(f"Failed to start container '{name}': {msg}")
        return False
    return True


def show_containers():
    """List uman containers with their project paths

    Returns:
        int: Exit code
    """
    containers = list_containers()
    if not containers:
        tout.notice('No uman containers found')
    else:
        home = os.path.expanduser('~')
        for cname, status, project, priv in containers:
            if project.startswith(home):
                project = '~' + project[len(home):]
            flags = ' [privileged]' if priv else ''
            tout.notice(f'{cname}  {status:8s}  {project}{flags}')
    return 0


def show_mounts(name):
    """List mounts for a container

    Args:
        name (str): Container name

    Returns:
        int: Exit code
    """
    result = exec_cmd(['lxc', 'config', 'device', 'show', name],
                       dry_run=False)
    if not result or result.return_code:
        tout.error(f'Container not found: {name}')
        return 1

    home = os.path.expanduser('~')
    mounts = []
    cur_name = None
    source = path = None
    for line in result.stdout.splitlines():
        if not line.startswith(' '):
            if cur_name and source and path:
                mounts.append((cur_name, source, path))
            cur_name = line.rstrip(':')
            source = path = None
        elif 'source:' in line:
            source = line.split(':', 1)[1].strip()
        elif 'path:' in line:
            path = line.split(':', 1)[1].strip()
    if cur_name and source and path:
        mounts.append((cur_name, source, path))

    if not mounts:
        tout.notice(f'No mounts for {name}')
        return 0

    for mname, source, path in mounts:
        if source.startswith(home):
            source = '~' + source[len(home):]
        print(f'  {mname:14s} {source} -> {path}')
    return 0


def run(args):  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    """Main entry point for the cc subcommand

    Creates a container, sets it up, and launches Claude Code or a shell.
    Ephemeral containers are deleted on exit (including Ctrl+C).

    Args:
        args (argparse.Namespace): Parsed arguments

    Returns:
        int: Exit code
    """
    if args.list_containers:
        return show_containers()

    if args.mounts:
        name = args.name or os.path.basename(os.path.realpath(os.getcwd()))
        return show_mounts(name)

    if args.mount and not args.shell:
        name = args.name or os.path.basename(os.path.realpath(os.getcwd()))
        if not args.dry_run and not container_exists(name):
            tout.error(f'Container not found: {name}')
            return 1
        rc = 0
        for mname, source, dest in get_cli_mounts(args.mount):
            if add_mount(name, mname, source, dest, args.dry_run):
                tout.notice(f'Mounted {source} -> {dest} ({mname})')
            else:
                rc = 1
        return rc

    if args.unmount:
        name = args.name or os.path.basename(os.path.realpath(os.getcwd()))
        if not args.dry_run and not container_exists(name):
            tout.error(f'Container not found: {name}')
            return 1
        return 0 if remove_mount(name, args.unmount, args.dry_run) else 1

    if args.delete:
        if not args.name:
            tout.error('Container name required for --delete')
            return 1
        delete_container(args.name, args.dry_run)
        return 0

    if args.rename:
        if not args.name:
            tout.error('Container name required for --rename')
            return 1
        rename_container(args.name, args.rename, args.dry_run)
        return 0

    if args.stop:
        if not args.name:
            tout.error('Container name required for --stop')
            return 1
        stop_container(args.name, args.dry_run)
        return 0

    dry_run = args.dry_run

    # Get config values
    cfg = settings.get_all()
    packages = None
    uboot_tools = None
    if cfg.has_section('claude-code'):
        packages = cfg.get('claude-code', 'packages', fallback=None)
        uboot_tools = cfg.get('claude-code', 'uboot_tools', fallback=None)

    # Use config base if user didn't override on command line
    base = args.base
    if base == 'noble' and cfg.has_section('claude-code'):
        base = cfg.get('claude-code', 'base', fallback=base)

    project_src = os.path.realpath(os.getcwd())

    # Ephemeral gets a random name; otherwise use explicit or dir name
    if args.ephemeral:
        name = gen_name(base)
        keep = False
    else:
        name = args.name or os.path.basename(project_src)
        keep = True

    # Check if container already exists
    existed = not dry_run and container_exists(name)
    if existed:
        tout.notice(f'Reusing container: {name}')
    else:
        tout.notice(f'Container: {name}')

    sock_path = os.path.join(project_src, EDITOR_SOCK)
    try:
        if not existed:
            tout.progress('Creating container')
            create_container(name, base, dry_run)

        tout.progress('Setting up mounts')
        add_all_mounts(name, project_src, args.mount, args.output,
                       args.no_output, dry_run)

        if args.restart and existed:
            status = container_status(name)
            if not dry_run and status == 'RUNNING':
                tout.notice('Stopping container for restart')
                lxc('stop', name)
            existed = False

        if args.privileged:
            lxc('config', 'set', '-q', name, 'security.privileged=true',
                dry_run=dry_run)
            lxc('config', 'set', '-q', name, 'raw.idmap=',
                dry_run=dry_run)
            raw_lxc = ('lxc.apparmor.profile=unconfined\n'
                       'lxc.seccomp.profile=')
            lxc('config', 'set', '-q', name, 'raw.lxc', raw_lxc,
                dry_run=dry_run)
            lxc('config', 'set', '-q', name,
                'security.nesting=true', dry_run=dry_run)
            tout.notice('Enabled privileged mode')
            if existed and not dry_run:
                status = container_status(name)
                if status == 'RUNNING':
                    tout.notice(
                        f'Restart needed: um cc -R {name}')
                    return 0
        elif args.no_privileged:
            uid = str(os.getuid())
            gid = str(os.getgid())
            idmap = f'uid {uid} 1000\ngid {gid} 1000'
            lxc('config', 'set', '-q', name,
                'security.privileged=false', dry_run=dry_run)
            lxc('config', 'set', '-q', name, 'raw.lxc=',
                dry_run=dry_run)
            lxc('config', 'set', '-q', name,
                'security.nesting=false', dry_run=dry_run)
            if not dry_run:
                subprocess.run(
                    ['lxc', 'config', 'set', '-q', name, 'raw.idmap', '-'],
                    input=idmap.encode(), check=False, capture_output=True)
            else:
                tout.notice(
                    f'printf {idmap!r} | lxc config set -q {name} raw.idmap -')
            tout.notice('Disabled privileged mode')
            if existed and not dry_run:
                status = container_status(name)
                if status == 'RUNNING':
                    tout.notice('Restarting container')
                    lxc('stop', name)
                existed = False
        elif existed and not dry_run:
            if is_privileged(name):
                tout.notice(
                    'Running in privileged mode (device-mapper enabled)')

        tout.progress('Starting container')
        if not ensure_running(name, existed, dry_run):
            return 1

        # In privileged mode, uid namespacing is disabled, so the
        # container's ubuntu user (uid 1000) won't match the host uid.
        # Fix this by changing ubuntu's uid/gid to match the host.
        if args.privileged:
            uid = os.getuid()
            gid = os.getgid()
            lxc_exec(name,
                      f'usermod -u {uid} ubuntu; groupmod -g {gid} ubuntu;'
                      f' chown -R {uid}:{gid} /home/ubuntu',
                      dry_run=dry_run)
        elif args.no_privileged:
            lxc_exec(name,
                      'usermod -u 1000 ubuntu; groupmod -g 1000 ubuntu;'
                      ' chown -R 1000:1000 /home/ubuntu',
                      dry_run=dry_run)

        # Wait for user and set up (idempotent operations)
        tout.progress('Waiting for container to be ready')
        if not wait_for_user(name, dry_run):
            return 1
        tout.progress('Configuring container')
        setup_container(name, dry_run)
        tout.progress('Installing packages')
        install_tools(name, packages, dry_run)
        tout.progress('Installing Claude Code')
        install_claude(name, dry_run)
        tout.progress('Setting up uman')
        setup_uman(name, uboot_tools, dry_run)
        tout.clear_progress()

        # Check X11 access for clipboard (image paste)
        if not dry_run and os.path.isdir('/tmp/.X11-unix'):
            result = exec_cmd(['xhost'], dry_run=False)
            if result and 'LOCAL:' not in result.stdout:
                tout.notice(
                    'For clipboard access (image paste): '
                    'xhost +local:')

        # Start editor proxy so Ctrl-G opens the host editor
        sock_path = start_editor_proxy(project_src, dry_run)

        # Launch
        log_file = get_log_path(name)
        tout.notice(f'Logging to {log_file}')
        if args.shell:
            shell_cmd = args.shell if args.shell is not True else None
            launch_shell(name, shell_cmd, dry_run, log_file)
        else:
            launch_claude(name, args.cont, dry_run, log_file)

    finally:
        # Clean up editor socket
        if os.path.exists(sock_path):
            os.unlink(sock_path)

        # Only delete ephemeral containers that we created
        if not keep and not existed:
            delete_container(name, dry_run)

    return 0
