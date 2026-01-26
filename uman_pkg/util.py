# SPDX-License-Identifier: GPL-2.0+
# Copyright 2025 Canonical Ltd
# Written by Simon Glass <simon.glass@canonical.com>

"""Utility functions for uman

This module provides common utility functions used across uman modules.
"""

import os
import shlex
import subprocess
import sys

# pylint: disable=import-error
from u_boot_pylib import command
from u_boot_pylib import terminal
from u_boot_pylib import tout

from uman_pkg import settings


def get_uboot_dir():
    """Get the U-Boot source directory

    Checks if current directory is a U-Boot tree, otherwise uses $USRC.

    Returns:
        str: Path to U-Boot source directory, or None if not found
    """
    # Check if current directory is a U-Boot tree
    if os.path.exists('./test/py/test.py'):
        return os.getcwd()

    # Try USRC environment variable
    usrc = os.environ.get('USRC')
    if usrc and os.path.exists(os.path.join(usrc, 'test/py/test.py')):
        return usrc

    return None


def setup_uboot_dir():
    """Find and change to the U-Boot source directory

    Returns:
        str: Path to U-Boot source directory, or None if not found
    """
    uboot_dir = get_uboot_dir()
    if not uboot_dir:
        tout.error('Not in a U-Boot tree and $USRC not set')
        return None

    if uboot_dir != os.getcwd():
        tout.info(f'Changing to U-Boot directory: {uboot_dir}')
        os.chdir(uboot_dir)

    return uboot_dir


def exec_cmd(cmd, dry_run=False, env=None, capture=True):
    """Run a command or show what would be run in dry-run mode

    Args:
        cmd (list): Command to run
        dry_run (bool): If True, just show command without running
        env (dict): Optional environment variables to set
        capture (bool): Whether to capture output (default True). When False,
            runs interactively with proper Ctrl+C handling.

    Returns:
        CommandResult or None: Result if run, None if dry-run
    """
    if dry_run:
        # Build command with env vars that differ from current environment
        env_prefix = ''
        if env:
            env_parts = [f"{k}={shlex.quote(v)}" for k, v in env.items()
                         if os.environ.get(k) != v]
            if env_parts:
                env_prefix = ' '.join(env_parts) + ' '
        tout.notice(f"{env_prefix}{shlex.join(cmd)}")
        return None

    tout.info(f"Running: {shlex.join(cmd)}")

    # For interactive commands (capture=False), use subprocess.run directly
    # so Ctrl+C is properly forwarded to the child process. Capture stderr
    # so callers can check for specific errors, but also print it.
    if not capture:
        result = subprocess.run(cmd, env=env, check=False,
                                stderr=subprocess.PIPE)
        stderr = ''
        if result.stderr:
            stderr = result.stderr.decode('utf-8', errors='replace')
            print(stderr, file=sys.stderr, end='')
        return command.CommandResult(return_code=result.returncode,
                                     stdout='', stderr=stderr)

    return command.run_pipe([cmd], env=env, capture=capture,
                            capture_stderr=True, raise_on_error=False)


def run_pytest(test_name, board='sandbox', build_dir=None, quiet=True,
               dry_run=False):
    """Run a pytest test

    Args:
        test_name (str): Test to run (e.g. 'test_ut_dm_init')
        board (str): Board name (default: 'sandbox')
        build_dir (str): Build directory, or None to use default
        quiet (bool): If True, capture output; if False, show output
        dry_run (bool): If True, just show command without running

    Returns:
        bool: True if test passed (or dry-run), False otherwise
    """
    uboot_dir = get_uboot_dir()
    if not uboot_dir:
        tout.error('Not in a U-Boot tree and $USRC not set')
        return False

    if not build_dir:
        base_dir = settings.get('build_dir', '/tmp/b')
        build_dir = os.path.join(base_dir, board)

    pytest_cmd = [
        './test/py/test.py', '-B', board, '--build-dir', build_dir,
        '--buildman', '-q', '-k', test_name,
    ]

    orig_dir = os.getcwd()
    os.chdir(uboot_dir)
    try:
        result = exec_cmd(pytest_cmd, dry_run=dry_run, capture=quiet)
        if result is None:  # dry-run
            return True
        if result.return_code:
            if quiet:
                tout.error(f'pytest {test_name} failed')
                tout.error(result.stderr)
            return False
    finally:
        os.chdir(orig_dir)

    return True


def git_output(*args):
    """Run a git command and return its output

    Args:
        *args: Arguments to pass to git (e.g., 'status', '-sb')

    Returns:
        str: Command output (stdout), stripped of trailing whitespace

    Raises:
        command.CommandExc: If the command fails
    """
    result = command.run_pipe([['git'] + list(args)], capture=True,
                              raise_on_error=True, binary=True)
    return result.stdout.decode('utf-8', errors='replace').strip()


def git_output_quiet(*args):
    """Run a git command and return its output, suppressing stderr

    Like git_output() but captures and discards stderr to avoid error
    messages being printed when commands fail expectedly.

    Args:
        *args: Arguments to pass to git (e.g., 'rev-parse', '--abbrev-ref')

    Returns:
        str: Command output (stdout), stripped of trailing whitespace

    Raises:
        command.CommandExc: If the command fails
    """
    result = command.run_pipe([['git'] + list(args)], capture=True,
                              capture_stderr=True, raise_on_error=True)
    return result.stdout.strip()


def git(*args, env=None, dry_run=False):
    """Run a git command, capturing output

    Args:
        *args: Arguments to pass to git (e.g., 'rebase', '-i', 'HEAD~3')
        env (dict): Optional environment variables
        dry_run (bool): If True, just show command without running

    Returns:
        CommandResult or None: Result with return_code, stdout, stderr,
            or None if dry_run
    """
    cmd = ['git'] + list(args)
    return exec_cmd(cmd, dry_run=dry_run, env=env, capture=True)


def format_duration(seconds):
    """Format a duration in seconds as a human-readable string

    Args:
        seconds (float): Duration in seconds

    Returns:
        str: Formatted duration (e.g., "1.23s", "1m 23s")
    """
    if seconds < 60:
        return f'{seconds:.2f}s'
    minutes = int(seconds // 60)
    secs = seconds % 60
    return f'{minutes}m {secs:.1f}s'


def show_summary(passed, failed, skipped, elapsed):
    """Show a test results summary

    Args:
        passed (int): Number of tests passed
        failed (int): Number of tests failed
        skipped (int): Number of tests skipped
        elapsed (float): Time taken in seconds
    """
    col = terminal.Color()
    green = col.start(terminal.Color.GREEN)
    red = col.start(terminal.Color.RED)
    yellow = col.start(terminal.Color.YELLOW)
    reset = col.stop()
    print(f'Results: {green}{passed} passed{reset}, '
          f'{red}{failed} failed{reset}, '
          f'{yellow}{skipped} skipped{reset} in '
          f'{format_duration(elapsed)}')
