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
from uman_pkg.cmdtest import get_sandbox_path, parse_results
from uman_pkg.util import (exec_cmd, get_uboot_dir, show_leak_top,
                            show_summary)

# Fallback hostname directory for test hooks (has standard QEMU board configs)
HOOKS_FALLBACK = 'travis-ci'

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


def has_no_full():
    """Check whether the U-Boot tree supports --no-full

    Looks for 'no-full' in test/py/conftest.py in the current directory.

    Returns:
        bool: True if --no-full is supported
    """
    conftest = os.path.join('test', 'py', 'conftest.py')
    try:
        return 'no-full' in tools.read_file(conftest, binary=False)
    except OSError:
        return False


def setup_riscv_env(board, env):
    """Set up OPENSBI environment for RISC-V boards

    Args:
        board (str): Board name
        env (dict): Environment variables dict to update
    """
    # Select 32-bit or 64-bit OpenSBI based on board name
    if 'riscv32' in board or 'mbv32' in board:
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


def ensure_hooks_host(hooks_bin):
    """Ensure a hostname directory exists in the hooks bin directory

    The test hooks scripts use hostname to find board config files. In
    containers the hostname may not match any existing directory. Create a
    symlink to the travis-ci directory which has all the standard QEMU configs.

    Args:
        hooks_bin (str): Path to the hooks bin/ directory
    """
    hostname = socket.gethostname()
    host_dir = os.path.join(hooks_bin, hostname)
    if os.path.exists(host_dir):
        return
    fallback = os.path.join(hooks_bin, HOOKS_FALLBACK)
    if os.path.exists(fallback):
        os.symlink(HOOKS_FALLBACK, host_dir)
        tout.notice(f'Created symlink {host_dir} -> {HOOKS_FALLBACK}')


def pytest_env(board, test_py_id=None):
    """Set up environment variables for pytest testing

    Args:
        board (str): Board name
        test_py_id (str or None): TEST_PY_ID override, or None for default

    Returns:
        dict: Environment variables that were set (not the full environment)
    """
    env = {}

    if 'riscv' in board or 'mbv' in board:
        setup_riscv_env(board, env)

    if 'sbsa' in board:
        setup_sbsa_env(board, env)

    # Build PATH with hooks directories
    path_parts = []

    # Local hooks from U-Boot tree take precedence
    uboot_dir = get_uboot_dir()

    # When --id is specified, add hooks pythonpath so boardenv files are found
    if test_py_id and uboot_dir:
        hooks_py = os.path.join(uboot_dir, 'test/hooks/py/travis-ci')
        if os.path.exists(hooks_py):
            current = os.environ.get('PYTHONPATH', '')
            if current:
                env['PYTHONPATH'] = f'{hooks_py}:{current}'
            else:
                env['PYTHONPATH'] = hooks_py

    if uboot_dir:
        local_hooks = os.path.join(uboot_dir, 'test/hooks/bin')
        if os.path.exists(local_hooks):
            ensure_hooks_host(local_hooks)
            path_parts.append(local_hooks)

    # Then configured hooks from settings
    hooks = settings.get('test_hooks')
    if hooks and os.path.exists(hooks):
        hooks_bin = os.path.join(hooks, 'bin')
        if os.path.exists(hooks_bin):
            hooks = hooks_bin
        path_parts.append(hooks)

    # Add custom QEMU build if present
    qemu_build = settings.get('qemu_build_dir',
                              fallback='~/dev/qemu/build')
    if qemu_build and os.path.isdir(qemu_build):
        path_parts.append(qemu_build)

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
        result = command.run_pipe(
            [[build_mod.get_buildman(), '-nv', pattern]], capture=True,
            capture_stderr=True, raise_on_error=False)
    finally:
        os.chdir(orig_dir)

    if result.return_code != 0:
        stderr = result.stderr.strip() if result.stderr else ''
        stdout = result.stdout.strip() if result.stdout else ''
        msg = stderr or stdout
        if msg:
            last = msg.splitlines()[-1]
            if 'No matching' not in last:
                tout.warning(f'buildman: {last}')
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


def get_gitlab_content():
    """Read and cache .gitlab-ci.yml content

    Returns:
        str: File content, or None if not found
    """
    uboot_dir = get_uboot_dir()
    if not uboot_dir:
        return None

    gitlab_file = os.path.join(uboot_dir, '.gitlab-ci.yml')
    if not os.path.exists(gitlab_file):
        return None

    try:
        return tools.read_file(gitlab_file, binary=False)
    except OSError:
        return None


