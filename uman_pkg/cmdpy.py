# SPDX-License-Identifier: GPL-2.0+
# Copyright 2025 Canonical Ltd
# Written by Simon Glass <simon.glass@canonical.com>

"""Pytest command for running U-Boot tests

This module handles the 'pytest' subcommand which runs U-Boot's pytest
test framework.
"""

import ast
import collections
import glob
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import time

# pylint: disable=import-error
from u_boot_pylib import command
from u_boot_pylib import tools
from u_boot_pylib import tout

from uman_pkg import build as build_mod
from uman_pkg import settings
from uman_pkg.cmdtest import get_sandbox_path
from uman_pkg.util import exec_cmd, get_uboot_dir, show_summary

# Pattern to parse test spec: TestClass:method or TestClass.method or just name
RE_TEST_SPEC = re.compile(r'(?:Test)?(\w+?)(?:[:.](\w+))?$', re.IGNORECASE)

# Glob pattern to find test files (use with .format(name=...))
GLOB_TEST = 'test/py/**/test_{name}.py'

# Named tuple for C test information extracted from Python test files
#
# Attributes:
#     suite (str): Test suite name (e.g., 'fs', 'pxe', 'dm')
#     c_test (str): C test function name with _norun suffix
#         (e.g., 'fs_test_ext4l_probe_norun')
#     kwargs (list): List of (arg_key, fixture_name) tuples for run_ut() kwargs
#         (e.g., [('fs_image', 'ext4_image'), ('cfg_path', 'cfg')])
#     fixtures (list): List of fixture names from test method signature
#         (e.g., ['ext4_image'] or ['pxe_fdtdir_image'])
#
# All fields are None on parse failure.
CTestInfo = collections.namedtuple('CTestInfo',
                                   ['suite', 'c_test', 'kwargs', 'fixtures'])


def setup_riscv_env(board, env):
    """Set up OPENSBI environment for RISC-V boards

    Args:
        board (str): Board name
        env (dict): Environment variables dict to update
    """
    # Select 32-bit or 64-bit OpenSBI based on board name
    if 'riscv32' in board:
        opensbi = settings.get('opensbi_rv32', fallback=None)
        # Fallback: derive rv32 path from rv64 path
        if not opensbi:
            rv64_path = settings.get('opensbi', fallback=None)
            if rv64_path:
                opensbi = rv64_path.replace('.bin', '_rv32.bin')
    else:
        opensbi = settings.get('opensbi', fallback=None)
    if opensbi and os.path.exists(opensbi):
        env['OPENSBI'] = opensbi
    elif opensbi:
        tout.warning(f'OPENSBI firmware not found: {opensbi}')
    else:
        tout.warning(f'No OPENSBI firmware configured for {board}')


def setup_sbsa_env(board, env):
    """Set up TF-A environment for SBSA boards

    Args:
        board (str): Board name
        env (dict): Environment variables dict to update
    """
    tfa_dir = settings.get('tfa_dir', fallback=None)
    # Fallback: derive tfa_dir from blobs_dir
    if not tfa_dir:
        blobs_dir = settings.get('blobs_dir', fallback=None)
        if blobs_dir:
            tfa_dir = os.path.join(blobs_dir, 'tfa')
    if tfa_dir and os.path.exists(tfa_dir):
        # Add TF-A directory to binman search path
        current = os.environ.get('BINMAN_INDIRS', '')
        if current:
            env['BINMAN_INDIRS'] = f'{current}:{tfa_dir}'
        else:
            env['BINMAN_INDIRS'] = tfa_dir
    elif tfa_dir:
        tout.warning(f'TF-A directory not found: {tfa_dir}')
    else:
        tout.warning(f'No TF-A directory configured for {board}')


def pytest_env(board):
    """Set up environment variables for pytest testing

    Args:
        board (str): Board name

    Returns:
        dict: Environment variables that were set (not the full environment)
    """
    env = {}

    if 'riscv' in board:
        setup_riscv_env(board, env)

    if 'sbsa' in board:
        setup_sbsa_env(board, env)

    # Build PATH with hooks directories
    path_parts = []

    # Local hooks from U-Boot tree take precedence
    uboot_dir = get_uboot_dir()
    if uboot_dir:
        local_hooks = os.path.join(uboot_dir, 'test/hooks/bin')
        if os.path.exists(local_hooks):
            path_parts.append(local_hooks)

    # Then configured hooks from settings
    hooks = settings.get('test_hooks')
    if hooks and os.path.exists(hooks):
        hooks_bin = os.path.join(hooks, 'bin')
        if os.path.exists(hooks_bin):
            hooks = hooks_bin
        path_parts.append(hooks)

    if path_parts:
        current_path = os.environ.get('PATH', '')
        env['PATH'] = ':'.join(path_parts) + ':' + current_path

    return env


