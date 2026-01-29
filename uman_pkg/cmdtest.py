# SPDX-License-Identifier: GPL-2.0+
# Copyright 2025 Canonical Ltd
# Written by Simon Glass <simon.glass@canonical.com>

"""Test command for running U-Boot sandbox tests

This module handles the 'test' subcommand which runs U-Boot's unit tests
in sandbox.
"""

from collections import namedtuple
import os
import re
import shlex
import struct
import time

# pylint: disable=import-error
from u_boot_pylib import command
from u_boot_pylib import terminal
from u_boot_pylib import tout

from uman_pkg import build, settings
from uman_pkg.util import run_pytest, show_summary

# Named tuple for test result counts
TestCounts = namedtuple('TestCounts', ['passed', 'failed', 'skipped'])

# Patterns for parsing linker-list symbols from nm output
# Format: _u_boot_list_2_ut_<suite>_2_<test>
RE_TEST_ALL = re.compile(r'_u_boot_list_2_ut_(\w+?)_2_(\w+)')
RE_TEST_SUITE = r'_u_boot_list_2_ut_{}_2_(\w+)'

# Pattern for parsing .data.rel.ro section from readelf output
RE_DATA_REL_RO = re.compile(
    r'\.data\.rel\.ro\s+PROGBITS\s+([0-9a-f]+)\s+([0-9a-f]+)')

# Patterns for parsing test output
RE_TEST_NAME = re.compile(r'Test:\s*(\S+)')
RE_RESULT = re.compile(r'Result:\s*(PASS|FAIL|SKIP):?\s+(\S+)')

# Unit test flags from include/test/test.h
UTF_FLAT_TREE = 0x08
UTF_LIVE_TREE = 0x10
UTF_DM = 0x80


def get_sandbox_path():
    """Get path to the sandbox U-Boot executable

    Returns:
        str: Path to sandbox u-boot, or None if not found
    """
    build_dir = settings.get('build_dir', '/tmp/b')
    sandbox_path = os.path.join(build_dir, 'sandbox', 'u-boot')
    if os.path.exists(sandbox_path):
        return sandbox_path
    return None


def get_section_info(sandbox):
    """Get .data.rel.ro section address and file offset

    Args:
        sandbox (str): Path to sandbox executable

    Returns:
        tuple: (section_addr, section_offset) or (None, None) if not found
    """
    result = command.run_one('readelf', '-S', sandbox, capture=True)
    match = RE_DATA_REL_RO.search(result.stdout)
    if match:
        return int(match.group(1), 16), int(match.group(2), 16)
    return None, None


def get_test_flags(sandbox, suite):
    """Get flags for all tests in a suite by parsing the binary

    Reads the unit_test structs from the linker list to extract flags.

    struct unit_test {
        const char *file;     // offset 0
        const char *name;     // offset 8
        int (*func)();        // offset 16
        int flags;            // offset 24
        ...
    };

    Args:
        sandbox (str): Path to sandbox executable
        suite (str): Suite name to get flags for

    Returns:
        list: List of (test_name, flags) tuples
    """
    # Get symbol addresses
    result = command.run_one('nm', sandbox, capture=True)
    pattern = rf'([0-9a-f]+) D _u_boot_list_2_ut_{suite}_2_(\w+)'
    tests = re.findall(pattern, result.stdout)

    if not tests:
        return []

    section_addr, section_offset = get_section_info(sandbox)
    if section_addr is None:
        return []

    test_flags = []
    with open(sandbox, 'rb') as fh:
        for addr_str, name in tests:
            addr = int(addr_str, 16)
            file_offset = section_offset + (addr - section_addr)
            fh.seek(file_offset)
            data = fh.read(28)
            if len(data) < 28:
                continue
            _, _, _, flags = struct.unpack('<QQQI', data)
            test_flags.append((name, flags))

    return test_flags


