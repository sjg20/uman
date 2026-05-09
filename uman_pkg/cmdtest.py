# SPDX-License-Identifier: GPL-2.0+
# Copyright 2025 Canonical Ltd
# Written by Simon Glass <simon.glass@canonical.com>

"""Test command for running U-Boot sandbox tests

This module handles the 'test' subcommand which runs U-Boot's unit tests
in sandbox.
"""

from collections import namedtuple
import fnmatch
import os
import re
import shlex
import struct
import sys
import time

# pylint: disable=import-error
from u_boot_pylib import command
from u_boot_pylib import terminal
from u_boot_pylib import tout

from uman_pkg import build, settings
from uman_pkg.util import format_bytes, run_pytest, show_leak_top, show_summary

# Named tuple for test result counts
TestCounts = namedtuple('TestCounts', ['passed', 'failed', 'skipped',
                                       'leaked', 'leak_bytes', 'leak_top'],
                        defaults=[0, 0, None])

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
RE_SUMMARY = re.compile(r'Tests run:\s*(\d+),.*failures:\s*(\d+)')
RE_TEST_FAILED = re.compile(r"Test '.+' failed \d+ times")
RE_LEAK = re.compile(r'Leak:\s+(\d+)\s+alloc')
RE_LEAK_DETAIL = re.compile(r'\s+[0-9a-f]+\s+([0-9a-f]+)\s+(\S+:\d+.*)')

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


def resolve_one(suite, pattern, all_tests, known_suites):
    """Resolve a single (suite, pattern) spec against known tests

    Args:
        suite (str or None): Suite name, 'all', or None to search
        pattern (str or None): Test pattern or None for whole suite
        all_tests (list): List of (suite, test_name) from nm
        known_suites (set): Set of known suite names

    Returns:
        tuple: (resolved_list, matched) where resolved_list is a list of
            (suite, pattern) tuples and matched is True if something matched
    """
    if suite == 'all' or suite in known_suites:
        return [(suite, pattern)], True

    if suite is not None:
        # Suite doesn't exist - try to find full test name
        if pattern:
            full_name = f'{suite}_test_{pattern}'
        else:
            full_name = suite
        for test_suite, test_name in all_tests:
            if test_name == full_name:
                return [(test_suite, full_name)], True

        # Try as a pattern across all suites: collect actual matching test
        # names. Constructing a glob like '{suite}_test_{search}*' would
        # miss tests whose name does not start with that prefix (e.g.
        # 'bootstd_test_bootflow_efi' for search='efi') and tests whose
        # suite name happens to contain the search term.
        if pattern is None:
            matches = []
            for test_suite, test_name in all_tests:
                if fnmatch.fnmatch(test_name, f'*{suite}*'):
                    matches.append((test_suite, test_name))
            if matches:
                return sorted(matches), True
        return [], False

    # suite is None - search all suites for this pattern
    for test_suite, test_name in all_tests:
        if fnmatch.fnmatch(test_name, f'*{pattern}'):
            return [(test_suite, pattern)], True
    return [], False


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
    all_tests = None
    known_suites = None

    for suite, pattern in specs:
        if suite not in (None, 'all'):
            if known_suites is None:
                all_tests = get_tests_from_nm(sandbox)
                known_suites = {s for s, _ in all_tests}
        elif suite is None and all_tests is None:
            all_tests = get_tests_from_nm(sandbox)
            known_suites = {s for s, _ in all_tests}

        found, matched = resolve_one(suite, pattern,
                                     all_tests or [], known_suites or set())
        resolved.extend(found)
        if not matched:
            unmatched.append((suite, pattern))

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
            if fnmatch.fnmatch(test_name, f'*{pattern}'):
                found = True
                break
        if not found:
            unmatched.append((suite, pattern))

    return unmatched


def has_no_flat():
    """Check whether the U-Boot tree supports the -F sandbox flag

    Looks for 'noflat' in arch/sandbox/cpu/start.c in the current directory.

    Returns:
        bool: True if -F is supported
    """
    start_c = os.path.join('arch', 'sandbox', 'cpu', 'start.c')
    try:
        with open(start_c, encoding='utf-8') as inf:
            return 'noflat' in inf.read()
    except OSError:
        return False