def list_boards_by_pattern(pattern):
    """List available boards matching a pattern using buildman

    Args:
        pattern (str): Board pattern to match (e.g. 'qemu', 'sandbox')

    Returns:
        list: Sorted list of board names
    """
    uboot_dir = get_uboot_dir()
    orig_dir = os.getcwd()
    try:
        if uboot_dir:
            os.chdir(uboot_dir)
        result = command.run_pipe([['buildman', '-nv', pattern]], capture=True,
                                   capture_stderr=True, raise_on_error=False)
    finally:
        os.chdir(orig_dir)

    if result.return_code != 0:
        return []

    boards = []
    for line in result.stdout.splitlines():
        # Board names are on indented lines after "pattern : N boards"
        if line.startswith('   '):
            boards.extend(line.split())
    return sorted(boards)


def list_qemu_boards():
    """List available QEMU boards using buildman

    Returns:
        list: Sorted list of QEMU board names
    """
    return list_boards_by_pattern('qemu')


def get_board_test_id(board):
    """Get the TEST_PY_ID for a board from .gitlab-ci.yml

    Parses the .gitlab-ci.yml file to find the TEST_PY_ID setting for the
    given board.

    Args:
        board (str): Board name to look up

    Returns:
        str: The ID value (e.g. 'qemu'), or 'na' if not found
    """
    uboot_dir = get_uboot_dir()
    if not uboot_dir:
        return 'na'

    gitlab_file = os.path.join(uboot_dir, '.gitlab-ci.yml')
    if not os.path.exists(gitlab_file):
        return 'na'

    try:
        with open(gitlab_file, 'r', encoding='utf-8') as inf:
            content = inf.read()
    except OSError:
        return 'na'

    # Look for TEST_PY_BD: "board" followed by TEST_PY_ID: "--id xxx"
    # The pattern handles YAML format with quotes
    esc = re.escape(board)
    pattern = (rf'TEST_PY_BD:\s*["\']?{esc}["\']?\s*\n'
               r'\s*TEST_PY_ID:\s*["\']?--id\s+(\w+)["\']?')
    match = re.search(pattern, content)
    if match:
        return match.group(1)

    return 'na'


def get_qemu_binary(board, board_id):
    """Get the QEMU binary required for a board from test hooks config

    Searches for the test hooks configuration file and parses the
    qemu_binary setting.

    Args:
        board (str): Board name
        board_id (str): Board ID (e.g. 'qemu', 'na')

    Returns:
        str or None: QEMU binary name if found, None otherwise
    """
    uboot_dir = get_uboot_dir()
    if not uboot_dir:
        return None

    # Search for config file in test hooks directories
    hooks_dir = os.path.join(uboot_dir, 'test', 'hooks', 'bin')
    config_name = f'conf.{board}_{board_id}'

    # Check in subdirectories (travis-ci, ellesmere, etc.)
    for root, _, files in os.walk(hooks_dir):
        if config_name in files:
            config_path = os.path.join(root, config_name)
            try:
                with open(config_path, 'r', encoding='utf-8') as inf:
                    for line in inf:
                        pat = r'qemu_binary=["\']?([^"\']+)["\']?'
                        match = re.match(pat, line)
                        if match:
                            return match.group(1)
            except OSError:
                pass

    return None


def check_qemu_binary(board, board_id):
    """Check if the required QEMU binary is available

    Args:
        board (str): Board name
        board_id (str): Board ID (e.g. 'qemu', 'na')

    Returns:
        tuple: (binary_name, is_available) or (None, True) if no QEMU needed
    """
    binary = get_qemu_binary(board, board_id)
    if not binary:
        return None, True

    return binary, shutil.which(binary) is not None


def build_pytest_cmd(args):
    """Build the pytest command line

    Args:
        args (argparse.Namespace): Arguments from cmdline

    Returns:
        list: Command and arguments to run
    """
    cmd = ['./test/py/test.py']
    cmd.extend(['-B', args.board])

    if args.output_dir:
        build_dir = args.output_dir
    else:
        base_dir = settings.get('build_dir', '/tmp/b')
        build_dir = f'{base_dir}/{args.board}'
    cmd.extend(['--build-dir', build_dir])

    if args.build:
        cmd.append('--build')

    cmd.append('--buildman')

    board_id = get_board_test_id(args.board)
    cmd.extend(['--id', board_id])

    if args.test_spec:
        # Convert Class:method or Class::method to "Class and method" for -k
        spec = ' '.join(args.test_spec)
        spec = spec.replace('::', ' and ').replace(':', ' and ')
        cmd.extend(['-k', spec])

    if args.no_timeout:
        cmd.append('--no-timeout')

    cmd.append('-q')
    if args.quiet:
        cmd.extend(['--no-header', '--quiet-hooks'])
    if args.show_output:
        cmd.append('-s')
    if args.timing is not None:
        cmd.extend(['--timing', '--durations=0',
                    f'--durations-min={args.timing}'])
    if args.setup_only:
        cmd.append('--setup-only')
    if args.persist:
        cmd.append('--persist')
    if args.gdbserver:
        cmd.extend(['--gdbserver', args.gdbserver])
    if args.exitfirst:
        cmd.append('-x')
    if not args.flattree_too:
        cmd.append('--no-full')

    # Add extra pytest arguments (after --)
    if args.extra_args:
        cmd.extend(args.extra_args)

    return cmd