def predict_test_count(sandbox, suite, full=False):
    """Predict how many times tests will run

    Args:
        sandbox (str): Path to sandbox executable
        suite (str): Suite name
        full (bool): Whether running both live-tree and flat-tree tests

    Returns:
        int: Predicted number of test runs
    """
    test_flags = get_test_flags(sandbox, suite)
    if not test_flags:
        return 0

    count = 0
    for name, flags in test_flags:
        # Tests with UTF_FLAT_TREE only run on flat tree (skip unless full)
        if flags & UTF_FLAT_TREE:
            if full:
                count += 1
            continue

        # All other tests run once on live tree
        count += 1

        # Tests with UTF_DM run again on flat tree (only if full)
        if full and flags & UTF_DM and not flags & UTF_LIVE_TREE:
            # Video tests skip flattree (except video_base)
            if 'video' not in name or 'video_base' in name:
                count += 1

    return count


# Tests that require test_ut_dm_init to create data files
HOST_TESTS = ['cmd_host', 'host', 'host_dup']


def needs_dm_init(specs):
    """Check if tests require dm init data files

    Args:
        specs (list): List of (suite, pattern) tuples

    Returns:
        bool: True if dm init is needed
    """
    for suite, pattern in specs:
        # Check if running dm suite or all tests
        if suite in ('dm', 'all'):
            return True
        # Check for specific host tests
        if pattern:
            for host_test in HOST_TESTS:
                if host_test in pattern:
                    return True
    return False


def ensure_dm_init_files():
    """Ensure dm init data files exist, creating them if needed

    Returns:
        bool: True if files exist or were created successfully
    """
    build_dir = settings.get('build_dir', '/tmp/b')
    persistent_dir = os.path.join(build_dir, 'sandbox', 'persistent-data')
    test_file = os.path.join(persistent_dir, '2MB.ext2.img')

    if os.path.exists(test_file):
        return True

    tout.notice('Creating dm test data files...')
    if not run_pytest('test_ut_dm_init'):
        tout.error('Failed to create dm test data files')
        return False
    return True


def get_suites_from_nm(sandbox):
    """Get available test suites by parsing nm output

    Looks for symbols matching 'suite_end_<name>' pattern.

    Args:
        sandbox (str): Path to sandbox executable

    Returns:
        list: Sorted list of suite names
    """
    result = command.run_one('nm', sandbox, capture=True)
    suites = re.findall(r'\bsuite_end_(\w+)', result.stdout)
    return sorted(set(suites))


def get_tests_from_nm(sandbox, suite=None):
    """Get available tests by parsing nm output

    U-Boot uses linker lists to register unit tests. Each test creates a
    symbol with the pattern '_u_boot_list_2_ut_<suite>_2_<test>', where
    '_2_' represents the linker-list section separator.

    Args:
        sandbox (str): Path to sandbox executable
        suite (str): Optional suite name to filter tests

    Returns:
        list: Sorted list of (suite, test) tuples, e.g. [('dm', 'test_acpi')]
    """
    result = command.run_one('nm', sandbox, capture=True)
    if suite:
        matches = re.findall(RE_TEST_SUITE.format(suite), result.stdout)
        return sorted(set((suite, test) for test in matches))

    # Find all tests across all suites
    matches = RE_TEST_ALL.findall(result.stdout)
    return sorted(set(matches))


def parse_one_test(arg):
    """Parse a single test argument into (suite, pattern) tuple

    Args:
        arg (str): Test argument (suite, suite_test_name, "suite pattern",
                   test_name, ut_suite_testname, or partial_name for searching
                   all suites)

    Returns:
        tuple: (suite, pattern) where pattern may be None, or suite may be
               None to search all suites
    """
    parts = arg.split(None, 1)
    suite = parts[0]
    pattern = parts[1] if len(parts) > 1 else None

    # Strip ut_ prefix from pytest-style names (e.g. ut_bootstd_bootflow)
    # Format is ut_<suite>_<testname> where suite is first underscore-delimited
    if suite.startswith('ut_'):
        suite = suite[3:]
        # Split on first underscore: suite_testname -> (suite, testname)
        if '_' in suite and pattern is None:
            suite, pattern = suite.split('_', 1)
            return (suite, pattern)

    # Check for suite.test format
    if '.' in suite and pattern is None:
        suite, pattern = suite.split('.', 1)
    # Check for full test name: suite_test_name
    elif '_test_' in suite:
        suite, pattern = suite.split('_test_', 1)
    # Check for test name only: test_something -> search all suites
    elif suite.startswith('test_'):
        pattern = suite[5:]  # Strip 'test_' prefix
        suite = None
    # Check for partial test name containing underscore (e.g. ext4l_unlink)
    elif '_' in suite and pattern is None:
        pattern = suite
        suite = None

    return (suite, pattern)