def has_emit_result():
    """Check whether the U-Boot tree supports the -E ut flag

    Looks for 'emit_result' in test/cmd_ut.c in the current directory.

    Returns:
        bool: True if -E is supported
    """
    cmd_ut = os.path.join('test', 'cmd_ut.c')
    try:
        with open(cmd_ut, encoding='utf-8') as inf:
            return 'emit_result' in inf.read()
    except OSError:
        return False


def build_ut_cmd(sandbox, specs, full=False, verbose=False, legacy=False,
                 manual=False, malloc_dump=None, leak_check=False):
    """Build the sandbox command line for running tests

    Args:
        sandbox (str): Path to sandbox executable
        specs (list): List of (suite, pattern) tuples from parse_test_specs
        full (bool): Run both live-tree and flat-tree tests
        verbose (bool): Enable verbose test output
        legacy (bool): Legacy mode (don't use -E flag for older U-Boot)
        manual (bool): Force manual tests to run
        malloc_dump (str or None): File to write malloc dump to on exit
        leak_check (bool): Check for memory leaks around each test

    Returns:
        list: Command and arguments
    """
    cmd = [sandbox, '-T']

    if malloc_dump:
        cmd.extend(['--malloc_dump', malloc_dump.replace('%d', '0')])

    # Add -F to skip flat-tree tests (live-tree only) unless full mode
    if not full and has_no_flat():
        cmd.append('-F')

    # Add -v to sandbox to show test output
    if verbose:
        cmd.append('-v')

    # Build ut commands from specs; use -E to emit Result: lines
    # Flags must come before suite name
    flags = ''
    if not legacy and has_emit_result():
        flags += '-E '
    if manual:
        flags += '-m '
    if leak_check:
        flags += '-L '
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
        if not name_match:
            continue
        name = name_match.group(1)
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


def parse_summary(output):
    """Parse 'Tests run:' summary line from test output

    Handles the format: Tests run: N, Xms, average: Xms, failures: N

    Args:
        output (str): Test output from sandbox

    Returns:
        TestCounts or None: Counts of passed/failed/skipped, or None if none
    """
    for line in output.splitlines():
        match = RE_SUMMARY.match(line)
        if match:
            total = int(match.group(1))
            failed = int(match.group(2))
            return TestCounts(total - failed, failed, 0)
    return None


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
    leaked = 0
    leak_bytes = 0
    cur_test = None
    cur_leak_bytes = 0
    cur_details = []
    leak_top = []

    def flush_leak():
        if cur_leak_bytes and cur_test:
            leak_top.append((cur_leak_bytes, cur_test, cur_details))

    for line in output.splitlines():
        test_match = RE_TEST_NAME.match(line)
        if test_match:
            flush_leak()
            cur_test = test_match.group(1)
            cur_leak_bytes = 0
            cur_details = []
            continue
        if RE_LEAK.match(line):
            leaked += 1
            continue
        detail = RE_LEAK_DETAIL.match(line)
        if detail:
            nbytes = int(detail.group(1), 16)
            leak_bytes += nbytes
            cur_leak_bytes += nbytes
            cur_details.append((nbytes, detail.group(2)))
            continue
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
            flush_leak()
            cur_leak_bytes = 0
            cur_details = []
    flush_leak()

    if not passed and not failed and not skipped and not leaked:
        return None
    leak_top.sort(reverse=True)
    return TestCounts(passed, failed, skipped, leaked, leak_bytes,
                      leak_top)


def count_tests(sandbox, specs):
    """Count expected tests for the given specs

    Args:
        sandbox (str): Path to sandbox executable
        specs (list): List of (suite, pattern) tuples

    Returns:
        int: Number of matching tests, or 0 if unknown
    """
    total = 0
    for suite, pattern in specs:
        if suite == 'all':
            return len(get_tests_from_nm(sandbox))
        tests = get_tests_from_nm(sandbox, suite)
        if pattern:
            total += sum(1 for _, name in tests if name.endswith(pattern))
        else:
            total += len(tests)
    return total