def parse_hook_config(config_path):
    """Parse shell variable assignments from a hook config file

    Args:
        config_path (str): Path to the config file

    Returns:
        dict: Dictionary of variable names to values
    """
    variables = {}
    if not os.path.exists(config_path):
        return variables

    with open(config_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            # Skip comments and empty lines
            if not line or line.startswith('#'):
                continue
            # Match variable assignments: name=value or name="value"
            match = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*)=(.*)$', line)
            if match:
                name, value = match.groups()
                # Remove surrounding quotes if present
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                elif value.startswith("'") and value.endswith("'"):
                    value = value[1:-1]
                variables[name] = value
    return variables


def expand_vars(value, env):
    """Expand shell-style variable references in a string

    Args:
        value (str): String potentially containing ${VAR} references
        env (dict): Environment variables for substitution

    Returns:
        str: String with variables expanded
    """
    def replace_var(match):
        var_name = match.group(1)
        return env.get(var_name, f'${{{var_name}}}')

    return re.sub(r'\$\{([^}]+)\}', replace_var, value)


def get_board_config(board):
    """Get the hook configuration for a board

    Args:
        board (str): Board name

    Returns:
        dict: Configuration with keys like 'console_impl', 'qemu_binary',
            'qemu_machine', 'qemu_extra_args', 'qemu_kernel_args', etc.,
            or None if not found
    """
    hooks = settings.get('test_hooks')
    if not hooks:
        tout.error('test_hooks not configured in settings')
        return None

    hooks_bin = os.path.join(hooks, 'bin')
    if not os.path.exists(hooks_bin):
        tout.error(f'Hooks bin directory not found: {hooks_bin}')
        return None

    hostname = socket.gethostname()
    board_id = 'na'  # Default board identifier

    # Build config file path
    cfg = os.path.join(hooks_bin, hostname, f'conf.{board}_{board_id}')

    # Resolve symlinks
    if os.path.islink(cfg):
        cfg = os.path.realpath(cfg)

    if not os.path.exists(cfg):
        tout.error(f'Config file not found: {cfg}')
        return None

    return parse_hook_config(cfg)


def get_qemu_command(board, args):
    """Build the QEMU command line from hook-config files

    Args:
        board (str): Board name
        args (argparse.Namespace): Arguments from cmdline

    Returns:
        str: QEMU command line, or None if not a QEMU board
    """
    config = get_board_config(board)
    if not config:
        return None

    # Check if this is a QEMU board
    if config.get('console_impl') != 'qemu':
        tout.warning(f'Board {board} is not a QEMU board '
                     f'(console_impl={config.get("console_impl")})')
        return None

    # Build environment for variable expansion
    if args.output_dir:
        build_dir = args.output_dir
    else:
        base_dir = settings.get('build_dir', '/tmp/b')
        build_dir = f'{base_dir}/{board}'

    env = os.environ.copy()
    env['U_BOOT_BUILD_DIR'] = build_dir
    env['UBOOT_TRAVIS_BUILD_DIR'] = build_dir

    # Add OPENSBI if configured
    pytest_vars = pytest_env(board)
    env.update(pytest_vars)

    # Extract QEMU command components
    qemu_binary = config.get('qemu_binary', 'qemu-system-unknown')
    qemu_machine = config.get('qemu_machine', '')
    qemu_extra_args = config.get('qemu_extra_args', '')
    qemu_kernel_args = config.get('qemu_kernel_args', '')

    # Expand variables
    qemu_extra_args = expand_vars(qemu_extra_args, env)
    qemu_kernel_args = expand_vars(qemu_kernel_args, env)

    # Build command line
    cmd_parts = [qemu_binary]
    if qemu_extra_args:
        cmd_parts.append(qemu_extra_args)
    cmd_parts.append(f'-M {qemu_machine}')
    if qemu_kernel_args:
        cmd_parts.append(qemu_kernel_args)

    return ' '.join(cmd_parts)


def camel_to_snake(name):
    """Convert CamelCase to snake_case

    Args:
        name (str): CamelCase string (e.g., 'PxeParser')

    Returns:
        str: snake_case string (e.g., 'pxe_parser')
    """
    return re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()