def parse_test_specs(tests):
    """Parse test arguments into list of (suite, pattern) tuples

    Handles formats:
        - None or ['all'] -> [('all', None)]
        - ['dm'] -> [('dm', None)]
        - ['dm', 'video*'] -> [('dm', 'video*')]
        - ['dm video*'] -> [('dm', 'video*')]
        - ['log', 'lib'] -> [('log', None), ('lib', None)]
        - ['bloblist_test_blob'] -> [('bloblist', 'blob')]
        - ['dm.test_acpi'] -> [('dm', 'test_acpi')]

    Args:
        tests (list): Test arguments from command line

    Returns:
        list: List of (suite, pattern) tuples
    """
    if not tests or tests == ['all']:
        return [('all', None)]

    # Single arg
    if len(tests) == 1:
        return [parse_one_test(tests[0])]

    # Two args: could be suite+pattern or two suites/tests
    # If second arg contains glob chars, treat as pattern
    if len(tests) == 2 and any(c in tests[1] for c in '*?['):
        return [(tests[0], tests[1])]

    # Multiple suites or full test names
    return [parse_one_test(t) for t in tests]


def resolve_specs(sandbox, specs):
    """Resolve specs with suite=None or invalid suite by looking up from nm

    Args:
        sandbox (str): Path to sandbox executable
        specs (list): List of (suite, pattern) tuples

    Returns:
        tuple: (resolved_specs, unmatched_specs)
    """
    resolved = []
    unmatched = []
    all_tests = None  # Lazy load
    known_suites = None  # Lazy load

    for suite, pattern in specs:
        if suite is not None and suite != 'all':
            # Check if suite exists
            if known_suites is None:
                if all_tests is None:
                    all_tests = get_tests_from_nm(sandbox)
                known_suites = {s for s, _ in all_tests}

            if suite in known_suites:
                resolved.append((suite, pattern))
            else:
                # Suite doesn't exist - try to find full test name
                # Reconstruct the original test name
                if pattern:
                    full_name = f'{suite}_test_{pattern}'
                else:
                    full_name = suite
                found = False
                for test_suite, test_name in all_tests:
                    if test_name == full_name:
                        resolved.append((test_suite, full_name))
                        found = True
                        break
                if not found:
                    unmatched.append((suite, pattern))
        elif suite == 'all':
            resolved.append((suite, pattern))
        else:
            # Need to find suite(s) for this pattern
            if all_tests is None:
                all_tests = get_tests_from_nm(sandbox)
            found = False
            for test_suite, test_name in all_tests:
                if test_name.endswith(pattern):
                    resolved.append((test_suite, pattern))
                    found = True
                    break  # Only add first match
            if not found:
                unmatched.append((None, pattern))

    return resolved, unmatched


def validate_specs(sandbox, specs):
    """Check that each spec matches at least one test

    Args:
        sandbox (str): Path to sandbox executable
        specs (list): List of (suite, pattern) tuples

    Returns:
        list: List of unmatched specs (empty if all match)
    """
    if specs == [('all', None)]:
        return []

    all_tests = get_tests_from_nm(sandbox)
    unmatched = []

    for suite, pattern in specs:
        found = False
        for test_suite, test_name in all_tests:
            if test_suite != suite:
                continue
            if pattern is None:
                found = True
                break
            if test_name.endswith(pattern):
                found = True
                break
        if not found:
            unmatched.append((suite, pattern))

    return unmatched