class Progress:
    """Show live test progress on stderr

    Parses sandbox output as it arrives and displays a running count of
    passed/failed/skipped tests, updating in place with carriage return.

    With -E (emit_result=True): counts Result: PASS/FAIL/SKIP lines.
    Without -E: counts Test: lines as passes, detects failure lines like
    "Test '<name>' failed N times" to adjust the count.
    """

    def __init__(self, emit_result, total=0):
        # emit_result kept for API compatibility but no longer drives the
        # counting strategy: Progress switches to Result: lines as soon as
        # one is seen, regardless of has_emit_result().
        self.emit = emit_result
        self.total = total
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.leaked = 0
        self.leak_bytes = 0
        self.buf = ''
        self.pending = False  # A Test: line seen, not yet resolved
        self.has_result = False  # True once a Result: line has been seen

    def _show(self):
        """Print the progress line, overwriting the previous one"""
        col = terminal.Color()
        grn = col.start(terminal.Color.GREEN)
        red = col.start(terminal.Color.RED)
        yel = col.start(terminal.Color.YELLOW)
        rst = col.stop()
        done = self.passed + self.failed + self.skipped
        if self.total:
            hdr = f'{done}/{self.total}:'
        else:
            hdr = f'{done}:'
        mag = col.start(terminal.Color.MAGENTA)
        parts = [f'{grn}{self.passed} passed{rst}']
        if self.failed:
            parts.append(f'{red}{self.failed} failed{rst}')
        if self.skipped:
            parts.append(f'{yel}{self.skipped} skipped{rst}')
        if self.leaked:
            leak_str = f'{self.leaked} leaked'
            if self.leak_bytes:
                leak_str += f' ({format_bytes(self.leak_bytes)})'
            parts.append(f'{mag}{leak_str}{rst}')
        # Pad with spaces to overwrite previous longer output
        sys.stderr.write(f'\r  {hdr} {", ".join(parts)}\033[K')
        sys.stderr.flush()

    def _process_line(self, line):
        """Process one complete line of output"""
        if RE_LEAK.match(line):
            self.leaked += 1
            self._show()
            return
        detail = RE_LEAK_DETAIL.match(line)
        if detail:
            self.leak_bytes += int(detail.group(1), 16)
            return

        # Result: lines are the source of truth when present. Switching on
        # the first Result: line drops any Test:-line counts gathered before
        # so the live progress matches what parse_results() will report.
        match = RE_RESULT.match(line)
        if match:
            if not self.has_result:
                self.has_result = True
                self.passed = 0
                self.failed = 0
                self.skipped = 0
                self.pending = False
            status = match.group(1)
            if status == 'PASS':
                self.passed += 1
            elif status == 'FAIL':
                self.failed += 1
            elif status == 'SKIP':
                self.skipped += 1
            self._show()
            return

        # No Result: line yet -- fall back to counting Test: lines so older
        # U-Boot trees that do not emit Result: still show progress.
        if self.has_result:
            return
        if RE_TEST_FAILED.search(line):
            self.failed += 1
            self.pending = False
            self._show()
        elif RE_TEST_NAME.match(line):
            if self.pending:
                self.passed += 1
            self.pending = True
            self._show()

    def update(self, _stream, data):  # pylint: disable=W9016,W9019
        """output_func callback for command.run_pipe()"""
        if isinstance(data, (bytes, bytearray)):
            data = data.decode('utf-8', errors='replace')
        self.buf += data
        while '\n' in self.buf:
            line, self.buf = self.buf.split('\n', 1)
            self._process_line(line)

    def finish(self):
        """Close out the progress line"""
        if not self.has_result and self.pending:
            self.passed += 1
            self.pending = False
        if self.passed or self.failed or self.skipped:
            self._show()
            sys.stderr.write('\n')
            sys.stderr.flush()