def find_test(uboot_dir, test_spec):
    """Find the Python test file for a test spec

    Args:
        uboot_dir (str): U-Boot source directory
        test_spec (str): Test spec like 'TestPxeParser:test_pxe_ipappend'

    Returns:
        tuple: (file_path, class_name, method_name) or (None, None, None)
    """
    match = RE_TEST_SPEC.match(test_spec)
    if not match:
        return None, None, None

    base_name = match.group(1)
    method = match.group(2)

    # Convert CamelCase to snake_case for file lookup
    snake_name = camel_to_snake(base_name)

    # Search for test file
    pattern = os.path.join(uboot_dir, GLOB_TEST.format(name=snake_name))
    matches = glob.glob(pattern, recursive=True)
    if matches:
        test_file = matches[0]
        # Build class name from original base_name
        class_name = f'Test{base_name[0].upper()}{base_name[1:]}'
        return test_file, class_name, method

    return None, None, None


def find_run_ut_call(method_node):
    """Find a run_ut() call in a method's AST

    Args:
        method_node (ast.FunctionDef): Method node to search

    Returns:
        ast.Call or None: The run_ut() call node, or None if not found
    """
    for stmt in ast.walk(method_node):
        if not isinstance(stmt, ast.Call):
            continue
        if not isinstance(stmt.func, ast.Attribute):
            continue
        if stmt.func.attr == 'run_ut':
            return stmt
    return None


def parse_c_test_call(source, class_name, method_name):
    """Parse Python test source to extract the C test command

    Looks for ubman.run_ut() calls in the test method.

    Args:
        source (str): Python source code
        class_name (str): Test class name
        method_name (str): Test method name

    Returns:
        CTestInfo: Named tuple with suite, c_test, kwargs, fixtures fields,
            or CTestInfo(None, None, None, None) on failure
    """
    tree = ast.parse(source)

    # Find the class and method
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for item in node.body:
            if not isinstance(item, ast.FunctionDef):
                continue
            if item.name != method_name:
                continue
            call = find_run_ut_call(item)
            if call:
                # Extract fixture names from params (skip self, ubman)
                fixtures = [arg.arg for arg in item.args.args
                            if arg.arg not in ('self', 'ubman')]
                info = extract_run_ut_args(call)
                return CTestInfo(info.suite, info.c_test, info.kwargs, fixtures)

    return CTestInfo(None, None, None, None)


def extract_run_ut_args(call_node):
    """Extract C test info from a run_ut() call AST node

    Parses: ubman.run_ut('fs', 'fs_test_ext4l_probe', fs_image=img, cfg=path)

    Args:
        call_node (ast.Call): AST Call node for run_ut()

    Returns:
        CTestInfo: Named tuple with suite, c_test, kwargs fields
            (fixtures=None), or CTestInfo(None, None, None, None) on failure
    """
    # Need at least 2 positional args: suite and test name
    if len(call_node.args) < 2:
        return CTestInfo(None, None, None, None)

    # Extract suite (first arg)
    if not isinstance(call_node.args[0], ast.Constant):
        return CTestInfo(None, None, None, None)
    suite = call_node.args[0].value

    # Extract test name (second arg) - add _norun suffix
    if not isinstance(call_node.args[1], ast.Constant):
        return CTestInfo(None, None, None, None)
    c_test = call_node.args[1].value + '_norun'

    # Extract all keyword arguments (e.g., fs_image=ext4_image, cfg_path=cfg)
    if not call_node.keywords:
        return CTestInfo(None, None, None, None)

    kwargs = []
    for kw in call_node.keywords:
        if isinstance(kw.value, ast.Name):
            kwargs.append((kw.arg, kw.value.id))

    if not kwargs:
        return CTestInfo(None, None, None, None)

    return CTestInfo(suite, c_test, kwargs, None)