def build_ut_cmd(sandbox, specs, full=False, verbose=False, legacy=False,
                 manual=False):
    """Build the sandbox command line for running tests

    Args:
        sandbox (str): Path to sandbox executable
        specs (list): List of (suite, pattern) tuples from parse_test_specs
        full (bool): Run both live-tree and flat-tree tests
        verbose (bool): Enable verbose test output
        legacy (bool): Legacy mode (don't use -E flag for older U-Boot)
        manual (bool): Force manual tests to run

    Returns:
        list: Command and arguments
    """
    cmd = [sandbox, '-T']

    # Add -F to skip flat-tree tests (live-tree only) unless full mode
    if not full:
        cmd.append('-F')

    # Add -v to sandbox to show test output
    if verbose:
        cmd.append('-v')

    # Build ut commands from specs; use -E to emit Result: lines
    # Flags must come before suite name
    flags = ''
    if not legacy:
        flags += '-E '
    if manual:
        flags += '-m '
    cmds = []
    for suite, pattern in specs:
        if pattern:
            ut_cmd = f'ut {flags}{suite} {pattern}'
        else:
            ut_cmd = f'ut {flags}{suite}'
        cmds.append(ut_cmd)

    cmd.extend(['-c', '; '.join(cmds)])
    return cmd


def show_result(status, name, col):
    """Print a test result if showing results

    Args:
        status (str): Result status (PASS, FAIL, SKIP)
        name (str): Test name
        col (terminal.Color): Color object for output
    """
    if status == 'PASS':
        color = terminal.Color.GREEN
    elif status == 'FAIL':
        color = terminal.Color.RED
    else:
        color = terminal.Color.YELLOW
    print(f'  {col.start(color)}{status}{col.stop()}: {name}')


def parse_legacy_results(output, show_results=False, col=None):
    """Parse legacy test output to extract results

    Handles old-style "Test: test_name ... ok/FAILED/SKIPPED" lines

    Args:
        output (str): Test output from sandbox
        show_results (bool): Print per-test results
        col (terminal.Color): Color object for output

    Returns:
        TestCounts or None: Counts of passed/failed/skipped, or None if none
    """
    passed = 0
    failed = 0
    skipped = 0

    for line in output.splitlines():
        name_match = RE_TEST_NAME.search(line)
        name = name_match.group(1) if name_match else None
        lower = line.lower()

        if '... ok' in lower:
            status = 'PASS'
            passed += 1
        elif '... failed' in lower:
            status = 'FAIL'
            failed += 1
        elif '... skipped' in lower:
            status = 'SKIP'
            skipped += 1
        else:
            continue
        if show_results and name:
            show_result(status, name, col)

    if not passed and not failed and not skipped:
        return None
    return TestCounts(passed, failed, skipped)


def parse_results(output, show_results=False, col=None):
    """Parse test output to extract results from Result: lines

    Args:
        output (str): Test output from sandbox
        show_results (bool): Print per-test results
        col (terminal.Color): Color object for output

    Returns:
        TestCounts or None: Counts of passed/failed/skipped, or None if none
    """
    passed = 0
    failed = 0
    skipped = 0

    for line in output.splitlines():
        result_match = RE_RESULT.match(line)
        if result_match:
            status, name = result_match.groups()
            if status == 'PASS':
                passed += 1
            elif status == 'FAIL':
                failed += 1
            elif status == 'SKIP':
                skipped += 1
            if show_results:
                show_result(status, name, col)

    if not passed and not failed and not skipped:
        return None
    return TestCounts(passed, failed, skipped)