def get_board_gitlab_vars(board):
    """Get all CI variables for a board from .gitlab-ci.yml

    Parses the variables section for the board's test job.

    Args:
        board (str): Board name to look up

    Returns:
        dict: Variables dict with keys like TEST_PY_ID, TEST_PY_TEST_SPEC,
            OVERRIDE, or empty dict if not found
    """
    content = get_gitlab_content()
    if not content:
        return {}

    # Find the variables block for this board
    esc = re.escape(board)
    pattern = rf'TEST_PY_BD:\s*["\']?{esc}["\']?'
    match = re.search(pattern, content)
    if not match:
        return {}

    # Get lines after the board name until we hit a blank line
    start = match.end()
    lines = content[start:].split('\n')

    result = {}
    for line in lines:
        stripped = line.strip()
        # Skip empty lines at start, stop at blank line after content
        if not stripped:
            if result:  # Already have some vars, blank line ends block
                break
            continue

        # Stop at non-variable line (but skip <<: anchors)
        if stripped.startswith('<<:'):
            continue
        if not stripped.startswith('TEST_PY_') and \
           not stripped.startswith('OVERRIDE'):
            break

        # Parse variable: value
        var_match = re.match(r'(\w+):\s*["\']?([^"\']+)["\']?', stripped)
        if var_match:
            result[var_match.group(1)] = var_match.group(2).strip()

    return result


def get_board_test_id(board):
    """Get the TEST_PY_ID for a board from .gitlab-ci.yml

    Args:
        board (str): Board name to look up

    Returns:
        str: The ID value (e.g. 'qemu'), or 'na' if not found
    """
    variables = get_board_gitlab_vars(board)
    test_id = variables.get('TEST_PY_ID', '')

    # Parse "--id xxx" format
    match = re.match(r'--id\s+(\w+)', test_id)
    if match:
        return match.group(1)

    return 'na'


def get_board_test_spec(board):
    """Get the TEST_PY_TEST_SPEC for a board from .gitlab-ci.yml

    Args:
        board (str): Board name to look up

    Returns:
        str: The test spec (e.g. 'not sleep and not efi'), or None if not set
    """
    variables = get_board_gitlab_vars(board)
    return variables.get('TEST_PY_TEST_SPEC')


def get_board_override(board):
    """Get the OVERRIDE config adjustments for a board from .gitlab-ci.yml

    Args:
        board (str): Board name to look up

    Returns:
        list: List of adjust_cfg values (e.g. ['CONFIG_M68K_QEMU=y',
            '~CONFIG_MCFTMR']), or empty list if not set
    """
    variables = get_board_gitlab_vars(board)
    override = variables.get('OVERRIDE', '')

    # Parse "-a CONFIG_FOO=y -a ~CONFIG_BAR" format
    adjustments = []
    for match in re.finditer(r'-a\s+(\S+)', override):
        adjustments.append(match.group(1))

    return adjustments


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


def show_pytest_hint(args):
    """Show a hint about why pytest may have failed

    Checks the test log for common failure patterns and prints a
    helpful message.

    Args:
        args (argparse.Namespace): Arguments from cmdline
    """
    if args.output_dir:
        build_dir = args.output_dir
    else:
        base_dir = settings.get('build_dir', '/tmp/b')
        build_dir = f'{base_dir}/{args.board}'
    log_path = os.path.join(build_dir, 'test-log.html')
    if not os.path.exists(log_path):
        return

    try:
        with open(log_path) as fh:
            text = fh.read()
    except OSError:
        return

    # Check for common QEMU failures
    if 'Lab failure' in text or 'Marking connection bad' in text:
        # Look for QEMU error in the log
        import html as html_mod
        plain = re.sub(r'<[^>]+>', '\n', text)
        plain = html_mod.unescape(plain)
        for line in plain.splitlines():
            line = line.strip()
            if ('qemu' in line.lower() and
                    ('not found' in line or 'No such file' in line or
                     'No machine' in line or 'unsupported' in line or
                     'error' in line.lower())):
                tout.notice(f'Hint: {line}')
                if 'unsupported' in line or 'No machine' in line:
                    tout.notice(
                        'Try: uman setup qemu-build')
                return
            if 'Could not open' in line or 'Cannot open' in line:
                tout.notice(f'Hint: {line}')
                return
        tout.notice('Hint: QEMU may have failed to start; check '
                    f'{log_path}')


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

    # Skip check if binary contains unexpanded shell variables
    if '$' in binary:
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

    board_id = args.test_py_id or get_board_test_id(args.board)
    cmd.extend(['--id', board_id])

    # Build test spec from user args and gitlab defaults
    spec_parts = []
    if args.test_spec:
        # Convert Class:method or Class::method to "Class and method" for -k
        spec = ' '.join(args.test_spec)
        spec = spec.replace('::', ' and ').replace(':', ' and ')
        spec_parts.append(spec)

    # Add gitlab TEST_PY_TEST_SPEC as default filter
    gitlab_spec = get_board_test_spec(args.board)
    if gitlab_spec:
        spec_parts.append(f'({gitlab_spec})')

    if spec_parts:
        cmd.extend(['-k', ' and '.join(spec_parts)])

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
    gdb_channel = args.gdbserver or (
        'localhost:1234' if args.gdb_phase else None)
    if gdb_channel:
        cmd.extend(['--gdbserver', gdb_channel])
    if args.exitfirst:
        cmd.append('-x')
    if not args.flattree_too and has_no_full():
        cmd.append('--no-full')
    if args.malloc_dump:
        cmd.extend(['--malloc-dump', args.malloc_dump])
    if args.leak_check:
        cmd.append('--leak-check')

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
    cfg_name = f'conf.{board}_{board_id}'
    cfg = os.path.join(hooks_bin, hostname, cfg_name)

    # Resolve symlinks
    if os.path.islink(cfg):
        cfg = os.path.realpath(cfg)

    # Fall back to travis-ci directory when hostname dir is missing
    if not os.path.exists(cfg):
        cfg = os.path.join(hooks_bin, HOOKS_FALLBACK, cfg_name)

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