def get_fixture_paths(test_file, kwargs, fixtures):
    """Get fixture paths for all kwargs in a run_ut() call

    Args:
        test_file (str): Path to Python test file
        kwargs (list): List of (arg_key, fixture_name) tuples from run_ut()
        fixtures (list): List of fixture names from method signature

    Returns:
        tuple: (paths_dict, reason) where paths_dict maps arg_key to path,
            or (None, reason) on failure
    """
    source = tools.read_file(test_file, binary=False)
    build_dir = settings.get('build_dir', '/tmp/b')
    persistent_dir = os.path.join(build_dir, 'sandbox', 'persistent-data')

    # Find fixture definitions for image fixtures
    fixture_defs = {}
    for fixture in fixtures:
        # Match: def fixture_name(...):  ...until next def or end
        pat = rf"def\s+{re.escape(fixture)}\s*\([^)]*\):\s*(.*?)(?=\ndef\s|\Z)"
        match = re.search(pat, source, re.DOTALL)
        if match:
            fixture_defs[fixture] = match.group(1)

    paths = {}
    for arg_key, _ in kwargs:
        if arg_key in ('fs_image', 'image'):
            # Search in fixture definitions for FsHelper pattern
            for fixture_src in fixture_defs.values():
                match = re.search(
                    r"FsHelper\s*\([^,]+,\s*['\"](\w+)['\"].*?"
                    r"prefix\s*=\s*['\"](\w+)['\"]",
                    fixture_src, re.DOTALL)
                if match:
                    fs_type = match.group(1)
                    prefix = match.group(2)
                    img_name = f'{prefix}.{fs_type}.img'
                    paths[arg_key] = os.path.join(persistent_dir, img_name)
                    break
            if arg_key in paths:
                continue

            # Look for image_path pattern in fixture definitions
            for fixture_src in fixture_defs.values():
                match = re.search(r"image_path\s*=.*?['\"](\w+\.img)['\"]",
                                  fixture_src, re.DOTALL)
                if match:
                    img_name = match.group(1)
                    paths[arg_key] = os.path.join(persistent_dir, img_name)
                    break
            if arg_key in paths:
                continue

        elif arg_key == 'cfg_path':
            # Check if fixture calls create_extlinux_conf (standard path)
            for fixture_src in fixture_defs.values():
                if 'create_extlinux_conf' in fixture_src:
                    paths[arg_key] = '/extlinux/extlinux.conf'
                    break
            if arg_key in paths:
                continue

        # Couldn't find path for this kwarg
        return None, f'cannot determine {arg_key} path'

    return paths, None


def run_c_test(args):
    """Run just the C test part of a pytest test

    Args:
        args (argparse.Namespace): Arguments from cmdline

    Returns:
        int: Exit code
    """
    if not args.test_spec:
        tout.error('Test spec required for -C (e.g., TestExt4l:test_unlink)')
        return 1

    uboot_dir = get_uboot_dir()
    if not uboot_dir:
        tout.error('Not in a U-Boot tree and $USRC not set')
        return 1

    # Build if requested
    if args.build:
        if not build_mod.build_board(
                'sandbox', args.dry_run, args.lto,
                adjust_cfg=args.adjust_cfg,
                force_reconfig=args.force_reconfig, fresh=args.fresh,
                jobs=args.jobs, trace=args.trace,
                trace_early=not args.no_trace_early,
                output_dir=args.output_dir):
            return 1

    sandbox = get_sandbox_path()
    if not sandbox:
        tout.error('Sandbox not built - run: uman b sandbox')
        return 1

    test_name = args.test_spec[0]
    test_file, class_name, method = find_test(uboot_dir, test_name)
    if not test_file:
        tout.error(f"Cannot find test file for '{test_name}'")
        return 1

    if not method:
        tout.error('Method name required (e.g., TestExt4l:test_unlink)')
        return 1

    source = tools.read_file(test_file, binary=False)
    info = parse_c_test_call(source, class_name, method)
    if not info.suite:
        tout.error(f'Cannot find C test command in {class_name}.{method}')
        return 1

    # Get fixture paths for all kwargs
    paths, reason = get_fixture_paths(test_file, info.kwargs, info.fixtures)
    if not paths:
        tout.error(f'Test {reason} - not suitable for -C')
        tout.notice(f'Run the full test instead: um py {test_name}')
        return 1

    # Check fs_image exists (the main fixture file)
    for arg_key, path in paths.items():
        if arg_key in ('fs_image', 'image') and not os.path.exists(path):
            tout.error(f'Setup not done, run: um py -SP {test_name}')
            return 1

    # Build ut command with all kwargs
    ut_args = ' '.join(f'{k}={v}' for k, v in paths.items())
    ut_cmd = f'ut -Em {info.suite} {info.c_test} {ut_args}'
    cmd = [sandbox, '-T', '-F', '-c', ut_cmd]
    if args.show_output:
        cmd.insert(1, '-v')

    start = time.time()
    result = exec_cmd(cmd, dry_run=args.dry_run,
                      capture=not args.show_output)
    elapsed = time.time() - start

    if not result:
        return 0

    # Parse result and count passed/failed/skipped
    passed = failed = skipped = 0
    if not args.show_output:
        match = re.search(r'Result: (PASS|FAIL|SKIP):', result.stdout)
        if match:
            status = match.group(1)
            if status == 'PASS':
                passed = 1
            elif status == 'FAIL':
                failed = 1
            elif status == 'SKIP':
                skipped = 1

        # Show output only on failure
        if failed and result.stdout:
            print(result.stdout, end='')

    show_summary(passed, failed, skipped, elapsed)

    return result.return_code