def run_ut(cmd, sandbox, specs):
    """Run a ut command and capture the output

    Sets up the persistent-data directory and shows live progress on
    stderr when available.

    Args:
        cmd (list): Command and arguments to run
        sandbox (str): Path to sandbox executable
        specs (list): List of (suite, pattern) tuples

    Returns:
        tuple: (result, elapsed) or (None, 0) on failure
    """
    build_dir = settings.get('build_dir', '/tmp/b')
    persist_dir = os.path.join(build_dir, 'sandbox', 'persistent-data')
    env = os.environ.copy()
    env['U_BOOT_PERSISTENT_DATA_DIR'] = persist_dir

    emit = has_emit_result()
    if sys.stderr.isatty():
        total = count_tests(sandbox, specs)
        progress = Progress(emit, total)
    else:
        progress = None
    output_func = progress.update if progress else None

    start_time = time.time()
    try:
        result = command.run_one(*cmd, capture=True, env=env,
                                 output_func=output_func)
    except command.CommandExc as exc:
        result = exc.result
        if result and isinstance(result.stdout, (bytes, bytearray)):
            result.to_output(False)
        if not result:
            tout.error(f'Command failed: {exc}')
            return None, 0
    finally:
        if progress:
            progress.finish()
    return result, time.time() - start_time


def show_test_output(result, args, col):
    """Parse and display test results

    Args:
        result (CommandResult): Output from running tests
        args (argparse.Namespace): Arguments from cmdline
        col (terminal.Color): Color object for output

    Returns:
        TestCounts, False, or None: Parsed result counts, False if no
            results were found, None on error
    """
    # Detect old U-Boot that doesn't understand -E or -F flags
    if 'failed while parsing option: -E' in result.stdout:
        tout.error('U-Boot does not support -E flag; use -L for legacy mode')
        return None
    if 'failed while parsing option: -F' in result.stdout:
        tout.error('U-Boot does not support -F flag; use -f to run all tests')
        return None

    legacy = args.legacy or not has_emit_result()
    res = parse_results(result.stdout, show_results=args.results, col=col)
    if not res and legacy:
        res = parse_legacy_results(result.stdout, show_results=args.results,
                                   col=col)
    if not res:
        res = parse_summary(result.stdout)

    # Print output in verbose mode, if there are failures, or no results
    if result.stdout and not args.results:
        if args.test_verbose or (res and res.failed) or not res:
            in_tests = False
            for line in result.stdout.splitlines():
                if not in_tests:
                    if line.startswith(('Running ', 'Test: ', 'Missing ')):
                        in_tests = True
                if in_tests:
                    print(line)
    return res or False


def check_signal(return_code):
    """Check if a return code indicates signal termination

    Args:
        return_code (int): Process return code

    Returns:
        int or None: Signal number, or None if not a signal
    """
    if return_code < 0:
        return -return_code
    if return_code > 128:
        return return_code - 128
    return None


def run_gdb(cmd, args):
    """Run sandbox under gdb-multiarch

    Args:
        cmd (list): Sandbox command and arguments
        args (argparse.Namespace): Arguments from cmdline

    Returns:
        int: Exit code from gdb
    """
    import subprocess  # pylint: disable=import-outside-toplevel

    run_args = shlex.join(cmd[1:])
    gdb_cmd = [
        'gdb-multiarch',
        '-q',
        cmd[0],
        '-iex', 'set auto-load safe-path /',
        '-iex', 'set debuginfod enabled off',
        '-iex', 'set sysroot',
        '-iex', 'handle SIGUSR2 nostop noprint pass',
        '-ex', f'run {run_args}',
    ]
    for extra in args.gdb_cmd:
        gdb_cmd.extend(['-ex', extra])
    if args.bt:
        gdb_cmd.extend(['-ex', 'bt', '-ex', 'quit'])

    if args.dry_run:
        tout.notice(shlex.join(gdb_cmd))
        return 0

    tout.info(f"Running: {shlex.join(gdb_cmd)}")
    result = subprocess.run(gdb_cmd, check=False)
    return result.returncode