def run_tests(sandbox, specs, args, col):  # pylint: disable=R0914
    """Run sandbox tests

    Args:
        sandbox (str): Path to sandbox executable
        specs (list): List of (suite, pattern) tuples from parse_test_specs
        args (argparse.Namespace): Arguments from cmdline
        col (terminal.Color): Color object for output

    Returns:
        int: Exit code from tests
    """
    # Ensure dm init data files exist if needed
    if needs_dm_init(specs) and not ensure_dm_init_files():
        return 1

    cmd = build_ut_cmd(sandbox, specs, full=args.flattree_too,
                       verbose=args.test_verbose, legacy=args.legacy,
                       manual=args.manual)

    if args.dry_run:
        tout.notice(shlex.join(cmd))
        return 0

    tout.info(f"Running: {shlex.join(cmd)}")

    # Set up environment with persistent data directory
    build_dir = settings.get('build_dir', '/tmp/b')
    persist_dir = os.path.join(build_dir, 'sandbox', 'persistent-data')
    env = os.environ.copy()
    env['U_BOOT_PERSISTENT_DATA_DIR'] = persist_dir

    start_time = time.time()
    try:
        result = command.run_one(*cmd, capture=True, env=env)
    except command.CommandExc as exc:
        # Tests may fail but still produce parseable output
        result = exc.result
        if result and isinstance(result.stdout, (bytes, bytearray)):
            result.to_output(False)
        if not result:
            tout.error(f'Command failed: {exc}')
            return 1
    elapsed = time.time() - start_time

    # Detect old U-Boot that doesn't understand -E or -F flags
    if 'failed while parsing option: -E' in result.stdout:
        tout.error('U-Boot does not support -E flag; use -L for legacy mode')
        return 1
    if 'failed while parsing option: -F' in result.stdout:
        tout.error('U-Boot does not support -F flag; use -f to run all tests')
        return 1

    # Parse results first to check for failures
    res = parse_results(result.stdout, show_results=args.results, col=col)
    if not res and args.legacy:
        res = parse_legacy_results(result.stdout, show_results=args.results,
                                   col=col)

    # Print output in verbose mode, if there are failures, or no results
    if result.stdout and not args.results:
        if args.test_verbose or (res and res.failed) or not res:
            # Skip U-Boot banner, show only test output
            in_tests = False
            for line in result.stdout.splitlines():
                if not in_tests:
                    if line.startswith(('Running ', 'Test: ', 'Missing ')):
                        in_tests = True
                if in_tests:
                    print(line)
    if res:
        show_summary(res.passed, res.failed, res.skipped, elapsed)
        return result.return_code

    # Check for crash (signal termination)
    ret = result.return_code
    sig = None
    if ret < 0:
        sig = -ret
    elif ret > 128:
        sig = ret - 128
    if sig:
        sig_names = {6: 'SIGABRT', 11: 'SIGSEGV', 15: 'SIGTERM'}
        sig_name = sig_names.get(sig, f'signal {sig}')
        os.system('tset')
        tout.error(f'Test crashed ({sig_name})')
        return ret

    tout.warning('No results detected (use -L for older U-Boot)')
    return 1


def do_test(args):  # pylint: disable=R0912
    """Handle test command - run U-Boot sandbox tests

    Args:
        args (argparse.Namespace): Arguments from cmdline

    Returns:
        int: Exit code
    """
    board = args.board or 'sandbox'

    # Build if requested
    if args.build:
        if not build.build_board(
                board, args.dry_run, lto=args.lto,
                adjust_cfg=args.adjust_cfg,
                force_reconfig=args.force_reconfig, fresh=args.fresh,
                jobs=args.jobs, trace=args.trace,
                trace_early=not args.no_trace_early,
                output_dir=args.output_dir):
            return 1

    sandbox = get_sandbox_path()
    if not sandbox:
        tout.error(f'Sandbox not found. Build first with: uman build {board}')
        return 1

    # Handle list suites
    if args.list_suites:
        suites = get_suites_from_nm(sandbox)
        tout.notice('Available test suites:')
        for suite in suites:
            print(f'  {suite}')
        return 0

    # Handle list tests
    if args.list_tests:
        suite = args.tests[0] if args.tests else None
        tests = get_tests_from_nm(sandbox, suite)
        if suite:
            tout.notice(f'Tests in suite "{suite}":')
        else:
            tout.notice('Available tests:')
        for suite_name, test_name in tests:
            print(f'  {suite_name}.{test_name}')
        return 0

    # Parse test specs
    specs = parse_test_specs(args.tests)

    # Resolve any specs that need suite lookup
    specs, unmatched = resolve_specs(sandbox, specs)
    if unmatched:
        for suite, pattern in unmatched:
            tout.error(f'No tests found matching: {pattern}')
        return 1

    # Validate that specs match actual tests
    unmatched = validate_specs(sandbox, specs)
    if unmatched:
        for suite, pattern in unmatched:
            if pattern:
                tout.error(f'No tests found matching: {suite}.{pattern}')
            else:
                tout.error(f'No tests found in suite: {suite}')
        return 1

    # Run tests
    return run_tests(sandbox, specs, args, args.col)