def run_with_gdb(args):
    """Launch gdb to connect to an existing gdbserver

    Args:
        args (argparse.Namespace): Arguments from cmdline

    Returns:
        int: Exit code
    """
    # Get the U-Boot executable path
    if args.output_dir:
        build_dir = args.output_dir
    else:
        base_dir = settings.get('build_dir', '/tmp/b')
        build_dir = f'{base_dir}/{args.board}'
    uboot_exe = os.path.join(build_dir, 'u-boot')

    if not os.path.exists(uboot_exe):
        tout.error(f'U-Boot executable not found: {uboot_exe}')
        return 1

    # Get gdbserver channel
    channel = args.gdbserver or 'localhost:1234'

    # Build gdb command
    gdb_cmd = [
        'gdb-multiarch',
        uboot_exe,
        '-ex', f'target remote {channel}',
        '-ex', 'continue',
    ]

    tout.info(f"Running: {' '.join(gdb_cmd)}")

    # Parse host:port from channel
    if ':' in channel:
        host, port = channel.rsplit(':', 1)
        port = int(port)
    else:
        host, port = 'localhost', int(channel)

    def port_alive():
        """Check if gdbserver port is accepting connections"""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.5)
            sock.connect((host, port))
            sock.close()
            return True
        except (ConnectionRefusedError, socket.timeout, OSError):
            return False

    # Run gdb in a loop, reconnecting when server restarts
    reconnect_timeout = 5  # seconds to wait for server after gdb disconnects
    while True:
        # pylint: disable=consider-using-with
        proc = subprocess.Popen(gdb_cmd)

        # Wait for gdb to connect before monitoring
        time.sleep(1)

        # Monitor for server to become available (indicates U-Boot restarted)
        # While gdb is connected, the port is occupied so port_alive() is False
        # When U-Boot restarts, gdbserver listens again and port_alive() is True
        try:
            while proc.poll() is None:
                if port_alive():
                    # Server is accepting connections - U-Boot restarted
                    tout.notice('Server restarted, reconnecting...')
                    proc.terminate()
                    proc.wait()
                    break
                time.sleep(0.5)
        except KeyboardInterrupt:
            proc.terminate()
            proc.wait()
            break

        # gdb exited - wait briefly for server to come back
        if proc.returncode is not None:
            start = time.time()
            while time.time() - start < reconnect_timeout:
                if port_alive():
                    tout.notice('Server restarted, reconnecting...')
                    break
                time.sleep(0.2)
            else:
                # Server didn't come back, tests finished
                tout.notice('Server not responding, exiting')
                break

    return 0


def collect_tests(args):
    """Collect all tests using pytest --collect-only

    Args:
        args (argparse.Namespace): Arguments from cmdline

    Returns:
        list: Ordered list of test node IDs, or None on error
    """
    if args.output_dir:
        build_dir = args.output_dir
    else:
        base_dir = settings.get('build_dir', '/tmp/b')
        build_dir = f'{base_dir}/{args.board}-pollute'

    cmd = ['./test/py/test.py', '-B', args.board, '--build-dir', build_dir,
           '--buildman', '--id', 'na', '--collect-only', '-q']

    if args.build:
        cmd.append('--build')
    if not args.flattree_too:
        cmd.append('--no-full')

    if args.test_spec:
        spec = ' '.join(args.test_spec)
        cmd.extend(['-k', spec])

    result = command.run_pipe([cmd], capture=True, capture_stderr=True,
                              raise_on_error=False)
    if result.return_code != 0:
        if 'unrecognized arguments: --no-full' in result.stderr:
            tout.error(
                'U-Boot does not support --no-full; use -f to run all tests')
        else:
            tout.error('Failed to collect tests')
            if result.stderr:
                print(result.stderr)
        return None

    tests = []
    for line in result.stdout.splitlines():
        line = line.strip()
        # Test lines contain :: (e.g., test_ut.py::TestUt::test_dm)
        if '::' in line and not line.startswith('<'):
            tests.append(line)
    return tests


def find_tests(args):
    """Find tests matching a pattern and show their full IDs

    Args:
        args (argparse.Namespace): Arguments from cmdline

    Returns:
        int: Exit code
    """
    uboot_dir = get_uboot_dir()
    if not uboot_dir:
        tout.error('Not in a U-Boot tree and $USRC not set')
        return 1

    if uboot_dir != os.getcwd():
        os.chdir(uboot_dir)

    tout.notice('Collecting tests...')
    tests = collect_tests(args)
    if tests is None:
        return 1

    pattern = args.find.lower()
    matches = [t for t in tests if pattern in t.lower()]

    if not matches:
        tout.warning(f"No tests matching '{args.find}'")
        return 1

    tout.notice(f'Found {len(matches)} test(s):')
    for test in matches:
        print(f'  {test}')
    return 0