def run_tests(sandbox, specs, args, col):
    """Run sandbox tests

    Args:
        sandbox (str): Path to sandbox executable
        specs (list): List of (suite, pattern) tuples from parse_test_specs
        args (argparse.Namespace): Arguments from cmdline
        col (terminal.Color): Color object for output

    Returns:
        int: Exit code from tests
    """
    if needs_dm_init(specs) and not ensure_dm_init_files():
        return 1

    cmd = build_ut_cmd(sandbox, specs, full=args.flattree_too,
                       verbose=args.test_verbose, legacy=args.legacy,
                       manual=args.manual,
                       malloc_dump=args.malloc_dump,
                       leak_check=args.leak_check)

    if args.gdb or args.bt or args.gdb_cmd:
        return run_gdb(cmd, args)

    if args.dry_run:
        tout.notice(shlex.join(cmd))
        return 0

    tout.info(f"Running: {shlex.join(cmd)}")
    ret = 1

    result, elapsed = run_ut(cmd, sandbox, specs)
    if result:
        res = show_test_output(result, args, col)
    else:
        res = None

    # Reset terminal if killed by a signal (e.g. SIGSEGV)
    sig = check_signal(result.return_code) if result else None
    if sig:
        os.system('tset')

    if res is not None and res:
        show_summary(res.passed, res.failed, res.skipped, elapsed,
                     res.leaked, res.leak_bytes)
        if res.leak_top and args.show_leaks:
            show_leak_top(res.leak_top, args.show_leaks)
        if sig:
            sig_names = {6: 'SIGABRT', 11: 'SIGSEGV', 15: 'SIGTERM'}
            tout.error(f'Test crashed '
                       f'({sig_names.get(sig, f"signal {sig}")})')
        ret = result.return_code
    elif res is not None:
        if sig:
            sig_names = {6: 'SIGABRT', 11: 'SIGSEGV', 15: 'SIGTERM'}
            tout.error(f'Test crashed '
                       f'({sig_names.get(sig, f"signal {sig}")})')
            ret = result.return_code
        else:
            tout.warning('No results detected (use -L for older U-Boot)')

    return ret


def report_unmatched(unmatched):
    """Report unmatched test specs to stderr

    Args:
        unmatched (list): List of (suite, pattern) tuples that did not match
    """
    for suite, pattern in unmatched:
        if suite and pattern:
            tout.error(f'No tests found matching: {suite}.{pattern}')
        elif suite:
            tout.error(f'No tests found in suite: {suite}')
        else:
            tout.error(f'No tests found matching: {pattern}')


def list_suites(sandbox):
    """List available test suites

    Args:
        sandbox (str): Path to sandbox executable
    """
    suites = get_suites_from_nm(sandbox)
    tout.notice('Available test suites:')
    for suite in suites:
        print(f'  {suite}')


def list_tests(sandbox, suite):
    """List available tests, optionally filtered by suite

    Args:
        sandbox (str): Path to sandbox executable
        suite (str or None): Suite name to filter by, or None for all
    """
    tests = get_tests_from_nm(sandbox, suite)
    if suite:
        tout.notice(f'Tests in suite "{suite}":')
    else:
        tout.notice('Available tests:')
    for suite_name, test_name in tests:
        print(f'  {suite_name}.{test_name}')


def do_test(args):
    """Handle test command - run U-Boot sandbox tests

    Args:
        args (argparse.Namespace): Arguments from cmdline

    Returns:
        int: Exit code
    """
    board = args.board or 'sandbox'
    ret = 0

    if args.build:
        if not build.build_board(
                board, args.dry_run, lto=args.lto,
                adjust_cfg=args.adjust_cfg,
                force_reconfig=args.force_reconfig, fresh=args.fresh,
                jobs=args.jobs, trace=args.trace,
                trace_early=not args.no_trace_early,
                output_dir=args.output_dir):
            ret = 1

    sandbox = None if ret else get_sandbox_path()
    if not ret and not sandbox:
        tout.error(f'Sandbox not found. Build first with: uman build {board}')
        ret = 1

    if not ret and args.list_suites:
        list_suites(sandbox)
    elif not ret and args.list_tests:
        list_tests(sandbox, args.tests[0] if args.tests else None)
    elif not ret:
        specs = parse_test_specs(args.tests)
        specs, unmatched = resolve_specs(sandbox, specs)
        if not unmatched:
            unmatched = validate_specs(sandbox, specs)
        if unmatched:
            report_unmatched(unmatched)
            ret = 1
        else:
            ret = run_tests(sandbox, specs, args, args.col)
    return ret