def gdb_monitor(gdb_cmd, channel):
    """Run GDB and auto-reconnect when the remote connection closes

    Runs GDB in a pseudo-terminal to monitor its output. When
    'Remote connection closed' appears, automatically sends reconnect
    and continue commands so the debug session resumes after U-Boot
    restarts.

    Args:
        gdb_cmd (list of str): GDB command and arguments
        channel (str): Remote channel (e.g. 'localhost:1234')

    Returns:
        int: GDB exit code
    """
    import fcntl  # pylint: disable=import-outside-toplevel
    import pty  # pylint: disable=import-outside-toplevel
    import select  # pylint: disable=import-outside-toplevel
    import signal  # pylint: disable=import-outside-toplevel
    import termios  # pylint: disable=import-outside-toplevel
    import tty  # pylint: disable=import-outside-toplevel

    master_fd, slave_fd = pty.openpty()

    # Copy host terminal size to pty
    try:
        winsz = fcntl.ioctl(sys.stdout, termios.TIOCGWINSZ, b'\0' * 8)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsz)
    except OSError:
        pass

    proc = subprocess.Popen(
        gdb_cmd, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        preexec_fn=os.setsid, close_fds=True)
    os.close(slave_fd)

    # Forward terminal resizes to GDB
    orig_winch = signal.getsignal(signal.SIGWINCH)

    def on_winch(signo, frame):  # pylint: disable=unused-argument
        try:
            winsz = fcntl.ioctl(sys.stdout, termios.TIOCGWINSZ, b'\0' * 8)
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsz)
            os.kill(proc.pid, signal.SIGWINCH)
        except OSError:
            pass

    signal.signal(signal.SIGWINCH, on_winch)

    old_attr = termios.tcgetattr(sys.stdin)
    try:
        tty.setraw(sys.stdin)

        buf = b''
        trigger = b'Remote connection closed'
        reconnect = f'target remote {channel}\nc\n'.encode()

        while True:
            try:
                rlist = select.select(
                    [sys.stdin, master_fd], [], [], 0.5)[0]
            except (InterruptedError, select.error):
                continue

            if not rlist and proc.poll() is not None:
                break

            if sys.stdin in rlist:
                try:
                    data = os.read(sys.stdin.fileno(), 1024)
                except OSError:
                    break
                if not data:
                    break
                os.write(master_fd, data)

            if master_fd in rlist:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                os.write(sys.stdout.fileno(), data)

                buf += data
                if trigger in buf:
                    buf = b''
                    time.sleep(0.5)
                    os.write(master_fd, reconnect)
                elif len(buf) > 1024:
                    buf = buf[-512:]

        proc.wait()
        return proc.returncode
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_attr)
        signal.signal(signal.SIGWINCH, orig_winch)
        os.close(master_fd)


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

    if not args.dry_run and not os.path.exists(uboot_exe):
        tout.error(f'U-Boot executable not found: {uboot_exe}')
        return 1

    # Get gdbserver channel
    channel = getattr(args, 'gdbserver', None) or 'localhost:1234'

    # Build gdb command
    gdb_cmd = [
        'gdb-multiarch',
        '-q',  # Quiet mode (suppress startup banner)
        uboot_exe,
        '-iex', 'set auto-load safe-path /',  # Auto-load .gdbinit
        '-iex', 'set debuginfod enabled off',  # Disable debuginfod prompts
        '-iex', 'set sysroot',  # Suppress remote file transfer warnings
        '-iex', 'handle SIGUSR2 nostop noprint pass',  # Used by sandbox coroutines
        '-ex', f'target remote {channel}',
    ]
    for extra in args.gdb_cmd:
        gdb_cmd.extend(['-ex', extra])
    if args.bt:
        gdb_cmd.extend(['-ex', 'bt', '-ex', 'quit'])

    if args.dry_run:
        print(' '.join(gdb_cmd))
        return 0

    return gdb_monitor(gdb_cmd, channel)


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
    if not args.flattree_too and has_no_full():
        cmd.append('--no-full')

    if args.test_spec:
        spec = ' '.join(args.test_spec)
        cmd.extend(['-k', spec])

    result = command.run_pipe([cmd], capture=True, capture_stderr=True,
                              raise_on_error=False)
    if result.return_code != 0:
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
    if not args.flattree_too and has_no_full():
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
        cmd = [build_mod.get_buildman()] + build_mod.base_bm_args(
            args.board, build_dir, args.lto)
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
        found = False
        for label, pattern in [('QEMU', 'qemu'), ('MicroBlaze', 'mbv'),
                                ('m68k', 'M5208'),
                                ('sandbox', 'sandbox')]:
            boards = list_boards_by_pattern(pattern)
            if boards:
                tout.notice(f'Available {label} boards:')
                for board in boards:
                    print(f'  {board}')
                found = True
            elif not found:
                # First pattern failed — database is probably empty
                tout.warning(
                    'No boards found (check ~/.buildman and $UBOOT_TOOLS)')
                break
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

    # Handle --bt / --gdb-cmd implying -G
    if (args.bt or args.gdb_cmd) and not args.gdb:
        args.gdb = True

    # Handle -G: set gdb_phase if not already set
    if args.gdb and not args.gdb_phase:
        args.gdb_phase = 'u-boot'

    # Build with um if requested, rather than letting pytest do it
    if args.build:
        # Combine user adjust_cfg with gitlab OVERRIDE values
        adjust_cfg = list(args.adjust_cfg) if args.adjust_cfg else []
        gitlab_override = get_board_override(args.board)
        for cfg in gitlab_override:
            if cfg not in adjust_cfg:
                adjust_cfg.append(cfg)

        pytest_vars = pytest_env(args.board, args.test_py_id)
        if args.env:
            for item in args.env:
                key, _, val = item.partition('=')
                pytest_vars[key] = val
        if not build_mod.build_board(
                args.board, args.dry_run, args.lto,
                adjust_cfg=adjust_cfg,
                force_reconfig=args.force_reconfig, fresh=args.fresh,
                jobs=args.jobs, trace=args.trace,
                trace_early=not args.no_trace_early,
                output_dir=args.output_dir, extra_env=pytest_vars):
            return 1
        args.build = False  # Don't build again in pytest
    else:
        pytest_vars = pytest_env(args.board, args.test_py_id)
        if args.env:
            for item in args.env:
                key, _, val = item.partition('=')
                pytest_vars[key] = val

    # Show -G command hint when using -g (not in dry-run mode)
    if args.gdb_phase and not args.gdb and not args.dry_run:
        tout.notice(f'In another terminal: um py -G -B {args.board}')
    cmd = build_pytest_cmd(args)

    env = os.environ.copy()
    env.update(pytest_vars)

    # Handle -G: just launch gdb to connect to existing gdbserver
    if args.gdb:
        return run_with_gdb(args)

    start_time = time.time()
    result = exec_cmd(cmd, args.dry_run, env=env, capture=False)
    elapsed = time.time() - start_time

    if result is None:  # dry-run
        qemu_cmd = get_qemu_command(board, args)
        if qemu_cmd:
            print(qemu_cmd)
        return 0

    if result.return_code != 0:
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        if not args.quiet:
            tout.error('pytest failed')
            show_pytest_hint(args)
    else:
        if not args.quiet:
            tout.notice('pytest passed')

    # Show leak summary from test log if leak-check was enabled
    if args.leak_check:
        if args.output_dir:
            build_dir = args.output_dir
        else:
            base_dir = settings.get('build_dir', '/tmp/b')
            build_dir = f'{base_dir}/{args.board}'
        log_path = os.path.join(build_dir, 'test-log.html')
        if os.path.exists(log_path):
            import html
            with open(log_path) as fh:
                text = fh.read()
            # Strip HTML tags first, then unescape entities
            text = re.sub(r'<[^>]+>', '\n', text)
            text = html.unescape(text)
            res = parse_results(text)
            if res and res.leaked:
                show_summary(res.passed, res.failed, res.skipped, elapsed,
                             res.leaked, res.leak_bytes)
                if res.leak_top and args.show_leaks:
                    show_leak_top(res.leak_top, args.show_leaks)
    return result.return_code