def node_to_name(node_id):
    """Extract test name from a pytest node ID for use with -k

    Args:
        node_id (str): Full node ID like 'tests/test_ut.py::test_ut[ut_dm_foo]'

    Returns:
        str: Test name suitable for -k, e.g. 'ut_dm_foo'
    """
    # Extract the part in brackets if present (parameterized tests)
    if '[' in node_id and node_id.endswith(']'):
        return node_id[node_id.index('[') + 1:-1]
    # Otherwise use the method name after the last ::
    if '::' in node_id:
        return node_id.split('::')[-1]
    return node_id


def pollute_run(tests, target, args, env):
    """Run a subset of tests followed by the target test

    Args:
        tests (list): Tests to run before target (full node IDs)
        target (str): Target test that may fail (full node ID)
        args (argparse.Namespace): Arguments from cmdline
        env (dict): Environment variables

    Returns:
        bool: True if target test failed, False if it passed
    """
    if args.output_dir:
        build_dir = args.output_dir
    else:
        base_dir = settings.get('build_dir', '/tmp/b')
        build_dir = f'{base_dir}/{args.board}-pollute'

    # Convert node IDs to test names and join with "or" for -k
    all_tests = tests + [target]
    names = [node_to_name(t) for t in all_tests]
    spec = ' or '.join(names)

    cmd = ['./test/py/test.py', '-B', args.board, '--build-dir', build_dir,
           '--buildman', '--id', 'na', '-q', '-k', spec]
    if args.lto:
        cmd.append('--lto')
    if not args.flattree_too:
        cmd.append('--no-full')

    total = len(all_tests)
    done = 0
    # pytest result chars: . pass, F fail, s skip, E error, x xfail, X xpass
    result_chars = '.FsExX'

    # Run with Popen to show progress as tests complete
    # pylint: disable=consider-using-with
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    while True:
        char = proc.stdout.read(1)
        if not char:
            break
        if char.decode('utf-8', errors='replace') in result_chars:
            done += 1
            tout.progress(f'    {done}/{total}', trailer='')
    tout.clear_progress()
    proc.wait()
    return proc.returncode != 0


def do_pollute(args):
    """Find which test pollutes the target test

    Args:
        args (argparse.Namespace): Arguments from cmdline

    Returns:
        int: Exit code
    """
    target = args.pollute

    # Find U-Boot source directory
    uboot_dir = get_uboot_dir()
    if not uboot_dir:
        tout.error('Not in a U-Boot tree and $USRC not set')
        return 1

    # Change to U-Boot directory if needed
    if uboot_dir != os.getcwd():
        os.chdir(uboot_dir)

    # Build to the pollute directory if requested
    if args.build:
        base_dir = settings.get('build_dir', '/tmp/b')
        build_dir = f'{base_dir}/{args.board}-pollute'
        tout.notice(f'Building to {build_dir}...')
        cmd = ['buildman', '-I', '-w', '--boards', args.board, '-o', build_dir]
        if not args.lto:
            cmd.insert(1, '-L')
        result = exec_cmd(cmd, args.dry_run, capture=False)
        if result and result.return_code != 0:
            tout.error('Build failed')
            return 1

    tout.notice('Collecting tests...')
    tests = collect_tests(args)
    if tests is None:
        return 1

    # Find target in test list
    target_idx = None
    for i, test in enumerate(tests):
        if target in test:
            target_idx = i
            target = test  # Use full test name
            break

    if target_idx is None:
        tout.error(f"Target test '{args.pollute}' not found in collection")
        tout.info('Available tests containing that string:')
        for test in tests:
            if args.pollute.lower() in test.lower():
                print(f'  {test}')
        return 1

    tout.notice(f"Found {len(tests)} tests, target '{target}' at position "
                f'{target_idx + 1}')

    if target_idx == 0:
        tout.error('Target is the first test - nothing can pollute it')
        return 1

    candidates = tests[:target_idx]
    pytest_vars = pytest_env(args.board)
    env = os.environ.copy()
    env.update(pytest_vars)

    # Verify target passes alone
    tout.notice('Verifying target passes alone...')
    if pollute_run([], target, args, env):
        tout.error('Target test fails when run alone - not a pollution issue')
        return 1
    tout.notice('  OK')

    # Verify target fails with all candidates
    tout.notice('Verifying target fails with all prior tests...')
    if not pollute_run(candidates, target, args, env):
        tout.error('Target test passes with all prior tests - cannot reproduce')
        return 1
    tout.notice('  FAIL (confirmed)')

    # Binary search
    steps = math.ceil(math.log2(len(candidates))) if candidates else 0
    step = 0

    tout.notice(f'Searching for polluter in {len(candidates)} candidates...')
    while len(candidates) > 1:
        step += 1
        mid = len(candidates) // 2
        first_half = candidates[:mid]

        print(f'  Step {step}/{steps}: {len(first_half)} tests...')
        if pollute_run(first_half, target, args, env):
            tout.notice('  -> FAIL (polluter in first half)')
            candidates = first_half
        else:
            tout.notice('  -> PASS (polluter in second half)')
            candidates = candidates[mid:]

    if not candidates:
        tout.error('No polluter found - may need multiple tests to trigger')
        return 1

    polluter = candidates[0]

    # Final verification
    print(f'  Verifying {node_to_name(polluter)}...')
    if pollute_run([polluter], target, args, env):
        tout.notice('  -> FAIL (confirmed)')
    else:
        tout.notice('  -> PASS (inconclusive - may need multiple tests)')
        return 1

    polluter_name = node_to_name(polluter)
    target_name = node_to_name(target)
    red = '\033[31m'
    reset = '\033[0m'
    tout.notice(
        f'\nFound: {target_name} polluted by {red}{polluter_name}{reset}')
    tout.notice(f'  Run: uman py -B {args.board} "{polluter} or {target}"')
    return 0


