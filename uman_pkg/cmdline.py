# SPDX-License-Identifier: GPL-2.0+
# Copyright 2025 Canonical Ltd
# Written by Simon Glass <simon.glass@canonical.com>

"""Handles parsing of uman arguments

Creates the argument parser and uses it to parse the arguments passed in
"""

import argparse
import os
import sys


def get_git_actions():
    """Get git actions from cmdgit module

    Returns:
        list: List of GitAction namedtuples
    """
    # pylint: disable=import-outside-toplevel
    from uman_pkg.cmdgit import GIT_ACTIONS
    return GIT_ACTIONS


def get_git_action_names():
    """Get set of all git action names (short and long)

    Returns:
        set: All valid action names for symlink detection
    """
    names = set()
    for action in get_git_actions():
        names.add(action.short)
        names.add(action.long)
    return names

# Aliases for subcommands
ALIASES = {
    'config': ['cfg'],
    'git': ['g'],
    'selftest': ['st'],
    'pytest': ['py'],
    'build': ['b'],
    'test': ['t'],
}


class ErrorCatchingArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that catches errors instead of exiting"""
    def __init__(self, **kwargs):
        self.exit_state = None
        self.catch_error = False
        super().__init__(**kwargs)

    def error(self, message):
        if self.catch_error:
            # Store message for potential use
            pass
        super().error(message)

    def exit(self, status=0, message=None):
        if self.catch_error:
            self.exit_state = True
            return
        super().exit(status, message)


def add_ci_subparser(subparsers):
    """Add the 'ci' subparser"""
    ci = subparsers.add_parser('ci', help='Push current branch to CI')

    # Help text only - choices shown with 'help' argument

    pytest_help = 'Enable PYTEST: to select a particular one: -p help'
    sjg_help = 'Enable SJG_LAB: to select a particular board: -l help'

    ci.add_argument('-0', '--null', action='store_true',
                    help='Set all CI vars to 0')
    ci.add_argument('-a', '--all', action='store_true',
                    help='Run all CI stages including lab')
    ci.add_argument('-d', '--dest', metavar='BRANCH', default=None,
                    help='Destination branch name (default: current branch)')
    ci.add_argument('-f', '--force', action='store_true',
                    help='Force push to remote branch')
    ci.add_argument('-l', '--sjg', nargs='?', const='1', default=None,
                    help=sjg_help)
    ci.add_argument('-m', '--merge', action='store_true',
                    help='Create merge request')
    ci.add_argument('-p', '--pytest', nargs='?', const='1', default=None,
                    help=pytest_help)
    ci.add_argument('-s', '--suites', action='store_true',
                    help='Enable SUITES')
    ci.add_argument('-t', '--test-spec', metavar='SPEC',
                    help="Override test spec (e.g. 'not sleep')")
    ci.add_argument('-w', '--world', action='store_true', help='Enable WORLD')
    return ci


def add_selftest_subparser(subparsers):
    """Add the 'selftest' subparser"""
    stest = subparsers.add_parser(
        'selftest', aliases=ALIASES['selftest'],
        help='Run uman functional tests')
    stest.add_argument(
        'testname', type=str, default=None, nargs='?',
        help='Specify the test to run')
    stest.add_argument(
        '-N', '--no-capture', action='store_true',
        help='Disable capturing of console output in tests')
    stest.add_argument(
        '-X', '--test-preserve-dirs', action='store_true',
        help='Preserve and display test-created directories')
    return stest


def add_build_opts(parser):
    """Add common build options to a parser

    Args:
        parser: Argument parser to add options to
    """
    parser.add_argument(
        '-a', '--adjust-cfg', action='append', metavar='CFG', dest='adjust_cfg',
        help='Adjust Kconfig setting (use with -b; can use multiple times)')
    parser.add_argument(
        '-f', '--force-reconfig', action='store_true',
        help='Force reconfiguration (use with -b)')
    parser.add_argument(
        '-F', '--fresh', action='store_true',
        help='Delete build dir before building (use with -b)')
    parser.add_argument(
        '-j', '--jobs', type=int, metavar='JOBS',
        help='Number of parallel jobs for build (use with -b)')
    parser.add_argument(
        '-L', '--lto', action='store_true',
        help='Enable LTO when building (use with -b)')
    parser.add_argument(
        '-o', '--output-dir', metavar='DIR', dest='output_dir',
        help='Override build directory (use with -b)')
    parser.add_argument(
        '-T', '--trace', action='store_true',
        help='Enable function tracing (use with -b)')
    parser.add_argument(
        '--no-trace-early', action='store_true', dest='no_trace_early',
        help='Disable TRACE_EARLY when using -T (use with -b)')


def add_pytest_subparser(subparsers):
    """Add the 'pytest' subparser"""
    pyt = subparsers.add_parser(
        'pytest', aliases=ALIASES['pytest'],
        help='Run pytest tests for U-Boot')
    pyt.add_argument(
        'test_spec', type=str, nargs='*',
        help="Test specification (e.g. 'test_dm', 'not sleep')")
    pyt.add_argument(
        '-b', '--build', action='store_true',
        help='Build U-Boot before running tests')
    pyt.add_argument(
        '-B', '--board', metavar='BOARD',
        help='Board name to test (required; use -l to list QEMU boards)')
    pyt.add_argument(
        '-c', '--show-cmd', action='store_true',
        help='Show QEMU command line without running tests')
    pyt.add_argument(
        '-C', '--c-test', action='store_true',
        help='Run just the C test part (assumes setup done with -SP)')
    pyt.add_argument(
        '--flattree-too', action='store_true',
        help='Run both live-tree and flat-tree tests (default: live-tree only)')
    pyt.add_argument(
        '--find', metavar='PATTERN',
        help='Find tests matching PATTERN and show full IDs')
    pyt.add_argument(
        '-g', action='store_const', const='localhost:1234', dest='gdbserver',
        help='Run sandbox under gdbserver at localhost:1234')
    pyt.add_argument(
        '-G', '--gdb', action='store_true',
        help='Run under gdbserver and launch gdb-multiarch connected to it')
    pyt.add_argument(
        '-l', '--list', action='store_true', dest='list_boards',
        help='List available QEMU boards')
    pyt.add_argument(
        '-P', '--persist', action='store_true',
        help='Persist test artifacts (do not clean up after tests)')
    pyt.add_argument(
        '-q', '--quiet', action='store_true',
        help='Quiet mode: only show build output, progress, and result')
    pyt.add_argument(
        '-s', '--show-output', action='store_true',
        help='Show all test output in real-time (pytest -s)')
    pyt.add_argument(
        '-S', '--setup-only', action='store_true',
        help='Run only fixture setup (create test images) without tests')
    pyt.add_argument(
        '-t', '--timing', type=float, nargs='?', const=0.1, default=None,
        metavar='SECS',
        help='Show test timing (default min: 0.1s)')
    pyt.add_argument(
        '--no-timeout', action='store_true',
        help='Disable test timeout')
    pyt.add_argument(
        '-x', '--exitfirst', action='store_true',
        help='Stop on first test failure')
    pyt.add_argument(
        '--pollute', metavar='TEST',
        help='Find which test pollutes TEST (causes it to fail)')
    pyt.add_argument(
        '--gdbserver', metavar='CHANNEL', dest='gdbserver',
        help='Run sandbox under gdbserver (e.g., localhost:5555)')
    add_build_opts(pyt)
    # extra_args is set by parse_args() when '--' is present
    pyt.set_defaults(extra_args=[])
    return pyt


def add_build_subparser(subparsers):
    """Add the 'build' subparser"""
    bld = subparsers.add_parser(
        'build', aliases=ALIASES['build'],
        help='Build U-Boot for a board')
    bld.add_argument(
        '-B', metavar='BOARD', dest='board_opt',
        help='Board name (alternative to positional; or set $b)')
    bld.add_argument(
        'board', nargs='?', metavar='BOARD',
        help='Board name to build')
    bld.add_argument('-a', '--adjust-cfg', action='append', metavar='CFG',
                     dest='adjust_cfg',
                     help='Adjust Kconfig setting (can use multiple times)')
    bld.add_argument('-f', '--force-reconfig', action='store_true',
                     help='Force reconfiguration')
    bld.add_argument('-E', '--werror', action='store_true',
                     help='Treat warnings as errors (KCFLAGS=-Werror)')
    bld.add_argument('--fail-on-warning', action='store_true',
                     help='Fail if build produces warnings')
    bld.add_argument('-F', '--fresh', action='store_true',
                     help='Delete build dir first')
    bld.add_argument('-g', '--debug', action='store_true',
                     help='Enable debug build (CC_OPTIMIZE_FOR_DEBUG)')
    bld.add_argument('-I', '--in-tree', action='store_true',
                     help='Build in source tree, not separate directory')
    bld.add_argument('-j', '--jobs', type=int, metavar='JOBS',
                     help='Number of parallel jobs (passed to make)')
    bld.add_argument('-L', '--lto', action='store_true', help='Enable LTO')
    bld.add_argument('-o', '--output-dir', metavar='DIR',
                     help='Override output directory')
    bld.add_argument('-O', '--objdump', action='store_true',
                     help='Write disassembly of u-boot and SPL ELFs')
    bld.add_argument('-s', '--size', action='store_true',
                     help='Show size of u-boot and SPL ELFs')
    bld.add_argument('-t', '--target', metavar='TARGET',
                     help='Build specific target (e.g. u-boot.bin)')
    bld.add_argument('-T', '--trace', action='store_true',
                     help='Enable function tracing (FTRACE=1)')
    bld.add_argument('--no-trace-early', action='store_true', dest='no_trace_early',
                     help='Disable TRACE_EARLY when using -T')
    bld.add_argument('--bisect', action='store_true',
                     help='Bisect to find first failing commit')
    bld.add_argument('--gprof', action='store_true',
                     help='Enable gprof profiling (GPROF=1)')
    return bld


def add_setup_subparser(subparsers):
    """Add the 'setup' subparser"""
    setup = subparsers.add_parser(
        'setup', help='Build firmware blobs needed for testing')
    setup.add_argument(
        'component', type=str, nargs='?', default=None,
        help="Component to build (e.g. 'opensbi'), or omit to build all")
    setup.add_argument(
        '-d', '--alias-dir', metavar='DIR',
        help='Directory for aliases symlinks (default: ~/bin)')
    setup.add_argument(
        '-f', '--force', action='store_true',
        help='Force rebuild even if already built')
    setup.add_argument(
        '-l', '--list', action='store_true', dest='list_components',
        help='List available components')
    return setup


def add_test_subparser(subparsers):
    """Add the 'test' subparser for running U-Boot sandbox tests"""
    test = subparsers.add_parser(
        'test', aliases=ALIASES['test'],
        help='Run U-Boot sandbox tests')
    test.add_argument(
        'tests', nargs='*', metavar='TEST',
        help='Test name(s) to run (e.g. "dm" or "env")')
    test.add_argument(
        '-b', '--build', action='store_true',
        help='Build before running tests')
    test.add_argument(
        '-B', '--board', metavar='BOARD', default='sandbox',
        help='Board to build/test (default: sandbox)')
    test.add_argument(
        '--flattree-too', action='store_true',
        help='Run both live-tree and flat-tree tests (default: live-tree only)')
    test.add_argument(
        '-l', '--list', action='store_true', dest='list_tests',
        help='List available tests')
    test.add_argument(
        '--legacy', action='store_true',
        help='Use legacy result parsing (for old U-Boot without Result: lines)')
    test.add_argument(
        '-m', '--manual', action='store_true',
        help='Force manual tests to run (tests with _norun suffix)')
    test.add_argument(
        '-r', '--results', action='store_true',
        help='Show per-test pass/fail status')
    test.add_argument(
        '-s', '--suites', action='store_true', dest='list_suites',
        help='List available test suites')
    test.add_argument(
        '-V', '--test-verbose', action='store_true', dest='test_verbose',
        help='Enable verbose test output')
    add_build_opts(test)
    return test


def add_git_subparser(subparsers):
    """Add the 'git' subparser for rebase helpers"""
    git = subparsers.add_parser(
        'git', aliases=['g'],
        help='Git rebase helpers')

    git.add_argument(
        '-a', '--aliases', action='store_true',
        help='Output shell alias definitions for eval')

    # Build choices and help from GIT_ACTIONS
    actions = get_git_actions()
    choices = []
    help_parts = []
    for action in actions:
        choices.extend([action.short, action.long])
        help_parts.append(f'{action.short}/{action.long}')

    git.add_argument(
        'action', nargs='?',
        choices=choices,
        metavar='ACTION',
        help=f"Action: {', '.join(help_parts)}")
    git.add_argument(
        'arg', nargs='?',
        help='Commit count (for gr/rf), patch number (for rp/rn), or ref (for sd)')
    git.add_argument(
        'extra', nargs='*',
        help='Additional arguments (e.g., file paths for rd)')
    return git


def add_config_subparser(subparsers):
    """Add the 'config' subparser"""
    cfg = subparsers.add_parser(
        'config', aliases=['cfg'],
        help='Examine U-Boot configuration')
    cfg.add_argument(
        '-B', '--board', metavar='BOARD',
        help='Board name (required; or set $b)')
    cfg.add_argument(
        '-g', '--grep', metavar='PATTERN',
        help='Grep .config for PATTERN (regex, case-insensitive)')
    cfg.add_argument(
        '-m', '--meld', action='store_true',
        help='Compare defconfig with meld (build cfg, savedefconfig, meld)')
    cfg.add_argument(
        '-s', '--sync', action='store_true',
        help='Resync defconfig from .config (build cfg, savedefconfig, copy)')
    cfg.add_argument(
        '--build-dir', metavar='DIR',
        help='Override build directory (default: /tmp/b/BOARD)')
    return cfg


def setup_parser():
    """Set up command-line parser

    Returns:
        argparse.Parser object
    """
    epilog = '''U-Boot development tool'''

    parser = ErrorCatchingArgumentParser(epilog=epilog)
    parser.add_argument(
        '-D', '--debug', action='store_true',
        help='Enable debugging (provides full traceback on error)')
    parser.add_argument(
        '-n', '--dry-run', action='store_true',
        help='Show what would be executed without running commands')
    parser.add_argument(
        '-v', '--verbose', action='store_true', dest='verbose', default=False,
        help='Verbose output')

    subparsers = parser.add_subparsers(dest='cmd', required=True)
    add_build_subparser(subparsers)
    add_ci_subparser(subparsers)
    add_config_subparser(subparsers)
    add_git_subparser(subparsers)
    add_selftest_subparser(subparsers)
    add_pytest_subparser(subparsers)
    add_setup_subparser(subparsers)
    add_test_subparser(subparsers)

    return parser


def parse_args(argv=None, prog_name=None):
    """Parse command line arguments from sys.argv[]

    Args:
        argv (str or None): Arguments to process, or None to use sys.argv[1:]
        prog_name (str or None): Program name for symlink detection, or None
            to use sys.argv[0]

    Returns:
        argparse.Namespace: Parsed arguments
    """
    parser = setup_parser()

    if argv is None:
        argv = sys.argv[1:]

    # Check if invoked via symlink matching a git action name or other shortcut
    if prog_name is None:
        prog_name = sys.argv[0] if sys.argv else ''
    invoked_as = os.path.basename(prog_name)
    if invoked_as in get_git_action_names():
        argv = ['git', invoked_as] + list(argv)
    elif invoked_as == 'cg':
        argv = ['config', '-g'] + list(argv)

    # Handle '--' separator for extra pytest arguments
    extra_args = []
    if '--' in argv:
        idx = argv.index('--')
        extra_args = argv[idx + 1:]
        argv = argv[:idx]

    args = parser.parse_args(argv)

    # Set extra_args for pytest command
    if hasattr(args, 'extra_args'):
        args.extra_args = extra_args

    # Resolve aliases
    for full, aliases in ALIASES.items():
        if args.cmd in aliases:
            args.cmd = full

    return args