def do_pytest(args):  # pylint: disable=too-many-return-statements,too-many-branches
    """Handle pytest command - run pytest tests for U-Boot

    Args:
        args (argparse.Namespace): Arguments from cmdline

    Returns:
        int: Exit code
    """
    if args.list_boards:
        qemu_boards = list_qemu_boards()
        sandbox_boards = list_boards_by_pattern('sandbox')
        m68k_boards = list_boards_by_pattern('M5208')
        if qemu_boards:
            tout.notice('Available QEMU boards:')
            for board in qemu_boards:
                print(f'  {board}')
        if m68k_boards:
            tout.notice('Available m68k boards:')
            for board in m68k_boards:
                print(f'  {board}')
        if sandbox_boards:
            tout.notice('Available sandbox boards:')
            for board in sandbox_boards:
                print(f'  {board}')
        if not qemu_boards and not sandbox_boards and not m68k_boards:
            tout.warning('No boards found (is buildman configured?)')
        return 0

    # Handle -C option: run just the C test part
    if args.c_test:
        return run_c_test(args)

    board = args.board or os.environ.get('b')
    if not board:
        tout.error('Board is required: use -B BOARD or set $b (use -l to list)')
        return 1
    args.board = board

    # Handle --pollute option
    if args.pollute:
        return do_pollute(args)

    # Handle --find option
    if args.find:
        return find_tests(args)

    # Handle --show-cmd option
    if args.show_cmd:
        qemu_cmd = get_qemu_command(board, args)
        if qemu_cmd:
            print(qemu_cmd)
            return 0
        return 1

    # Find U-Boot source directory
    uboot_dir = get_uboot_dir()
    if not uboot_dir:
        tout.error('Not in a U-Boot tree and $USRC not set')
        return 1

    # Change to U-Boot directory if needed
    if uboot_dir != os.getcwd():
        tout.info(f'Changing to U-Boot directory: {uboot_dir}')
        os.chdir(uboot_dir)

    tout.info(f'Running pytest for board: {args.board}')

    # Check if required QEMU binary is available
    board_id = get_board_test_id(args.board)
    qemu_binary, available = check_qemu_binary(args.board, board_id)
    if qemu_binary and not available:
        tout.error(f'QEMU binary not found: {qemu_binary}')
        tout.notice('Try: uman setup qemu')
        return 1

    # Handle -G: set gdbserver if not already set
    if args.gdb and not args.gdbserver:
        args.gdbserver = 'localhost:1234'

    # Build with um if requested, rather than letting pytest do it
    if args.build:
        if not build_mod.build_board(
                args.board, args.dry_run, args.lto,
                adjust_cfg=args.adjust_cfg,
                force_reconfig=args.force_reconfig, fresh=args.fresh,
                jobs=args.jobs, trace=args.trace,
                trace_early=not args.no_trace_early,
                output_dir=args.output_dir):
            return 1
        args.build = False  # Don't build again in pytest

    # Show -G command hint when using -g (not in dry-run mode)
    if args.gdbserver and not args.gdb and not args.dry_run:
        tout.notice(f'In another terminal: um py -G -B {args.board}')

    pytest_vars = pytest_env(args.board)
    cmd = build_pytest_cmd(args)

    env = os.environ.copy()
    env.update(pytest_vars)

    # Handle -G: just launch gdb to connect to existing gdbserver
    if args.gdb:
        return run_with_gdb(args)

    result = exec_cmd(cmd, args.dry_run, env=env, capture=False)

    if result is None:  # dry-run
        return 0

    if result.return_code != 0:
        if 'unrecognized arguments: --no-full' in result.stderr:
            tout.error(
                'U-Boot does not support --no-full; use -f to run all tests')
        else:
            if result.stderr:
                print(result.stderr, file=sys.stderr)
            if not args.quiet:
                tout.error('pytest failed')
        return result.return_code

    if not args.quiet:
        tout.notice('pytest passed')
    return 0
