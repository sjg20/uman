# SPDX-License-Identifier: GPL-2.0+
# Copyright 2025 Canonical Ltd
# Written by Simon Glass <simon.glass@canonical.com>

"""Functional tests for uman CI automation tool"""

# pylint: disable=import-error,too-many-lines

import argparse
import ast
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from u_boot_pylib import command
from u_boot_pylib import terminal
from u_boot_pylib import tools
from u_boot_pylib import tout
import gitlab

from uman_pkg import (build, cc, cmdconfig, cmddocker, cmdgit, cmdline, cmdpy,
                      cmdtest, control, gitlab_parser, settings, setup, util)

# Capture stdout and stderr for silent command execution
CAPTURE = {'capture': True, 'capture_stderr': True}


class TestBase(unittest.TestCase):
    """Base class for all uman tests"""
    preserve_indir = False
    preserve_outdirs = False
    toolpath = None
    verbosity = None
    no_capture = None

    @classmethod
    def setup_test_args(cls, preserve_indir=False, preserve_outdirs=False,
                        toolpath=None, verbosity=None, no_capture=None):
        # pylint: disable=too-many-arguments
        """Set up test arguments similar to other u-boot tools

        Args:
            preserve_indir (bool): Preserve input directories used by tests
            preserve_outdirs (bool): Preserve output directories used by tests
            toolpath (str): Path to tools directory
            verbosity (int): Verbosity level for output
            no_capture (bool): True to disable output capture during tests
        """
        cls.preserve_indir = preserve_indir
        cls.preserve_outdirs = preserve_outdirs
        cls.toolpath = toolpath
        cls.verbosity = verbosity
        cls.no_capture = no_capture
        if no_capture is not None:
            terminal.USE_CAPTURE = not no_capture

    def setUp(self):
        """Set up test fixtures"""
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        """Clean up and restore command.TEST_RESULT after each test"""
        command.TEST_RESULT = None
        if self.test_dir and os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)


class GitRepoMixin:
    """Mixin providing a real git repository for testing

    Requires self.test_dir to be set (provided by TestBase).
    Call setup_git_repo() in setUp() after super().setUp().
    Call teardown_git_repo() in tearDown() before super().tearDown().
    """

    def setup_git_repo(self):
        """Create a test git repository with commits"""
        self.orig_dir = os.getcwd()
        os.chdir(self.test_dir)

        command.output('git', 'init')
        command.output('git', 'config', 'user.email', 'test@test.com')
        command.output('git', 'config', 'user.name', 'Test User')

        # Create initial commit on main branch
        tools.write_file('file.txt', 'initial\n', binary=False)
        command.output('git', 'add', 'file.txt')
        command.output('git', 'commit', '-m', 'Initial commit')

        # Create 'upstream' branch as a tracking point
        command.output('git', 'branch', 'upstream')

        # Create feature commits
        for i in range(1, 5):
            tools.write_file(f'file{i}.txt', f'content {i}\n', binary=False)
            command.output('git', 'add', f'file{i}.txt')
            command.output('git', 'commit', '-m', f'Commit {i}')

        # Set upstream tracking
        command.output('git', 'branch', '--set-upstream-to=upstream')

    def teardown_git_repo(self):
        """Return to original directory"""
        os.chdir(self.orig_dir)

    def git_run(self, *args):
        """Run git command, ignoring errors (for cleanup)"""
        command.run_one('git', *args, raise_on_error=False, **CAPTURE)


def make_args(**kwargs):
    """Create an argparse.Namespace with default CI arguments"""
    defaults = {
        'adjust_cfg': None,
        'all': False,
        'bisect': None,
        'board': None,
        'bt': False,
        'build': False,
        'build_dir': None,
        'c_test': False,
        'cmd': 'ci',
        'debug': False,
        'dest': None,
        'dry_run': False,
        'exitfirst': False,
        'extra_args': [],
        'find': None,
        'force': False,
        'force_reconfig': False,
        'flattree_too': False,
        'fresh': False,
        'gdb': False,
        'gdb_cmd': [],
        'gdb_phase': None,
        'gdbserver': None,
        'jobs': None,
        'leak_check': False,
        'list_boards': False,
        'lto': False,
        'malloc_dump': None,
        'merge': False,
        'no_timeout': False,
        'null': False,
        'output_dir': None,
        'persist': False,
        'pollute': None,
        'pytest': None,
        'quiet': False,
        'remote': None,
        'setup_only': False,
        'show_cmd': False,
        'show_output': False,
        'sage': None,
        'sjg': None,
        'suites': False,
        'test_spec': [],
        'timing': None,
        'trace': False,
        'no_trace_early': False,
        'verbose': False,
        'world': False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


class TestUmanCmdline(TestBase):
    """Test the command line parsing"""

    def test_ci_subcommand_parsing(self):
        """Test that CI subcommand is parsed correctly"""
        parser = cmdline.setup_parser()

        # Test basic CI command
        args = parser.parse_args(['ci'])
        self.assertEqual('ci', args.cmd)
        self.assertFalse(args.suites)
        self.assertIsNone(args.pytest)

        # Test CI with flags
        args = parser.parse_args(['ci', '--suites', '--pytest'])
        self.assertTrue(args.suites)
        self.assertEqual('1', args.pytest)

        # Test short flags
        args = parser.parse_args(['ci', '-s', '-p', '-w'])
        self.assertTrue(args.suites)
        self.assertEqual('1', args.pytest)
        self.assertTrue(args.world)

    def test_selftest_subcommand_parsing(self):
        """Test that selftest subcommand is parsed correctly"""
        parser = cmdline.setup_parser()

        # Test basic selftest command
        args = parser.parse_args(['selftest'])
        self.assertEqual('selftest', args.cmd)
        self.assertIsNone(args.testname)

        # Test selftest with test name
        args = parser.parse_args(['selftest', 'test_ci'])
        self.assertEqual('selftest', args.cmd)
        self.assertEqual('test_ci', args.testname)

        # Test selftest with flags
        args = parser.parse_args(['selftest', '-N', '-X'])
        self.assertTrue(args.no_capture)
        self.assertTrue(args.test_preserve_dirs)

        # Test selftest alias (use cmdline.parse_args for alias resolution)
        args = cmdline.parse_args(['st'])
        self.assertEqual(args.cmd, 'selftest')

    def test_dry_run_flag(self):
        """Test that dry-run flag is parsed correctly"""
        parser = cmdline.setup_parser()

        # Test long flag
        args = parser.parse_args(['--dry-run', 'ci'])
        self.assertTrue(args.dry_run)

        # Test short flag
        args = parser.parse_args(['-n', 'ci'])
        self.assertTrue(args.dry_run)

        # Test without flag
        args = parser.parse_args(['ci'])
        self.assertFalse(args.dry_run)

    def test_quiet_flag(self):
        """Test that quiet flag is parsed correctly"""
        parser = cmdline.setup_parser()

        args = parser.parse_args(['--quiet', 'ci'])
        self.assertTrue(args.quiet)

        args = parser.parse_args(['-q', 'ci'])
        self.assertTrue(args.quiet)

        args = parser.parse_args(['ci'])
        self.assertFalse(args.quiet)

    def test_no_command_required(self):
        """Test that a command is required"""
        parser = cmdline.setup_parser()

        # Test that no command raises SystemExit (argparse error)
        with self.assertRaises(SystemExit):
            with terminal.capture():
                parser.parse_args([])

    def test_pytest_subcommand_parsing(self):
        """Test that pytest subcommand is parsed correctly"""
        parser = cmdline.setup_parser()

        # Test basic pytest command - board is required but not enforced by
        # argparse (checked in do_pytest)
        args = parser.parse_args(['pytest'])
        self.assertEqual(args.cmd, 'pytest')
        self.assertIsNone(args.board)
        self.assertEqual(args.test_spec, [])
        self.assertFalse(args.no_timeout)

        # Test pytest with board specified
        args = parser.parse_args(['pytest', '-B', 'sandbox', 'test_dm'])
        self.assertEqual(args.test_spec, ['test_dm'])
        self.assertEqual(args.board, 'sandbox')

        # Test pytest with multi-word test spec (no quotes needed)
        args = parser.parse_args(['pytest', '-B', 'coreboot', 'not', 'sleep'])
        self.assertEqual(args.test_spec, ['not', 'sleep'])
        self.assertEqual(args.board, 'coreboot')

        # Test pytest with --no-timeout flag
        args = parser.parse_args(['pytest', '-B', 'coreboot', 'test_dm',
                                  '--no-timeout'])
        self.assertEqual(args.board, 'coreboot')
        self.assertEqual(args.test_spec, ['test_dm'])
        self.assertTrue(args.no_timeout)

        # Test pytest alias (use cmdline.parse_args for alias resolution)
        args = cmdline.parse_args(['py', '-B', 'sandbox'])
        self.assertEqual(args.cmd, 'pytest')
        self.assertEqual(args.board, 'sandbox')

    def test_env_flag_parsing(self):
        """Test that -e flag parses environment variables"""
        args = cmdline.parse_args(
            ['py', '-B', 'sandbox', '-b', '-e', 'BL31=/path/bl31.bin'])
        self.assertEqual(['BL31=/path/bl31.bin'], args.env)

        # Multiple -e flags
        args = cmdline.parse_args(
            ['py', '-B', 'sandbox', '-b',
             '-e', 'BL31=/a', '-e', 'TEE=/b'])
        self.assertEqual(['BL31=/a', 'TEE=/b'], args.env)

        # No -e flag
        args = cmdline.parse_args(['py', '-B', 'sandbox'])
        self.assertIsNone(args.env)

        # Also works with build subcommand
        args = cmdline.parse_args(
            ['build', 'sandbox', '-e', 'BL31=/path/bl31.bin'])
        self.assertEqual(['BL31=/path/bl31.bin'], args.env)

    def test_id_flag_parsing(self):
        """Test that --id flag parses TEST_PY_ID"""
        args = cmdline.parse_args(['py', '-B', 'sandbox', '--id', 'na'])
        self.assertEqual('na', args.test_py_id)

        # No --id flag
        args = cmdline.parse_args(['py', '-B', 'sandbox'])
        self.assertIsNone(args.test_py_id)

    def test_why_skip_flag_parsing(self):
        """Test that --why-skip flag is parsed"""
        args = cmdline.parse_args(['py', '-B', 'sandbox', '--why-skip'])
        self.assertTrue(args.why_skip)

        args = cmdline.parse_args(['py', '-B', 'sandbox'])
        self.assertFalse(args.why_skip)

    def test_build_subcommand_parsing(self):
        """Test that build subcommand parses correctly"""
        args = cmdline.parse_args(['build', 'sandbox'])
        self.assertEqual('build', args.cmd)
        self.assertEqual('sandbox', args.board)

    def test_build_alias(self):
        """Test that 'b' alias works for build"""
        args = cmdline.parse_args(['b', 'sandbox'])
        self.assertEqual('build', args.cmd)
        self.assertEqual('sandbox', args.board)


class TestBuildSubcommand(TestBase):  # pylint: disable=R0904
    """Test build subcommand functionality"""

    def setUp(self):
        super().setUp()
        tout.init(tout.NOTICE)

    def test_get_dir(self):
        """Test build directory generation"""
        with mock.patch.object(settings, 'get', return_value='/tmp/b'):
            self.assertEqual('/tmp/b/sandbox', build.get_dir('sandbox'))

    def test_get_dir_default(self):
        """Test build directory with default when not configured"""
        with mock.patch.object(settings, 'get', return_value='/tmp/b'):
            self.assertEqual('/tmp/b/qemu-arm', build.get_dir('qemu-arm'))

    def test_get_cmd_basic(self):
        """Test basic build command generation (LTO disabled by default)"""
        args = cmdline.parse_args(['build', 'sandbox'])
        self.assertEqual(['buildman', '-L', '-I', '-w', '-W', '--boards',
                          'sandbox', '-o', '/tmp/b/sandbox'],
                         build.get_cmd(args, 'sandbox', '/tmp/b/sandbox'))

    def test_run_no_board(self):
        """Test that run requires a board"""
        args = cmdline.parse_args(['build'])
        # Clear any $b environment variable
        with mock.patch.dict(os.environ, {}, clear=True):
            with terminal.capture():
                result = build.run(args)
        self.assertEqual(1, result)

    def test_run_dry_run(self):
        """Test build dry-run returns the correct command"""
        args = cmdline.parse_args(['-n', 'build', 'sandbox'])
        got_cmd = None

        def mock_exec_cmd(cmd, dry_run=False, env=None, capture=True):
            nonlocal got_cmd

            del dry_run, env, capture  # unused
            got_cmd = cmd

        with mock.patch.object(build, 'exec_cmd', mock_exec_cmd):
            with mock.patch.object(build, 'setup_uboot_dir',
                                   return_value='/tmp'):
                with terminal.capture():
                    result = build.run(args)
        self.assertEqual(0, result)
        self.assertIn('buildman', got_cmd)

    def test_build_lto_flag(self):
        """Test -L/--lto flag"""
        args = cmdline.parse_args(['build', 'sandbox', '-L'])
        self.assertTrue(args.lto)

        args = cmdline.parse_args(['build', 'sandbox', '--lto'])
        self.assertTrue(args.lto)

    def test_get_cmd_lto_default(self):
        """Test that -L is passed to buildman by default (LTO disabled)"""
        args = cmdline.parse_args(['build', 'sandbox'])
        self.assertIn('-L', build.get_cmd(args, 'sandbox', '/tmp/b/sandbox'))

    def test_get_cmd_lto_enabled(self):
        """Test that -L is not passed when --lto is specified"""
        args = cmdline.parse_args(['build', 'sandbox', '-L'])
        self.assertNotIn('-L', build.get_cmd(args, 'sandbox', '/tmp/b/sandbox'))

    def test_build_board_lto_default(self):
        """Test that build_board() passes -L by default (LTO disabled)"""
        cap = []

        def mock_exec_cmd(cmd, *_args, **_kwargs):
            cap.append(cmd)
            return command.CommandResult(return_code=0)

        with mock.patch.object(build, 'setup_uboot_dir', return_value=True):
            with mock.patch.object(build, 'exec_cmd', mock_exec_cmd):
                with terminal.capture():
                    build.build_board('sandbox')

        self.assertIn('-L', cap[0])

    def test_build_board_lto_enabled(self):
        """Test that build_board() omits -L when lto=True"""
        cap = []

        def mock_exec_cmd(cmd, *_args, **_kwargs):
            cap.append(cmd)
            return command.CommandResult(return_code=0)

        with mock.patch.object(build, 'setup_uboot_dir', return_value=True):
            with mock.patch.object(build, 'exec_cmd', mock_exec_cmd):
                with terminal.capture():
                    build.build_board('sandbox', lto=True)

        self.assertNotIn('-L', cap[0])

    def test_build_board_trace_adds_config(self):
        """Test that build_board() adds -a TRACE and TRACE_EARLY by default"""
        cap = []
        captured_env = {}

        def mock_exec_cmd(cmd, dry_run=False, env=None, capture=True):
            del dry_run, capture  # unused
            cap.append(cmd)
            if env:
                captured_env.update(env)
            return command.CommandResult(return_code=0)

        with mock.patch.object(build, 'setup_uboot_dir', return_value=True):
            with mock.patch.object(build, 'exec_cmd', mock_exec_cmd):
                with terminal.capture():
                    build.build_board('sandbox', trace=True)

        # Check both TRACE and TRACE_EARLY are added
        cmd = cap[0]
        trace_indices = [i for i, x in enumerate(cmd) if x == '-a']
        self.assertEqual(2, len(trace_indices))
        self.assertEqual('TRACE', cmd[trace_indices[0] + 1])
        self.assertEqual('TRACE_EARLY', cmd[trace_indices[1] + 1])
        self.assertEqual('1', captured_env.get('FTRACE'))

    def test_build_board_trace_no_early(self):
        """Test that build_board() skips TRACE_EARLY when trace_early=False"""
        cap = []

        def mock_exec_cmd(cmd, dry_run=False, env=None, capture=True):
            del dry_run, env, capture  # unused
            cap.append(cmd)
            return command.CommandResult(return_code=0)

        with mock.patch.object(build, 'setup_uboot_dir', return_value=True):
            with mock.patch.object(build, 'exec_cmd', mock_exec_cmd):
                with terminal.capture():
                    build.build_board('sandbox', trace=True, trace_early=False)

        cmd = cap[0]
        trace_indices = [i for i, x in enumerate(cmd) if x == '-a']
        self.assertEqual(1, len(trace_indices))
        self.assertEqual('TRACE', cmd[trace_indices[0] + 1])

    def test_build_fresh_flag(self):
        """Test -F/--fresh flag"""
        args = cmdline.parse_args(['build', 'sandbox', '-F'])
        self.assertTrue(args.fresh)

    def test_build_target_option(self):
        """Test -t/--target option"""
        args = cmdline.parse_args(['build', 'sandbox', '-t', 'u-boot.bin'])
        self.assertEqual('u-boot.bin', args.target)

    def test_get_cmd_target(self):
        """Test that --target is passed to buildman"""
        args = cmdline.parse_args(['build', 'sandbox', '-t', 'u-boot.bin'])
        cmd = build.get_cmd(args, 'sandbox', '/tmp/b/sandbox')
        self.assertIn('--target', cmd)
        self.assertIn('u-boot.bin', cmd)

    def test_build_jobs_option(self):
        """Test -j/--jobs option"""
        args = cmdline.parse_args(['build', 'sandbox', '-j', '8'])
        self.assertEqual(8, args.jobs)

    def test_get_cmd_jobs(self):
        """Test that -j is passed to buildman"""
        args = cmdline.parse_args(['build', 'sandbox', '-j', '4'])
        cmd = build.get_cmd(args, 'sandbox', '/tmp/b/sandbox')
        self.assertIn('-j', cmd)
        self.assertIn('4', cmd)

    def test_build_objdump_flag(self):
        """Test -O/--objdump flag"""
        args = cmdline.parse_args(['build', 'sandbox', '-O'])
        self.assertTrue(args.objdump)

        args = cmdline.parse_args(['build', 'sandbox', '--objdump'])
        self.assertTrue(args.objdump)

    def test_get_execs(self):
        """Test get_execs yields existing ELF files"""
        uboot_path = os.path.join(self.test_dir, 'u-boot')
        tools.write_file(uboot_path, b'ELF')
        self.assertEqual([uboot_path], list(build.get_execs(self.test_dir)))

    def test_get_execs_empty(self):
        """Test get_execs with no ELF files"""
        self.assertEqual([], list(build.get_execs(self.test_dir)))

    def test_build_size_flag(self):
        """Test -s/--size flag"""
        args = cmdline.parse_args(['build', 'sandbox'])
        self.assertFalse(args.size)

        args = cmdline.parse_args(['build', 'sandbox', '-s'])
        self.assertTrue(args.size)

        args = cmdline.parse_args(['build', 'sandbox', '--size'])
        self.assertTrue(args.size)

    def test_build_force_reconfig_flag(self):
        """Test -f/--force-reconfig flag passes -C to buildman"""
        args = cmdline.parse_args(['build', 'sandbox', '-f'])
        self.assertTrue(args.force_reconfig)
        cmd = build.get_cmd(args, 'sandbox', '/tmp/b/sandbox')
        self.assertIn('-C', cmd)

    def test_build_adjust_cfg_option(self):
        """Test -a/--adjust-cfg option"""
        args = cmdline.parse_args(['build', 'sandbox', '-a', 'FOO=y'])
        self.assertEqual(['FOO=y'], args.adjust_cfg)

        # Multiple arguments
        args = cmdline.parse_args(['build', 'sandbox', '-a', 'FOO=y', '-a', 'BAR=n'])
        self.assertEqual(['FOO=y', 'BAR=n'], args.adjust_cfg)

    def test_get_cmd_adjust_cfg(self):
        """Test that -a arguments are passed to buildman"""
        args = cmdline.parse_args(
            ['build', 'sandbox', '-a', 'FOO=y', '-a', 'BAR=n'])
        cmd = build.get_cmd(args, 'sandbox', '/tmp/b/sandbox')
        # Each -a should produce a separate -a in the buildman command
        self.assertEqual(2, cmd.count('-a'))
        self.assertIn('FOO=y', cmd)
        self.assertIn('BAR=n', cmd)

    def test_build_in_tree_flag(self):
        """Test -I/--in-tree flag uses -i for buildman"""
        args = cmdline.parse_args(['build', 'sandbox', '-I'])
        self.assertTrue(args.in_tree)
        cmd = build.get_cmd(args, 'sandbox', '/tmp/b/sandbox')
        self.assertIn('-i', cmd)
        self.assertNotIn('-I', cmd)
        self.assertNotIn('-o', cmd)  # No output directory for in-tree

    def test_build_trace_flag(self):
        """Test -T/--trace flag sets FTRACE and adds -a TRACE and TRACE_EARLY"""
        args = cmdline.parse_args(['build', 'sandbox', '-T'])
        self.assertTrue(args.trace)
        self.assertFalse(args.no_trace_early)

        # Test that FTRACE is set and -a TRACE/TRACE_EARLY are added
        captured_env = {}
        captured_cmd = []

        def mock_exec_cmd(cmd, dry_run=False, env=None, capture=True):
            del dry_run, capture  # unused
            captured_cmd.extend(cmd)
            if env:
                captured_env.update(env)

        with mock.patch.object(build, 'exec_cmd', mock_exec_cmd):
            with mock.patch.object(build, 'setup_uboot_dir',
                                   return_value='/tmp'):
                with terminal.capture():
                    build.run(args)

        self.assertIn('FTRACE', captured_env)
        self.assertEqual('1', captured_env.get('FTRACE'))
        # Check both -a TRACE and -a TRACE_EARLY were added
        trace_indices = [i for i, x in enumerate(captured_cmd) if x == '-a']
        self.assertEqual(2, len(trace_indices))
        self.assertEqual('TRACE', captured_cmd[trace_indices[0] + 1])
        self.assertEqual('TRACE_EARLY', captured_cmd[trace_indices[1] + 1])

    def test_build_trace_no_early_flag(self):
        """Test --no-trace-early flag prevents TRACE_EARLY"""
        args = cmdline.parse_args(['build', 'sandbox', '-T', '--no-trace-early'])
        self.assertTrue(args.trace)
        self.assertTrue(args.no_trace_early)

        captured_cmd = []

        def mock_exec_cmd(cmd, dry_run=False, env=None, capture=True):
            del dry_run, env, capture  # unused
            captured_cmd.extend(cmd)

        with mock.patch.object(build, 'exec_cmd', mock_exec_cmd):
            with mock.patch.object(build, 'setup_uboot_dir',
                                   return_value='/tmp'):
                with terminal.capture():
                    build.run(args)

        # Check only -a TRACE was added, not TRACE_EARLY
        trace_indices = [i for i, x in enumerate(captured_cmd) if x == '-a']
        self.assertEqual(1, len(trace_indices))
        self.assertEqual('TRACE', captured_cmd[trace_indices[0] + 1])

    def test_build_gprof_flag(self):
        """Test --gprof flag sets GPROF environment variable"""
        args = cmdline.parse_args(['build', 'sandbox', '--gprof'])
        self.assertTrue(args.gprof)

        # Test that GPROF is set in environment when gprof flag is used
        captured_env = {}

        def mock_exec_cmd(_cmd, dry_run=False, env=None, capture=True):
            del dry_run, capture  # unused
            if env:
                captured_env.update(env)

        with mock.patch.object(build, 'exec_cmd', mock_exec_cmd):
            with mock.patch.object(build, 'setup_uboot_dir',
                                   return_value='/tmp'):
                with terminal.capture():
                    build.run(args)

        self.assertIn('GPROF', captured_env)
        self.assertEqual('1', captured_env.get('GPROF'))

    def test_build_werror_flag(self):
        """Test -E/--werror flag sets KCFLAGS=-Werror"""
        args = cmdline.parse_args(['build', 'sandbox', '-E'])
        self.assertTrue(args.werror)

        captured_env = {}

        def mock_exec_cmd(_cmd, dry_run=False, env=None, capture=True):
            del dry_run, capture  # unused
            if env:
                captured_env.update(env)

        with mock.patch.object(build, 'exec_cmd', mock_exec_cmd):
            with mock.patch.object(build, 'setup_uboot_dir',
                                   return_value='/tmp'):
                with terminal.capture():
                    build.run(args)

        self.assertIn('KCFLAGS', captured_env)
        self.assertEqual('-Werror', captured_env.get('KCFLAGS'))

    def test_build_fail_on_warning_flag(self):
        """Test --fail-on-warning flag fails on buildman return code 101"""
        args = cmdline.parse_args(['build', 'sandbox', '--fail-on-warning'])
        self.assertTrue(args.fail_on_warning)

        def mock_exec_cmd(_cmd, dry_run=False, env=None, capture=True):
            del dry_run, env, capture  # unused
            # Return 101 to simulate warnings
            return command.CommandResult(return_code=101)

        # Create fake u-boot file to simulate successful build with warnings
        elf_path = os.path.join(self.test_dir, 'sandbox', 'u-boot')
        os.makedirs(os.path.dirname(elf_path), exist_ok=True)
        tools.write_file(elf_path, b'fake elf')

        with mock.patch.object(build, 'exec_cmd', mock_exec_cmd):
            with mock.patch.object(build, 'setup_uboot_dir',
                                   return_value='/tmp'):
                with mock.patch.object(build, 'get_dir',
                                       return_value=os.path.dirname(elf_path)):
                    with terminal.capture() as (_, err):
                        result = build.run(args)

        self.assertEqual(101, result)
        self.assertIn('warnings', err.getvalue())

    def test_build_debug_flag(self):
        """Test -g/--debug flag adds -a CC_OPTIMIZE_FOR_DEBUG"""
        args = cmdline.parse_args(['build', 'sandbox', '-g'])
        self.assertTrue(args.debug)

        captured_cmd = []

        def mock_exec_cmd(cmd, dry_run=False, env=None, capture=True):
            del dry_run, env, capture  # unused
            captured_cmd.extend(cmd)

        with mock.patch.object(build, 'exec_cmd', mock_exec_cmd):
            with mock.patch.object(build, 'setup_uboot_dir',
                                   return_value='/tmp'):
                with terminal.capture():
                    build.run(args)

        # Check -a CC_OPTIMIZE_FOR_DEBUG was added
        self.assertIn('-a', captured_cmd)
        idx = captured_cmd.index('-a')
        self.assertEqual('CC_OPTIMIZE_FOR_DEBUG', captured_cmd[idx + 1])

    def test_build_output_dir_flag(self):
        """Test -o/--output-dir flag overrides build directory"""
        args = cmdline.parse_args(['build', 'sandbox', '-o', '/custom/out'])
        self.assertEqual('/custom/out', args.output_dir)

        # Test that the output directory is used in the build command
        captured_cmd = []

        def mock_exec_cmd(cmd, dry_run=False, env=None, capture=True):
            del dry_run, env, capture  # unused
            captured_cmd.extend(cmd)

        with mock.patch.object(build, 'exec_cmd', mock_exec_cmd):
            with mock.patch.object(build, 'setup_uboot_dir',
                                   return_value='/tmp'):
                with terminal.capture():
                    build.run(args)

        # Check that buildman was called with the custom output directory
        self.assertIn('-o', captured_cmd)
        idx = captured_cmd.index('-o')
        self.assertEqual('/custom/out', captured_cmd[idx + 1])

    def test_do_bisect(self):
        """Test build bisect finds first bad commit"""
        git_calls = []
        # Simulate: HEAD bad, upstream good, bisect finds mid222 as first bad
        current_commit = ['abc123']
        bisect_step = [0]

        def mock_run_one(*args, capture=False):
            del capture
            git_calls.append(args)
            result = mock.Mock()
            result.stdout = ''
            if args[1] == 'status':
                result.stdout = 'On branch my-branch'
            elif args[1] == 'checkout':
                current_commit[0] = args[2][:6]
            elif args[1] == 'bisect':
                if args[2] == 'start':
                    pass
                elif args[2] in ('good', 'bad'):
                    if len(args) == 3:  # bisect good/bad without commit
                        bisect_step[0] += 1
                        if bisect_step[0] == 1:
                            current_commit[0] = 'mid111'
                        elif bisect_step[0] == 2:
                            # Bisect complete
                            result.stdout = 'mid222 is the first bad commit\n'
            return result

        def mock_output_one_line(*args):
            if args[1] == 'rev-parse':
                if args[2] == '@{u}':
                    return 'def456'
                return current_commit[0]
            if args[1] == 'symbolic-ref':
                return 'my-branch'
            if args[1] == 'log':
                return 'Bad commit message'
            return ''

        def mock_try_build(_board, _build_dir):
            # HEAD (abc123) bad, upstream (def456) good, mid111 bad
            return current_commit[0] == 'def456'

        with (
            mock.patch.object(command, 'run_one', mock_run_one),
            mock.patch.object(command, 'output_one_line', mock_output_one_line),
            mock.patch.object(build, 'try_build', mock_try_build),
            terminal.capture() as (out, _err),
        ):
            result = build.do_bisect('sandbox', '/tmp/b/sandbox')

        self.assertEqual(0, result)
        self.assertIn('First bad commit: mid222', out.getvalue())
        # Verify we returned to original branch
        self.assertEqual(('git', 'checkout', 'my-branch'), git_calls[-1])

    def test_do_bisect_rebase_in_progress(self):
        """Test bisect refuses to start during rebase"""
        def mock_run_one(*_args, capture=False):
            del capture
            result = mock.Mock()
            result.stdout = 'interactive rebase in progress'
            return result

        tout.init(tout.WARNING)
        with mock.patch.object(command, 'run_one', mock_run_one):
            with terminal.capture() as (_, err):
                result = build.do_bisect('sandbox', '/tmp/b/sandbox')

        self.assertEqual(1, result)
        self.assertIn('Rebase in progress', err.getvalue())

    def test_build_shows_stderr_on_failure(self):
        """Test build run() shows stderr when build fails"""
        def mock_exec_cmd(cmd, dry_run=False, env=None, capture=True):
            del cmd, dry_run, env, capture
            return command.CommandResult(
                return_code=2, stdout='', stderr='error: something failed\n')

        args = cmdline.parse_args(['build', 'sandbox'])
        with mock.patch.object(build, 'exec_cmd', mock_exec_cmd):
            with mock.patch.object(build, 'setup_uboot_dir', return_value=True):
                with terminal.capture() as (_, err):
                    result = build.run(args)

        self.assertEqual(2, result)
        self.assertIn('something failed', err.getvalue())

    def test_build_board_shows_stderr_on_failure(self):
        """Test build_board() shows stderr when build fails"""
        def mock_exec_cmd(cmd, dry_run=False, env=None, capture=True):
            del cmd, dry_run, env, capture
            return command.CommandResult(
                return_code=1, stdout='', stderr='make: *** Error 1\n')

        with mock.patch.object(build, 'exec_cmd', mock_exec_cmd):
            with mock.patch.object(build, 'setup_uboot_dir', return_value=True):
                with terminal.capture() as (_, err):
                    result = build.build_board('sandbox')

        self.assertFalse(result)
        self.assertIn('Error 1', err.getvalue())

    def test_build_board_shows_progress(self):
        """Test build_board() shows progress messages"""
        progress_msgs = []
        orig_progress = tout.progress

        def mock_progress(msg):
            progress_msgs.append(msg)
            orig_progress(msg)

        def mock_exec_cmd(cmd, dry_run=False, env=None, capture=True):
            del cmd, dry_run, env, capture
            return command.CommandResult(return_code=0)

        with mock.patch.object(tout, 'progress', mock_progress):
            with mock.patch.object(build, 'exec_cmd', mock_exec_cmd):
                with mock.patch.object(build, 'setup_uboot_dir',
                                       return_value=True):
                    with terminal.capture():
                        build.build_board('sandbox')

        self.assertEqual(['Building sandbox'], progress_msgs)


class TestConfigSubcommand(TestBase):
    """Test config subcommand"""

    def setUp(self):
        super().setUp()
        tout.init(tout.WARNING)
        self.build_dir = os.path.join(self.test_dir, 'sandbox')
        os.makedirs(self.build_dir)

        # Create a sample .config file
        config_content = """#
# Automatically generated file
#
CONFIG_SANDBOX=y
CONFIG_SYS_ARCH="sandbox"
CONFIG_VIDEO=y
# CONFIG_VIDEO_FONT_4X6 is not set
CONFIG_DM_TEST=y
"""
        with open(os.path.join(self.build_dir, '.config'), 'w',
                  encoding='utf-8') as outf:
            outf.write(config_content)

    def test_config_subcommand_parsing(self):
        """Test config subcommand argument parsing"""
        args = cmdline.parse_args(['config', '-B', 'sandbox', '-g', 'VIDEO'])
        self.assertEqual('config', args.cmd)
        self.assertEqual('sandbox', args.board)
        self.assertEqual('VIDEO', args.grep)

    def test_config_alias(self):
        """Test cfg alias works"""
        args = cmdline.parse_args(['cfg', '-B', 'sandbox', '-g', 'TEST'])
        self.assertEqual('config', args.cmd)

    def test_config_grep(self):
        """Test config grep finds matches"""
        args = cmdline.parse_args(['config', '-B', 'sandbox', '-g', 'VIDEO',
                                   '-o', self.build_dir])
        with terminal.capture() as (out, _):
            ret = cmdconfig.run(args)
        self.assertEqual(0, ret)
        self.assertIn('CONFIG_VIDEO=y', out.getvalue())
        self.assertIn('VIDEO_FONT', out.getvalue())

    def test_config_grep_case_insensitive(self):
        """Test config grep is case-insensitive"""
        args = cmdline.parse_args(['config', '-B', 'sandbox', '-g', 'video',
                                   '-o', self.build_dir])
        with terminal.capture() as (out, _):
            ret = cmdconfig.run(args)
        self.assertEqual(0, ret)
        self.assertIn('CONFIG_VIDEO=y', out.getvalue())

    def test_config_grep_no_match(self):
        """Test config grep with no matches"""
        args = cmdline.parse_args(
            ['config', '-B', 'sandbox', '-g', 'NONEXISTENT',
             '-o', self.build_dir])
        with terminal.capture() as (out, _):
            ret = cmdconfig.run(args)
        self.assertEqual(0, ret)
        self.assertFalse(out.getvalue())

    def test_config_no_board(self):
        """Test config fails without board"""
        orig_b = os.environ.pop('b', None)
        try:
            args = cmdline.parse_args(['config', '-g', 'VIDEO'])
            with terminal.capture() as (_, err):
                ret = cmdconfig.run(args)
            self.assertEqual(1, ret)
            self.assertIn('Board is required', err.getvalue())
        finally:
            if orig_b:
                os.environ['b'] = orig_b

    def test_config_no_action(self):
        """Test config fails without action"""
        args = cmdline.parse_args(['config', '-B', 'sandbox'])
        with terminal.capture() as (_, err):
            ret = cmdconfig.run(args)
        self.assertEqual(1, ret)
        self.assertIn('No action specified', err.getvalue())

    def test_config_missing_config_file(self):
        """Test config fails when .config not found"""
        args = cmdline.parse_args(['config', '-B', 'sandbox', '-g', 'VIDEO',
                                   '-o', '/nonexistent/path'])
        with terminal.capture() as (_, err):
            ret = cmdconfig.run(args)
        self.assertEqual(1, ret)
        self.assertIn('Config file not found', err.getvalue())

    def test_config_sync_flag(self):
        """Test -s/--sync flag parsing"""
        args = cmdline.parse_args(['config', '-B', 'sandbox', '-s'])
        self.assertEqual('config', args.cmd)
        self.assertTrue(args.sync)

    def test_config_sync_runs_commands(self):
        """Test config sync runs make defconfig and savedefconfig"""
        cap = []

        def mock_exec_cmd(cmd, dry_run=False, env=None, capture=True):
            del dry_run, env, capture
            cap.append(cmd)
            return command.CommandResult(return_code=0)

        args = cmdline.parse_args(['config', '-B', 'sandbox', '-s',
                                   '-o', self.build_dir])
        # Create defconfig in build dir for copy
        with open(os.path.join(self.build_dir, 'defconfig'), 'w',
                  encoding='utf-8') as outf:
            outf.write('CONFIG_SANDBOX=y\n')
        # Create configs dir for destination
        os.makedirs(os.path.join(self.test_dir, 'configs'))

        with mock.patch.object(cmdconfig, 'exec_cmd', mock_exec_cmd):
            with mock.patch.object(cmdconfig, 'get_uboot_dir',
                                   return_value=self.test_dir):
                with terminal.capture():
                    ret = cmdconfig.do_sync(args)

        self.assertEqual(0, ret)
        # Should have called make twice (defconfig and savedefconfig)
        self.assertEqual(2, len(cap))
        self.assertIn('make', cap[0])
        self.assertIn('-s', cap[0])
        self.assertIn('sandbox_defconfig', cap[0])
        self.assertIn('make', cap[1])
        self.assertIn('savedefconfig', cap[1])

    def test_config_meld_flag(self):
        """Test -m/--meld flag parsing"""
        args = cmdline.parse_args(['config', '-B', 'sandbox', '-m'])
        self.assertEqual('config', args.cmd)
        self.assertTrue(args.meld)

    def test_config_meld_runs_meld(self):
        """Test config meld opens meld after savedefconfig"""
        cap = []

        def mock_exec_cmd(cmd, dry_run=False, env=None, capture=True):
            del dry_run, env, capture
            cap.append(cmd)
            return command.CommandResult(return_code=0)

        args = cmdline.parse_args(['config', '-B', 'sandbox', '-m',
                                   '-o', self.build_dir])
        # Create defconfig in build dir
        with open(os.path.join(self.build_dir, 'defconfig'), 'w',
                  encoding='utf-8') as outf:
            outf.write('CONFIG_SANDBOX=y\n')
        # Create configs dir for destination
        os.makedirs(os.path.join(self.test_dir, 'configs'))

        with mock.patch.object(cmdconfig, 'exec_cmd', mock_exec_cmd):
            with mock.patch.object(cmdconfig, 'get_uboot_dir',
                                   return_value=self.test_dir):
                with terminal.capture():
                    ret = cmdconfig.do_sync(args, use_meld=True)

        self.assertEqual(0, ret)
        # Should have called make twice then meld
        self.assertEqual(3, len(cap))
        self.assertIn('make', cap[0])
        self.assertIn('sandbox_defconfig', cap[0])
        self.assertIn('make', cap[1])
        self.assertIn('savedefconfig', cap[1])
        self.assertEqual('meld', cap[2][0])

    def test_find_function(self):
        """Test finding a function in the binary"""
        # Create a source tree with cmd/version.c so prefix stripping works
        src_dir = os.path.join(self.test_dir, 'src')
        os.makedirs(os.path.join(src_dir, 'cmd'))
        with open(os.path.join(src_dir, 'cmd', 'version.c'), 'w',
                  encoding='utf-8') as outf:
            outf.write('')

        nm_output = ('0000000000073fb2 t do_version\n'
                     '0000000000073fc0 T do_version_cmd\n'
                     '00000000000886c5 t do_mem_md\n')
        addr2line_output = ('/build/env/cmd/version.c:18\n'
                            '/build/env/cmd/version.c:30\n')
        binary = os.path.join(self.build_dir, 'u-boot')
        with open(binary, 'w', encoding='utf-8') as outf:
            outf.write('')

        def mock_run(*cmd_args, **kwargs):
            if cmd_args[0] == 'nm':
                return command.CommandResult(return_code=0,
                                             stdout=nm_output)
            return command.CommandResult(return_code=0,
                                         stdout=addr2line_output)

        args = cmdline.parse_args(['config', '-B', 'sandbox', '-f',
                                   'do_version', '-o', self.build_dir])
        with mock.patch.object(command, 'run_one', mock_run):
            with mock.patch.object(cmdconfig, 'get_uboot_dir',
                                   return_value=src_dir):
                with terminal.capture() as (out, err):
                    ret = cmdconfig.do_find(args)

        self.assertEqual(0, ret)
        self.assertEqual('do_version: cmd/version.c:18\n'
                         'do_version_cmd: cmd/version.c:30\n',
                         out.getvalue())
        self.assertFalse(err.getvalue())

    def test_find_function_no_match(self):
        """Test finding a function with no matches"""
        nm_output = '0000000000073fb2 t do_version\n'
        binary = os.path.join(self.build_dir, 'u-boot')
        with open(binary, 'w', encoding='utf-8') as outf:
            outf.write('')

        def mock_run(*cmd_args, **kwargs):
            return command.CommandResult(return_code=0, stdout=nm_output)

        args = cmdline.parse_args(['config', '-B', 'sandbox', '-f',
                                   'nonexistent', '-o', self.build_dir])
        with mock.patch.object(command, 'run_one', mock_run):
            with terminal.capture() as (out, err):
                ret = cmdconfig.do_find(args)

        self.assertEqual(1, ret)
        self.assertFalse(out.getvalue())

    def test_find_function_no_binary(self):
        """Test finding a function when binary does not exist"""
        args = cmdline.parse_args(['config', '-B', 'sandbox', '-f',
                                   'do_version', '-o', self.build_dir])
        # Remove the build dir contents (no u-boot binary)
        with terminal.capture() as (out, err):
            ret = cmdconfig.do_find(args)

        self.assertEqual(1, ret)
        self.assertFalse(out.getvalue())


class TestGitSubcommand(TestBase):
    """Test git subcommand functionality"""

    def setUp(self):
        super().setUp()
        tout.init(tout.NOTICE)

    def test_git_subcommand_parsing(self):
        """Test git subcommand argument parsing"""
        args = cmdline.parse_args(['git', 'rf'])
        self.assertEqual('git', args.cmd)
        self.assertEqual('rf', args.action)
        self.assertIsNone(args.arg)

    def test_git_subcommand_with_arg(self):
        """Test git subcommand with argument"""
        args = cmdline.parse_args(['git', 'rf', '3'])
        self.assertEqual('git', args.cmd)
        self.assertEqual('rf', args.action)
        self.assertEqual('3', args.arg)

    def test_git_alias(self):
        """Test 'g' alias for git"""
        args = cmdline.parse_args(['g', 'rc'])
        self.assertEqual('git', args.cmd)
        self.assertEqual('rc', args.action)

    def test_git_aliases_flag(self):
        """Test git -a outputs shell alias definitions"""
        args = cmdline.parse_args(['git', '-a'])
        self.assertTrue(args.aliases)
        with terminal.capture() as (out, _):
            result = cmdgit.run(args)
        self.assertEqual(0, result)
        output = out.getvalue()
        self.assertIn("alias am='git commit --amend'", output)
        self.assertIn("alias st='git stash'", output)
        # Check cg function passes $b
        self.assertIn('cg() { b="$b" command cg "$@"; }', output)

    def test_git_symlink_invocation(self):
        """Test invoking via symlink automatically runs git subcommand"""
        # Simulate invoking as 'rf' symlink with argument
        args = cmdline.parse_args(['3'], prog_name='/usr/local/bin/rf')
        self.assertEqual('git', args.cmd)
        self.assertEqual('rf', args.action)
        self.assertEqual('3', args.arg)

        # Simulate invoking as 'rebase-diff' symlink
        args = cmdline.parse_args([], prog_name='rebase-diff')
        self.assertEqual('git', args.cmd)
        self.assertEqual('rebase-diff', args.action)

        # Normal invocation should not trigger symlink detection
        args = cmdline.parse_args(['git', 'rc'], prog_name='uman')
        self.assertEqual('git', args.cmd)
        self.assertEqual('rc', args.action)

    def test_cg_symlink_invocation(self):
        """Test invoking via 'cg' symlink runs config -g"""
        args = cmdline.parse_args(['TRACE'], prog_name='/usr/local/bin/cg')
        self.assertEqual('config', args.cmd)
        self.assertEqual('TRACE', args.grep)
        # Board not set via args, so will use $b env var
        self.assertIsNone(args.board)

    def test_git_all_actions(self):
        """Test all git actions are valid"""
        actions = ['et', 'fu', 'gr', 'gu', 'ol', 'pm', 'ra', 'rb', 'rd', 're',
                   'rf', 'rp', 'rn', 'rc', 'rs', 'us']
        for action in actions:
            args = cmdline.parse_args(['git', action])
            self.assertEqual(action, args.action)

    def test_git_full_command_names(self):
        """Test full command names are resolved to short names"""
        full_names = {
            'edit-todo': 'et',
            'find-upstream': 'fu',
            'git-rebase': 'gr',
            'grep-upstream': 'gu',
            'oneline-log': 'ol',
            'patch-merge': 'pm',
            'rebase-abort': 'ra',
            'rebase-beginning': 'rb',
            'rebase-diff': 'rd',
            'rebase-edit': 're',
            'rebase-first': 'rf',
            'rebase-patch': 'rp',
            'rebase-next': 'rn',
            'rebase-continue': 'rc',
            'rebase-skip': 'rs',
            'set-upstream': 'us',
        }
        for full_name, short_name in full_names.items():
            args = cmdline.parse_args(['git', full_name])
            self.assertEqual(full_name, args.action)
            # Verify alias resolution
            resolved = cmdgit.ACTION_ALIASES.get(full_name)
            self.assertEqual(short_name, resolved)

    def test_get_upstream(self):
        """Test get_upstream returns upstream branch"""
        def mock_git_output(*args):
            if args[0] == 'rev-parse' and args[2] == '@{upstream}':
                return 'origin/main'
            return ''

        with mock.patch('uman_pkg.cmdgit.git_output_quiet', mock_git_output):
            upstream = cmdgit.get_upstream()
        self.assertEqual('origin/main', upstream)

    def test_get_upstream_during_rebase(self):
        """Test get_upstream finds original branch's upstream during rebase"""
        # Simulate being in a rebase with head-name file
        rebase_dir = tempfile.mkdtemp()
        head_name_file = os.path.join(rebase_dir, 'head-name')
        tools.write_file(head_name_file, b'refs/heads/feature\n')

        def mock_git_output(*args):
            # @{upstream} fails (detached HEAD during rebase)
            if args[2] == '@{upstream}':
                raise command.CommandExc('no upstream', None)
            # feature@{upstream} succeeds
            if args[2] == 'feature@{upstream}':
                return 'origin/main'
            return ''

        with mock.patch('uman_pkg.cmdgit.get_rebase_dir',
                        return_value=rebase_dir):
            with mock.patch('uman_pkg.cmdgit.git_output_quiet',
                            mock_git_output):
                upstream = cmdgit.get_upstream()

        self.assertEqual('origin/main', upstream)

    def test_get_rebase_dir_not_rebasing(self):
        """Test get_rebase_dir returns None when not rebasing"""
        def mock_git_output(*_args):
            return '/nonexistent/.git/rebase-merge'

        with mock.patch('uman_pkg.cmdgit.git_output', mock_git_output):
            rebase_dir = cmdgit.get_rebase_dir()
        self.assertIsNone(rebase_dir)

    def test_get_rebase_position_not_rebasing(self):
        """Test get_rebase_position returns empty string when not rebasing"""
        def mock_git_output(*_args):
            return '/nonexistent/.git/rebase-merge'

        with mock.patch('uman_pkg.cmdgit.git_output', mock_git_output):
            pos = cmdgit.get_rebase_position()
        self.assertEqual('', pos)

    def test_seq_edit_env_break(self):
        """Test seq_edit_env inserts a break"""
        env = cmdgit.seq_edit_env('break')
        self.assertIn('GIT_SEQUENCE_EDITOR', env)
        todo = os.path.join(self.test_dir, 'todo')
        with open(todo, 'w', encoding='utf-8') as fil:
            fil.write('pick abc First\npick def Second\n')
        os.system(f'{env["GIT_SEQUENCE_EDITOR"]} {todo}')
        with open(todo, encoding='utf-8') as fil:
            self.assertEqual('break\npick abc First\npick def Second\n',
                             fil.read())

    def test_seq_edit_env_edit(self):
        """Test seq_edit_env changes pick to edit"""
        env = cmdgit.seq_edit_env('edit')
        todo = os.path.join(self.test_dir, 'todo')
        with open(todo, 'w', encoding='utf-8') as fil:
            fil.write('pick abc First\npick def Second\n')
        os.system(f'{env["GIT_SEQUENCE_EDITOR"]} {todo}')
        with open(todo, encoding='utf-8') as fil:
            self.assertEqual('edit abc First\npick def Second\n',
                             fil.read())

    def test_seq_edit_env_edit_line(self):
        """Test seq_edit_env with specific line number"""
        env = cmdgit.seq_edit_env('edit', 2)
        todo = os.path.join(self.test_dir, 'todo')
        with open(todo, 'w', encoding='utf-8') as fil:
            fil.write('pick abc First\npick def Second\npick ghi Third\n')
        os.system(f'{env["GIT_SEQUENCE_EDITOR"]} {todo}')
        with open(todo, encoding='utf-8') as fil:
            self.assertEqual(
                'pick abc First\nedit def Second\npick ghi Third\n',
                fil.read())

    def test_show_rebase_status_success(self):
        """Test show_rebase_status parses success message"""
        with terminal.capture() as (out, _):
            cmdgit.show_rebase_status(
                'Successfully rebased and updated refs/heads/main')
        self.assertEqual(
            'Successfully rebased and updated refs/heads/main\n',
            out.getvalue())

    def test_show_rebase_status_stopped(self):
        """Test show_rebase_status parses stopped message"""
        with mock.patch.object(cmdgit, 'get_rebase_position', return_value=''):
            with terminal.capture() as (out, _):
                cmdgit.show_rebase_status(
                    'Stopped at abc1234... Fix the bug')
        self.assertEqual(
            'Rebasing: stopped at abc1234... Fix the bug\n', out.getvalue())

    def test_show_rebase_status_conflict(self):
        """Test show_rebase_status parses conflict message"""
        with mock.patch.object(cmdgit, 'get_rebase_position',
                               return_value='3/5'):
            with mock.patch.object(cmdgit, 'has_conflicts',
                                   return_value=True):
                with terminal.capture() as (out, _):
                    cmdgit.show_rebase_status(
                        'Could not apply abc1234... Fix bug', return_code=1)
        self.assertEqual(
            'Rebasing 3/5: conflict in abc1234... Fix bug\n', out.getvalue())

    def test_show_rebase_status_empty(self):
        """Test show_rebase_status detects empty commit"""
        with mock.patch.object(cmdgit, 'get_rebase_position',
                               return_value='7/7'):
            with mock.patch.object(cmdgit, 'has_conflicts',
                                   return_value=False):
                with terminal.capture() as (out, _):
                    cmdgit.show_rebase_status(
                        'Could not apply abc1234... wip', return_code=1)
        self.assertEqual(
            'Rebasing 7/7: empty commit abc1234... wip\n', out.getvalue())

    def test_has_unstaged_changes_none(self):
        """Test has_unstaged_changes returns False when no changes"""
        with mock.patch('uman_pkg.cmdgit.git_output', return_value=''):
            self.assertFalse(cmdgit.has_unstaged_changes())

    def test_has_unstaged_changes_present(self):
        """Test has_unstaged_changes returns True when changes exist"""
        # git diff --quiet exits with 1 when there are changes
        exc = command.CommandExc('diff shows changes',
                                 command.CommandResult(return_code=1))
        with mock.patch('uman_pkg.cmdgit.git_output', side_effect=exc):
            self.assertTrue(cmdgit.has_unstaged_changes())

    def test_do_rc(self):
        """Test do_rc runs git rebase --continue"""
        cap = []

        def mock_git(*args, env=None, dry_run=None):
            del env, dry_run
            cap.append(args)
            return mock.Mock(return_code=0, stdout='', stderr='')

        args = cmdline.parse_args(['git', 'rc'])
        with mock.patch('uman_pkg.cmdgit.git', mock_git):
            with mock.patch.object(cmdgit, 'get_rebase_dir',
                                   return_value='/tmp/rebase'):
                result = cmdgit.do_rc(args)
        self.assertEqual(0, result.return_code)
        self.assertEqual(('rebase', '--continue'), cap[0])

    def test_do_rc_not_rebasing(self):
        """Test do_rc prints error when not rebasing"""
        args = cmdline.parse_args(['git', 'rc'])
        with mock.patch.object(cmdgit, 'get_rebase_dir', return_value=None):
            with terminal.capture() as (_, err):
                result = cmdgit.do_rc(args)
        self.assertEqual(1, result)
        self.assertEqual('Not in the middle of a rebase\n', err.getvalue())

    def test_do_rs(self):
        """Test do_rs runs git rebase --skip"""
        cap = []

        def mock_git(*args, env=None, dry_run=None):
            del env, dry_run
            cap.append(args)
            return mock.Mock(return_code=0, stdout='', stderr='')

        args = cmdline.parse_args(['git', 'rs'])
        with mock.patch('uman_pkg.cmdgit.git', mock_git):
            with mock.patch.object(cmdgit, 'get_rebase_dir',
                                   return_value='/tmp/rebase'):
                result = cmdgit.do_rs(args)
        self.assertEqual(0, result.return_code)
        self.assertEqual(('rebase', '--skip'), cap[0])

    def test_do_rs_not_rebasing(self):
        """Test do_rs prints error when not rebasing"""
        args = cmdline.parse_args(['git', 'rs'])
        with mock.patch.object(cmdgit, 'get_rebase_dir', return_value=None):
            with terminal.capture() as (_, err):
                result = cmdgit.do_rs(args)
        self.assertEqual(1, result)
        self.assertEqual('Not in the middle of a rebase\n', err.getvalue())

    def test_do_ra(self):
        """Test do_ra runs git rebase --abort"""
        cap = []

        def mock_git(*args, env=None, dry_run=None):
            del env, dry_run
            cap.append(args)
            return mock.Mock(return_code=0, stdout='', stderr='')

        def mock_git_output(*args):
            if args[0] == 'status':
                return ''  # No uncommitted changes
            if args[0] == 'rev-parse':
                return 'abc123'
            return ''

        args = cmdline.parse_args(['git', 'ra'])
        with mock.patch('uman_pkg.cmdgit.git', mock_git):
            with mock.patch('uman_pkg.cmdgit.git_output', mock_git_output):
                with mock.patch.object(cmdgit, 'get_rebase_dir',
                                       return_value='/tmp/rebase'):
                    with terminal.capture() as (out, err):
                        result = cmdgit.do_ra(args)
        self.assertEqual(0, result.return_code)
        self.assertEqual(('rebase', '--abort'), cap[0])
        self.assertEqual(
            'No uncommitted changes\n'
            'Current HEAD: abc123 (use "git reset --hard abc123" to recover)\n'
            'Rebase aborted\n', out.getvalue())
        self.assertFalse(err.getvalue())

    def test_do_ra_not_rebasing(self):
        """Test do_ra prints error when not rebasing"""
        args = cmdline.parse_args(['git', 'ra'])
        with mock.patch.object(cmdgit, 'get_rebase_dir', return_value=None):
            with terminal.capture() as (_, err):
                result = cmdgit.do_ra(args)
        self.assertEqual(1, result)
        self.assertEqual('Not in the middle of a rebase\n', err.getvalue())

    def test_do_ra_with_changes(self):
        """Test do_ra stashes uncommitted changes before aborting"""
        cap = []

        def mock_git(*args, env=None, dry_run=None):
            del env, dry_run
            cap.append(args)
            return mock.Mock(return_code=0, stdout='', stderr='')

        def mock_git_output(*args):
            if args[0] == 'status':
                return 'M  file1.txt\nA  file2.txt'
            if args[0] == 'rev-parse':
                return 'abc123'
            return ''

        args = cmdline.parse_args(['git', 'ra'])
        with mock.patch('uman_pkg.cmdgit.git', mock_git):
            with mock.patch('uman_pkg.cmdgit.git_output', mock_git_output):
                with mock.patch.object(cmdgit, 'get_rebase_dir',
                                       return_value='/tmp/rebase'):
                    with terminal.capture() as (out, err):
                        result = cmdgit.do_ra(args)
        self.assertEqual(0, result.return_code)
        # Check stash was called with named stash including commit hash
        self.assertEqual(('stash', 'push', '-m', 'uman-abort-abc123'), cap[0])
        self.assertEqual(
            'Stashed as "uman-abort-abc123" (use "git stash pop" to recover)\n'
            'Current HEAD: abc123 (use "git reset --hard abc123" to recover)\n'
            'Rebase aborted\n', out.getvalue())
        self.assertFalse(err.getvalue())

    def test_do_gr(self):
        """Test do_gr opens interactive rebase editor"""
        args = cmdline.parse_args(['git', 'gr'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            with mock.patch.object(cmdgit, 'get_upstream',
                                   return_value='origin/main'):
                result = cmdgit.do_gr(args)
        self.assertEqual(0, result)
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        self.assertEqual(('git', 'rebase', '-i', 'origin/main'), call_args[0])
        self.assertFalse(call_args[1]['capture'])

    def test_do_gr_with_count(self):
        """Test do_gr N rebases last N commits"""
        args = cmdline.parse_args(['git', 'gr', '3'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            result = cmdgit.do_gr(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args
        self.assertEqual(('git', 'rebase', '-i', 'HEAD~3'), call_args[0])
        self.assertFalse(call_args[1]['capture'])

    def test_do_et(self):
        """Test do_et runs git rebase --edit-todo"""
        args = cmdline.parse_args(['git', 'et'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            with mock.patch.object(cmdgit, 'get_rebase_dir',
                                   return_value='/tmp/rebase'):
                result = cmdgit.do_et(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args
        self.assertEqual(('git', 'rebase', '--edit-todo'), call_args[0])

    def test_do_et_not_rebasing(self):
        """Test do_et prints error when not rebasing"""
        args = cmdline.parse_args(['git', 'et'])
        with mock.patch.object(cmdgit, 'get_rebase_dir', return_value=None):
            with terminal.capture() as (_, err):
                result = cmdgit.do_et(args)
        self.assertEqual(1, result)
        self.assertEqual('Not in the middle of a rebase\n', err.getvalue())

    def test_do_re(self):
        """Test do_re runs git commit --amend"""
        args = cmdline.parse_args(['git', 're'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            with mock.patch.object(cmdgit, 'get_rebase_dir',
                                   return_value='/tmp/rebase'):
                result = cmdgit.do_re(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args
        self.assertEqual(('git', 'commit', '--amend'), call_args[0])

    def test_do_re_not_rebasing(self):
        """Test do_re prints error when not rebasing"""
        args = cmdline.parse_args(['git', 're'])
        with mock.patch.object(cmdgit, 'get_rebase_dir', return_value=None):
            with terminal.capture() as (_, err):
                result = cmdgit.do_re(args)
        self.assertEqual(1, result)
        self.assertEqual('Not in the middle of a rebase\n', err.getvalue())

    def test_do_us(self):
        """Test do_us sets upstream branch"""
        cap = []

        def mock_git(*args, env=None, dry_run=None):
            del env, dry_run
            cap.append(args)
            return mock.Mock(return_code=0, stdout='', stderr='')

        args = cmdline.parse_args(['git', 'us'])
        with mock.patch('uman_pkg.cmdgit.git', mock_git):
            with mock.patch('uman_pkg.cmdgit.git_output',
                            return_value='my-branch'):
                with terminal.capture() as (out, _):
                    result = cmdgit.do_us(args)
        self.assertEqual(0, result)
        self.assertEqual(
            ('branch', '--set-upstream-to', 'm/master', 'my-branch'), cap[0])
        self.assertEqual(
            'Set upstream of my-branch to m/master\n', out.getvalue())

    def test_do_ol(self):
        """Test do_ol shows oneline log"""
        args = cmdline.parse_args(['git', 'ol'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            with mock.patch.object(cmdgit, 'get_upstream',
                                   return_value='origin/main'):
                result = cmdgit.do_ol(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args[0]
        self.assertEqual(
            ('git', 'log', '--oneline', '--decorate', 'origin/main..'),
            call_args)

    def test_do_ol_with_count(self):
        """Test do_ol N shows last N commits"""
        args = cmdline.parse_args(['git', 'ol', '5'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            result = cmdgit.do_ol(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args[0]
        self.assertEqual(
            ('git', 'log', '--oneline', '--decorate', '-5'), call_args)

    def test_do_ol_with_path(self):
        """Test do_ol with file path filters by that file"""
        args = cmdline.parse_args(['git', 'ol', 'boot/scene.c'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            with mock.patch.object(cmdgit, 'get_upstream',
                                   return_value='origin/main'):
                result = cmdgit.do_ol(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args[0]
        self.assertEqual(
            ('git', 'log', '--oneline', '--decorate', 'origin/main..',
             '--', 'boot/scene.c'), call_args)

    def test_do_pe(self):
        """Test do_pe shows last 10 commits"""
        args = cmdline.parse_args(['git', 'pe'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            result = cmdgit.do_pe(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args[0]
        self.assertEqual(
            ('git', 'log', '--oneline', '-n10', '--decorate'), call_args)

    def test_do_am(self):
        """Test do_am runs git commit --amend"""
        args = cmdline.parse_args(['git', 'am'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            result = cmdgit.do_am(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args[0]
        self.assertEqual(('git', 'commit', '--amend'), call_args)

    def test_do_ams(self):
        """Test do_ams runs git commit --amend --signoff"""
        args = cmdline.parse_args(['git', 'ams'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            result = cmdgit.do_ams(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args[0]
        self.assertEqual(('git', 'commit', '--amend', '--signoff'), call_args)

    def test_do_au(self):
        """Test do_au runs git add -u"""
        args = cmdline.parse_args(['git', 'au'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            result = cmdgit.do_au(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args[0]
        self.assertEqual(('git', 'add', '-u'), call_args)

    def test_do_gd(self):
        """Test do_gd runs git difftool"""
        args = cmdline.parse_args(['git', 'gd'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            result = cmdgit.do_gd(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args[0]
        self.assertEqual(('git', 'difftool'), call_args)

    def test_do_gdc(self):
        """Test do_gdc runs git difftool --cached"""
        args = cmdline.parse_args(['git', 'gdc'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            result = cmdgit.do_gdc(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args[0]
        self.assertEqual(('git', 'difftool', '--cached'), call_args)

    def test_do_fm(self):
        """Test do_fm checks commits against us/master"""
        args = cmdline.parse_args(['git', 'fm'])
        with mock.patch.object(cmdgit, 'git_output') as mock_git:
            mock_git.side_effect = [
                'main',  # current branch
                'First commit\nSecond commit',  # branch commits
                'abc123 First commit\ndef456 Other commit',  # upstream log
            ]
            with terminal.capture() as (out, _):
                result = cmdgit.do_fm(args)
        self.assertEqual(0, result)
        # First commit found, second not found
        self.assertIn('Found: First commit', out.getvalue())
        self.assertIn('Not found: Second commit', out.getvalue())

    def test_do_fn(self):
        """Test do_fn checks commits against us/next"""
        args = cmdline.parse_args(['git', 'fn'])
        with mock.patch.object(cmdgit, 'git_output') as mock_git:
            mock_git.side_effect = [
                'main',  # current branch
                'Test commit',  # branch commits
                'abc123 Test commit',  # upstream log
            ]
            with terminal.capture() as (out, _):
                result = cmdgit.do_fn(args)
        self.assertEqual(0, result)
        self.assertIn('Found: Test commit', out.getvalue())

    def test_do_fci(self):
        """Test do_fci checks commits against ci/master"""
        args = cmdline.parse_args(['git', 'fci'])
        with mock.patch.object(cmdgit, 'git_output') as mock_git:
            mock_git.side_effect = [
                'main',  # current branch
                'CI commit',  # branch commits
                'abc123 Other stuff',  # upstream log (no match)
            ]
            with terminal.capture() as (out, _):
                result = cmdgit.do_fci(args)
        self.assertEqual(0, result)
        self.assertIn('Not found: CI commit', out.getvalue())

    def test_do_fa(self):
        """Test do_fa checks all branches against us/master"""
        args = cmdline.parse_args(['git', 'fa'])
        with mock.patch.object(cmdgit, 'git_output') as mock_git:
            mock_git.side_effect = [
                'branch-a\nbranch-b',  # branch list
                'Commit A',  # branch-a commits
                'abc123 Commit A',  # upstream log (match)
                'Commit B',  # branch-b commits
                'def456 Other stuff',  # upstream log (no match)
            ]
            with terminal.capture() as (out, _):
                result = cmdgit.do_fa(args)
        self.assertEqual(0, result)
        output = out.getvalue()
        self.assertIn('=== branch-a ===', output)
        self.assertIn('Found: Commit A', output)
        self.assertIn('=== branch-b ===', output)
        self.assertIn('Not found: Commit B', output)

    def test_do_gm(self):
        """Test do_gm searches us/master log"""
        args = cmdline.parse_args(['git', 'gm', 'pattern'])
        with mock.patch.object(cmdgit, 'git_output') as mock_git:
            mock_git.return_value = 'abc123 Found pattern here\ndef456 Other'
            with terminal.capture() as (out, _):
                result = cmdgit.do_gm(args)
        self.assertEqual(0, result)
        self.assertEqual('abc123 Found pattern here\n', out.getvalue())
        mock_git.assert_called_with('log', '--oneline', 'us/master')

    def test_do_gn(self):
        """Test do_gn searches us/next log"""
        args = cmdline.parse_args(['git', 'gn', 'test'])
        with mock.patch.object(cmdgit, 'git_output') as mock_git:
            mock_git.return_value = 'abc123 Test commit\ndef456 Other'
            with terminal.capture() as (out, _):
                result = cmdgit.do_gn(args)
        self.assertEqual(0, result)
        self.assertEqual('abc123 Test commit\n', out.getvalue())
        mock_git.assert_called_with('log', '--oneline', 'us/next')

    def test_do_gci(self):
        """Test do_gci searches ci/master log"""
        args = cmdline.parse_args(['git', 'gci', 'fix'])
        with mock.patch.object(cmdgit, 'git_output') as mock_git:
            mock_git.return_value = 'abc123 Fix bug\ndef456 Other'
            with terminal.capture() as (out, _):
                result = cmdgit.do_gci(args)
        self.assertEqual(0, result)
        self.assertEqual('abc123 Fix bug\n', out.getvalue())
        mock_git.assert_called_with('log', '--oneline', 'ci/master')

    def test_do_gm_no_pattern(self):
        """Test do_gm requires pattern"""
        args = cmdline.parse_args(['git', 'gm'])
        with terminal.capture() as (_, err):
            result = cmdgit.do_gm(args)
        self.assertEqual(1, result)
        self.assertIn('Pattern required', err.getvalue())

    def test_do_eg(self):
        """Test do_eg greps errno.h"""
        args = cmdline.parse_args(['git', 'eg', 'ENOENT'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            result = cmdgit.do_eg(args)
        self.assertEqual(0, result)
        mock_run.assert_called_with(
            'grep', 'ENOENT', 'include/linux/errno.h',
            capture=False, raise_on_error=False)

    def test_do_eg_no_pattern(self):
        """Test do_eg requires pattern"""
        args = cmdline.parse_args(['git', 'eg'])
        with terminal.capture() as (_, err):
            result = cmdgit.do_eg(args)
        self.assertEqual(1, result)
        self.assertIn('Pattern required', err.getvalue())

    def test_do_gb(self):
        """Test do_gb lists branches"""
        args = cmdline.parse_args(['git', 'gb'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            result = cmdgit.do_gb(args)
        self.assertEqual(0, result)
        mock_run.assert_called_with(
            'git', 'branch', capture=False, raise_on_error=False)

    def test_do_gba(self):
        """Test do_gba lists all branches"""
        args = cmdline.parse_args(['git', 'gba'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            result = cmdgit.do_gba(args)
        self.assertEqual(0, result)
        mock_run.assert_called_with(
            'git', 'branch', '-a', capture=False, raise_on_error=False)

    def test_do_cs(self):
        """Test do_cs runs git show"""
        args = cmdline.parse_args(['git', 'cs'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            result = cmdgit.do_cs(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args[0]
        self.assertEqual(('git', 'show'), call_args)

    def test_do_sc(self):
        """Test do_sc runs git show --stat"""
        args = cmdline.parse_args(['git', 'sc'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            result = cmdgit.do_sc(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args[0]
        self.assertEqual(('git', 'show', '--stat'), call_args)

    def test_do_g(self):
        """Test do_g runs git status -sb"""
        args = cmdline.parse_args(['git', 'g'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            result = cmdgit.do_g(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args[0]
        self.assertEqual(('git', 'status', '-sb'), call_args)

    def test_do_db(self):
        """Test do_db diffs commit files against upstream"""
        args = cmdline.parse_args(['git', 'db'])
        with mock.patch.object(cmdgit, 'git_output') as mock_git:
            mock_git.side_effect = [
                '1\t0\tfile1.c\n2\t1\tfile2.h',  # numstat output
            ]
            with mock.patch('u_boot_pylib.command.run_one') as mock_run:
                mock_run.return_value = mock.Mock(return_code=0)
                with mock.patch.object(cmdgit, 'get_upstream',
                                       return_value='origin/main'):
                    with terminal.capture():
                        result = cmdgit.do_db(args)
        self.assertEqual(0, result)
        # Check difftool was called with the files
        call_args = mock_run.call_args[0]
        self.assertEqual(
            ('git', 'difftool', 'origin/main', '--', 'file1.c', 'file2.h'),
            call_args)

    def test_do_db_with_branch(self):
        """Test do_db diffs against specified branch"""
        args = cmdline.parse_args(['git', 'db', 'expf'])
        with mock.patch.object(cmdgit, 'git_output') as mock_git:
            mock_git.side_effect = [
                '1\t0\tfile1.c',  # numstat output
            ]
            with mock.patch('u_boot_pylib.command.run_one') as mock_run:
                mock_run.return_value = mock.Mock(return_code=0)
                with terminal.capture():
                    result = cmdgit.do_db(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args[0]
        self.assertEqual(('git', 'difftool', 'expf', '--', 'file1.c'), call_args)

    def test_do_dh(self):
        """Test do_dh runs git difftool HEAD~"""
        args = cmdline.parse_args(['git', 'di'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            result = cmdgit.do_dh(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args[0]
        self.assertEqual(('git', 'difftool', 'HEAD~'), call_args)

    def test_do_dh_with_count(self):
        """Test do_dh N runs git difftool HEAD~N"""
        args = cmdline.parse_args(['git', 'di', '3'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            result = cmdgit.do_dh(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args[0]
        self.assertEqual(('git', 'difftool', 'HEAD~3'), call_args)

    def test_do_dh_with_file(self):
        """Test do_dh with file path passes it to difftool"""
        args = cmdline.parse_args(['git', 'di', 'some/file.c'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            result = cmdgit.do_dh(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args[0]
        self.assertEqual(('git', 'difftool', 'HEAD~', '--', 'some/file.c'),
                         call_args)

    def test_do_dh_with_count_and_file(self):
        """Test do_dh N file passes both to difftool"""
        args = cmdline.parse_args(['git', 'di', '2', 'some/file.c'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            result = cmdgit.do_dh(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args[0]
        self.assertEqual(('git', 'difftool', 'HEAD~2', '--', 'some/file.c'),
                         call_args)

    def test_do_sl(self):
        """Test do_sl runs git log --stat upstream.."""
        args = cmdline.parse_args(['git', 'sl'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            with mock.patch.object(cmdgit, 'get_upstream',
                                   return_value='origin/main'):
                result = cmdgit.do_sl(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args[0]
        self.assertEqual(('git', 'log', '--stat', 'origin/main..'), call_args)

    def test_do_sl_with_count(self):
        """Test do_sl N runs git log --stat -N"""
        args = cmdline.parse_args(['git', 'sl', '5'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            result = cmdgit.do_sl(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args[0]
        self.assertEqual(('git', 'log', '--stat', '-5'), call_args)

    def test_do_sl_with_path(self):
        """Test do_sl with file path filters by that file"""
        args = cmdline.parse_args(['git', 'sl', 'common/cmd_ut.c'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            with mock.patch.object(cmdgit, 'get_upstream',
                                   return_value='origin/main'):
                result = cmdgit.do_sl(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args[0]
        self.assertEqual(
            ('git', 'log', '--stat', 'origin/main..', '--', 'common/cmd_ut.c'),
            call_args)

    def test_do_cm(self):
        """Test do_cm runs git commit"""
        args = cmdline.parse_args(['git', 'cm'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            result = cmdgit.do_cm(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args[0]
        self.assertEqual(('git', 'commit'), call_args)

    def test_do_cms(self):
        """Test do_cms runs git commit --signoff"""
        args = cmdline.parse_args(['git', 'cms'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            result = cmdgit.do_cms(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args[0]
        self.assertEqual(('git', 'commit', '--signoff'), call_args)

    def test_do_co(self):
        """Test do_co runs git checkout"""
        args = cmdline.parse_args(['git', 'co'])
        with mock.patch('uman_pkg.cmdgit.exec_cmd') as mock_exec:
            mock_exec.return_value = mock.Mock(return_code=0)
            result = cmdgit.do_co(args)
        self.assertEqual(0, result)
        call_args = mock_exec.call_args[0]
        self.assertEqual(['git', 'checkout'], call_args[0])

    def test_do_co_with_flags(self):
        """Test do_co passes through flags to git checkout"""
        args = cmdline.parse_args(['-b', 'dock', 'gh/dock'],
                                  prog_name='co')
        with mock.patch('uman_pkg.cmdgit.exec_cmd') as mock_exec:
            mock_exec.return_value = mock.Mock(return_code=0)
            result = cmdgit.do_co(args)
        self.assertEqual(0, result)
        call_args = mock_exec.call_args[0]
        self.assertEqual(['git', 'checkout', '-b', 'dock', 'gh/dock'],
                         call_args[0])

    def test_do_gp(self):
        """Test do_gp runs git cherry-pick"""
        args = cmdline.parse_args(['git', 'gp', 'abc123'])
        with mock.patch('uman_pkg.cmdgit.exec_cmd') as mock_exec:
            mock_exec.return_value = mock.Mock(return_code=0)
            result = cmdgit.do_gp(args)
        self.assertEqual(0, result)
        call_args = mock_exec.call_args[0]
        self.assertEqual(['git', 'cherry-pick', 'abc123'], call_args[0])

    def test_do_sd(self):
        """Test do_sd shows commit using difftool"""
        args = cmdline.parse_args(['git', 'sd'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            result = cmdgit.do_sd(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args[0]
        self.assertEqual(('git', 'difftool', 'HEAD~..HEAD'), call_args)

    def test_do_sd_with_ref(self):
        """Test do_sd with specific commit reference"""
        args = cmdline.parse_args(['git', 'sd', 'abc123'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            result = cmdgit.do_sd(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args[0]
        self.assertEqual(('git', 'difftool', 'abc123~..abc123'), call_args)

    def test_do_st(self):
        """Test do_st runs git stash"""
        args = cmdline.parse_args(['git', 'st'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            result = cmdgit.do_st(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args[0]
        self.assertEqual(('git', 'stash'), call_args)

    def test_do_ust(self):
        """Test do_ust runs git stash pop"""
        args = cmdline.parse_args(['git', 'ust'])
        with mock.patch('u_boot_pylib.command.run_one') as mock_run:
            mock_run.return_value = mock.Mock(return_code=0)
            result = cmdgit.do_ust(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args[0]
        self.assertEqual(('git', 'stash', 'pop'), call_args)

    def test_do_pm(self):
        """Test do_pm applies patch from rebase directory"""
        args = cmdline.parse_args(['git', 'pm'])
        patch_content = b'--- a/file\n+++ b/file\n@@ -1 +1 @@\n-old\n+new\n'

        with mock.patch.object(cmdgit, 'get_rebase_dir',
                               return_value=self.test_dir):
            # Create patch file
            patch_file = os.path.join(self.test_dir, 'patch')
            tools.write_file(patch_file, patch_content)
            with mock.patch('u_boot_pylib.command.run_one') as mock_run:
                mock_run.return_value = mock.Mock(return_code=0)
                result = cmdgit.do_pm(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args
        self.assertEqual(('patch', '-p1', '--merge'), call_args[0])

    def test_do_pm_not_rebasing(self):
        """Test do_pm prints error when not rebasing"""
        args = cmdline.parse_args(['git', 'pm'])
        with mock.patch.object(cmdgit, 'get_rebase_dir', return_value=None):
            with terminal.capture() as (_, err):
                result = cmdgit.do_pm(args)
        self.assertEqual(1, result)
        self.assertEqual('Not in the middle of a rebase\n', err.getvalue())

    def test_do_pm_no_patch_file(self):
        """Test do_pm prints error when patch file not found"""
        args = cmdline.parse_args(['git', 'pm'])
        with mock.patch.object(cmdgit, 'get_rebase_dir',
                               return_value=self.test_dir):
            with terminal.capture() as (_, err):
                result = cmdgit.do_pm(args)
        self.assertEqual(1, result)
        self.assertEqual(
            'No patch file found in rebase directory\n', err.getvalue())

    def test_do_rd(self):
        """Test do_rd shows diff against next commit in rebase"""
        args = cmdline.parse_args(['git', 'rd'])
        todo_content = b'pick abc1234 First commit\npick def5678 Second commit\n'

        with mock.patch.object(cmdgit, 'get_rebase_dir',
                               return_value=self.test_dir):
            todo_file = os.path.join(self.test_dir, 'git-rebase-todo')
            tools.write_file(todo_file, todo_content)
            with mock.patch('u_boot_pylib.command.run_one') as mock_run:
                mock_run.return_value = mock.Mock(return_code=0)
                result = cmdgit.do_rd(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args
        self.assertEqual(('git', 'difftool', 'abc1234'), call_args[0])

    def test_do_rd_with_arg(self):
        """Test do_rd N shows diff against nth commit"""
        args = cmdline.parse_args(['git', 'rd', '2'])
        todo_content = b'pick abc1234 First commit\npick def5678 Second commit\n'

        with mock.patch.object(cmdgit, 'get_rebase_dir',
                               return_value=self.test_dir):
            todo_file = os.path.join(self.test_dir, 'git-rebase-todo')
            tools.write_file(todo_file, todo_content)
            with mock.patch('u_boot_pylib.command.run_one') as mock_run:
                mock_run.return_value = mock.Mock(return_code=0)
                result = cmdgit.do_rd(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args
        self.assertEqual(('git', 'difftool', 'def5678'), call_args[0])

    def test_do_rd_not_rebasing(self):
        """Test do_rd prints error when not rebasing"""
        args = cmdline.parse_args(['git', 'rd'])
        with mock.patch.object(cmdgit, 'get_rebase_dir', return_value=None):
            with terminal.capture() as (_, err):
                result = cmdgit.do_rd(args)
        self.assertEqual(1, result)
        self.assertEqual('Not in the middle of a rebase\n', err.getvalue())

    def test_do_rd_no_commit(self):
        """Test do_rd prints error when commit not found"""
        args = cmdline.parse_args(['git', 'rd', '5'])
        todo_content = b'pick abc1234 First commit\n'

        with mock.patch.object(cmdgit, 'get_rebase_dir',
                               return_value=self.test_dir):
            todo_file = os.path.join(self.test_dir, 'git-rebase-todo')
            tools.write_file(todo_file, todo_content)
            with terminal.capture() as (_, err):
                result = cmdgit.do_rd(args)
        self.assertEqual(1, result)
        self.assertEqual('No commit found at position 5\n', err.getvalue())

    def test_do_rd_with_file(self):
        """Test do_rd with file path passes it to difftool"""
        args = cmdline.parse_args(['git', 'rd', 'some/file.c'])
        todo_content = b'pick abc1234 First commit\n'

        with mock.patch.object(cmdgit, 'get_rebase_dir',
                               return_value=self.test_dir):
            todo_file = os.path.join(self.test_dir, 'git-rebase-todo')
            tools.write_file(todo_file, todo_content)
            with mock.patch('u_boot_pylib.command.run_one') as mock_run:
                mock_run.return_value = mock.Mock(return_code=0)
                result = cmdgit.do_rd(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args[0]
        self.assertEqual(('git', 'difftool', 'abc1234', '--', 'some/file.c'),
                         call_args)

    def test_do_rd_with_count_and_file(self):
        """Test do_rd N file passes both to difftool"""
        args = cmdline.parse_args(['git', 'rd', '2', 'some/file.c'])
        todo_content = b'pick abc1234 First commit\npick def5678 Second commit\n'

        with mock.patch.object(cmdgit, 'get_rebase_dir',
                               return_value=self.test_dir):
            todo_file = os.path.join(self.test_dir, 'git-rebase-todo')
            tools.write_file(todo_file, todo_content)
            with mock.patch('u_boot_pylib.command.run_one') as mock_run:
                mock_run.return_value = mock.Mock(return_code=0)
                result = cmdgit.do_rd(args)
        self.assertEqual(0, result)
        call_args = mock_run.call_args[0]
        self.assertEqual(('git', 'difftool', 'def5678', '--', 'some/file.c'),
                         call_args)

    def test_do_rb(self):
        """Test do_rb rebases to upstream, stopping before first commit"""
        cap = []
        cap_env = []

        def mock_git(*args, env=None, dry_run=None):
            del dry_run
            cap.append(args)
            cap_env.append(env)
            return mock.Mock(return_code=0, stdout='', stderr='')

        args = cmdline.parse_args(['git', 'rb'])
        with mock.patch('uman_pkg.cmdgit.git', mock_git):
            with mock.patch.object(cmdgit, 'get_upstream',
                                   return_value='origin/main'):
                result = cmdgit.do_rb(args)
        self.assertEqual(0, result.return_code)
        self.assertEqual(('rebase', '-i', 'origin/main'), cap[0])
        self.assertIn('GIT_SEQUENCE_EDITOR', cap_env[0])
        # rb inserts a break before first commit to stop at upstream
        self.assertIn("insert(0,'break", cap_env[0]['GIT_SEQUENCE_EDITOR'])

    def test_do_rf_no_arg(self):
        """Test do_rf without arg uses upstream"""
        cap = []
        cap_env = []

        def mock_git(*args, env=None, dry_run=None):
            del dry_run
            cap.append(args)
            cap_env.append(env)
            return mock.Mock(return_code=0, stdout='', stderr='')

        args = cmdline.parse_args(['git', 'rf'])
        with mock.patch('uman_pkg.cmdgit.git', mock_git):
            with mock.patch.object(cmdgit, 'get_upstream',
                                   return_value='origin/main'):
                with mock.patch.object(cmdgit, 'has_unstaged_changes',
                                       return_value=False):
                    result = cmdgit.do_rf(args)
        self.assertEqual(0, result.return_code)
        self.assertEqual(('rebase', '-i', 'origin/main'), cap[0])
        self.assertIn("re.sub(r'^pick','edit',lines[0])",
                      cap_env[0]['GIT_SEQUENCE_EDITOR'])

    def test_do_rf_with_count(self):
        """Test do_rf with commit count"""
        cap = []

        def mock_git(*args, env=None, dry_run=None):
            del env, dry_run
            cap.append(args)
            return mock.Mock(return_code=0, stdout='', stderr='')

        args = cmdline.parse_args(['git', 'rf', '5'])
        with mock.patch('uman_pkg.cmdgit.git', mock_git):
            with mock.patch.object(cmdgit, 'has_unstaged_changes',
                                   return_value=False):
                result = cmdgit.do_rf(args)
        self.assertEqual(0, result.return_code)
        self.assertEqual(('rebase', '-i', 'HEAD~5'), cap[0])

    def test_do_rf_with_hash(self):
        """Test do_rf with a commit hash rebases from its parent"""
        cap = []

        def mock_git(*args, env=None, dry_run=None):
            del env, dry_run
            cap.append(args)
            return mock.Mock(return_code=0, stdout='', stderr='')

        args = cmdline.parse_args(['git', 'rf', 'abc1234'])
        with mock.patch('uman_pkg.cmdgit.git', mock_git):
            with mock.patch.object(cmdgit, 'has_unstaged_changes',
                                   return_value=False):
                result = cmdgit.do_rf(args)
        self.assertEqual(0, result.return_code)
        self.assertEqual(('rebase', '-i', 'abc1234~1'), cap[0])

    def test_do_rp_with_hash(self):
        """Test do_rp with a commit hash"""
        cap_env = []

        def mock_git(*args, env=None, dry_run=None):
            del args, dry_run
            cap_env.append(env)
            return mock.Mock(return_code=0, stdout='', stderr='')

        args = cmdline.parse_args(['git', 'rp', 'abc1234'])
        with mock.patch('uman_pkg.cmdgit.git', mock_git):
            with mock.patch.object(cmdgit, 'get_upstream',
                                   return_value='origin/main'):
                result = cmdgit.do_rp(args)
        self.assertEqual(0, result.return_code)
        editor = cap_env[0]['GIT_SEQUENCE_EDITOR']
        self.assertIn('abc1234', editor)
        self.assertIn('edit', editor)

    def test_do_rf_unstaged_changes(self):
        """Test do_rf fails with unstaged changes"""
        args = cmdline.parse_args(['git', 'rf'])
        with mock.patch.object(cmdgit, 'has_unstaged_changes',
                               return_value=True):
            with terminal.capture() as (_, err):
                result = cmdgit.do_rf(args)
        self.assertEqual(1, result)
        self.assertIn('Unstaged changes', err.getvalue())

    def test_do_rp_requires_arg(self):
        """Test do_rp requires patch number"""
        args = cmdline.parse_args(['git', 'rp'])
        with terminal.capture() as (_, err):
            result = cmdgit.do_rp(args)
        self.assertEqual(1, result)
        self.assertIn('Patch number or commit hash required', err.getvalue())


class TestGitRebase(TestBase, GitRepoMixin):
    """Integration tests for git rebase subcommands using real git repos"""

    def setUp(self):
        """Create a test git repository with commits"""
        super().setUp()
        tout.init(tout.NOTICE)
        self.setup_git_repo()

    def tearDown(self):
        """Return to original directory and clean up"""
        self.teardown_git_repo()
        super().tearDown()

    def is_rebasing(self):
        """Check if a rebase is in progress"""
        return cmdgit.get_rebase_dir() is not None

    def get_head_subject(self):
        """Get the subject of HEAD commit"""
        return command.output('git', 'log', '-1', '--format=%s').strip()

    def test_get_upstream_no_stderr(self):
        """Test get_upstream doesn't print git errors in detached HEAD"""
        # Detach HEAD so @{upstream} will fail
        head = command.output('git', 'rev-parse', 'HEAD').strip()
        command.run_one('git', 'checkout', head, **CAPTURE)

        with terminal.capture() as (out, err):
            result = cmdgit.get_upstream()

        # Should not have git's "fatal:" error message
        self.assertNotIn('fatal:', err.getvalue())
        # Should still find upstream via @{-1} fallback
        self.assertIsNotNone(result)

    def test_rb_stops_at_upstream(self):
        """Test rb rebases to upstream and stops before first commit"""
        args = cmdline.parse_args(['git', 'rb'])
        with mock.patch('uman_pkg.cmdgit.git') as mock_git:
            mock_git.return_value = mock.Mock(return_code=0, stdout='', stderr='')
            result = cmdgit.do_rb(args)

        self.assertEqual(0, result.return_code)
        mock_git.assert_called_once()
        call_args = mock_git.call_args
        self.assertEqual(('rebase', '-i', 'upstream'), call_args[0])
        # Check that GIT_SEQUENCE_EDITOR inserts break before first commit
        env = call_args[1]['env']
        self.assertIn("insert(0,'break", env['GIT_SEQUENCE_EDITOR'])

    def test_rf_with_count(self):
        """Test rf N rebases last N commits, stopping at first"""
        args = cmdline.parse_args(['git', 'rf', '3'])
        with mock.patch('uman_pkg.cmdgit.git') as mock_git:
            mock_git.return_value = mock.Mock(return_code=0, stdout='', stderr='')
            result = cmdgit.do_rf(args)

        self.assertEqual(0, result.return_code)
        call_args = mock_git.call_args
        self.assertEqual(('rebase', '-i', 'HEAD~3'), call_args[0])
        env = call_args[1]['env']
        self.assertIn("re.sub(r'^pick','edit',lines[0])",
                      env['GIT_SEQUENCE_EDITOR'])

    def test_rp_stops_at_patch_n(self):
        """Test rp N stops at patch N"""
        args = cmdline.parse_args(['git', 'rp', '2'])
        with mock.patch('uman_pkg.cmdgit.git') as mock_git:
            mock_git.return_value = mock.Mock(return_code=0, stdout='', stderr='')
            result = cmdgit.do_rp(args)

        self.assertEqual(0, result.return_code)
        call_args = mock_git.call_args
        self.assertEqual(('rebase', '-i', 'upstream'), call_args[0])
        env = call_args[1]['env']
        self.assertIn("re.sub(r'^pick','edit',lines[1])",
                      env['GIT_SEQUENCE_EDITOR'])

    def test_real_rf_and_rc(self):
        """Test a real rf followed by rc to complete rebase"""
        # Start rebase with rf 2 (last 2 commits)
        args = cmdline.parse_args(['git', 'rf', '2'])
        with terminal.capture() as (out, err):
            result = cmdgit.do_rf(args)
        self.assertEqual(0, result.return_code)
        self.assertFalse(result.stdout)
        self.assertRegex(
            result.stderr,
            r'Rebasing \(1/2\)\r\r\x1b\[KStopped at [0-9a-f]+\.\.\.  Commit 3\n'
            r'You can amend the commit now, with\n\n'
            r'  git commit --amend \n\n'
            r'Once you are satisfied with your changes, run\n\n'
            r'  git rebase --continue\n')
        self.assertRegex(out.getvalue(),
                         r'Rebasing \d+/\d+: stopped at [0-9a-f]+\.\.\. Commit 3\n')
        self.assertFalse(err.getvalue())

        # Should now be in a rebase, stopped at first commit
        self.assertTrue(self.is_rebasing())
        self.assertEqual('Commit 3', self.get_head_subject())

        # Amend the commit (modify its own file to avoid conflicts)
        content = tools.read_file('file3.txt', binary=False)
        tools.write_file('file3.txt', content + 'amended\n', binary=False)
        command.output('git', 'add', 'file3.txt')
        command.output('git', 'commit', '--amend', '--no-edit')

        # Continue with rc
        args = cmdline.parse_args(['git', 'rc'])
        with terminal.capture() as (out, err):
            result = cmdgit.do_rc(args)
        self.assertEqual(0, result.return_code)
        self.assertFalse(result.stdout)
        self.assertRegex(
            result.stderr,
            r'Rebasing \(2/2\)\r\r\x1b\[K'
            r'Successfully rebased and updated refs/heads/master\.\n')
        self.assertRegex(out.getvalue(),
                         r'Successfully rebased and updated refs/heads/\w+\n')
        self.assertFalse(err.getvalue())

        # Rebase should be complete
        self.assertFalse(self.is_rebasing())
        # The amended content should be in the file
        self.assertIn('amended', tools.read_file('file3.txt', binary=False))

    def test_real_rf_rn_rc(self):
        """Test rf to start, rn to continue to next, then rc to finish"""
        # Start rebase with rf 3 (last 3 commits)
        args = cmdline.parse_args(['git', 'rf', '3'])
        with terminal.capture() as (out, err):
            result = cmdgit.do_rf(args)
        self.assertEqual(0, result.return_code)
        self.assertFalse(result.stdout)
        self.assertRegex(out.getvalue(),
                         r'Rebasing \d+/\d+: stopped at [0-9a-f]+\.\.\. Commit 2\n')
        self.assertFalse(err.getvalue())

        # Should be stopped at Commit 2
        self.assertTrue(self.is_rebasing())
        self.assertEqual('Commit 2', self.get_head_subject())

        # Use rn to continue to next commit (also set to edit)
        args = cmdline.parse_args(['git', 'rn'])
        with terminal.capture() as (out, err):
            result = cmdgit.do_rn(args)
        self.assertEqual(0, result.return_code)
        self.assertFalse(result.stdout)
        self.assertRegex(out.getvalue(),
                         r'Rebasing \d+/\d+: stopped at [0-9a-f]+\.\.\. Commit 3\n')
        self.assertFalse(err.getvalue())

        # Should be stopped at Commit 3
        self.assertTrue(self.is_rebasing())
        self.assertEqual('Commit 3', self.get_head_subject())

        # Use rc to continue without editing (should finish rebase)
        args = cmdline.parse_args(['git', 'rc'])
        with terminal.capture() as (out, err):
            result = cmdgit.do_rc(args)
        self.assertEqual(0, result.return_code)
        self.assertFalse(result.stdout)
        self.assertRegex(out.getvalue(),
                         r'Successfully rebased and updated refs/heads/\w+\n')
        self.assertFalse(err.getvalue())

        # Should be complete
        self.assertFalse(self.is_rebasing())
        self.assertEqual('Commit 4', self.get_head_subject())

    def test_real_rn_with_skip(self):
        """Test rn 2 to skip ahead and edit the 2nd remaining commit"""
        # Start rebase with rp 1 (stop at first commit)
        args = cmdline.parse_args(['git', 'rp', '1'])
        with terminal.capture() as (out, err):
            result = cmdgit.do_rp(args)
        self.assertEqual(0, result.return_code)
        self.assertFalse(result.stdout)
        self.assertRegex(out.getvalue(),
                         r'Rebasing \d+/\d+: stopped at [0-9a-f]+\.\.\. Commit 1\n')
        self.assertFalse(err.getvalue())

        # Should be stopped at Commit 1
        self.assertTrue(self.is_rebasing())
        self.assertEqual('Commit 1', self.get_head_subject())

        # Use rn 2 to set the 2nd remaining commit to edit
        # Todo has: Commit 2, Commit 3, Commit 4
        # rn 2 sets Commit 3 to edit and continues
        args = cmdline.parse_args(['git', 'rn', '2'])
        with terminal.capture() as (out, err):
            result = cmdgit.do_rn(args)
        self.assertEqual(0, result.return_code)
        self.assertFalse(result.stdout)
        self.assertRegex(out.getvalue(),
                         r'Rebasing \d+/\d+: stopped at [0-9a-f]+\.\.\. Commit 3\n')
        self.assertFalse(err.getvalue())

        # Should be stopped at Commit 3 (skipped Commit 2)
        self.assertTrue(self.is_rebasing())
        self.assertEqual('Commit 3', self.get_head_subject())

        # Use rc to finish
        args = cmdline.parse_args(['git', 'rc'])
        with terminal.capture() as (out, err):
            result = cmdgit.do_rc(args)
        self.assertEqual(0, result.return_code)
        self.assertFalse(result.stdout)
        self.assertRegex(out.getvalue(),
                         r'Successfully rebased and updated refs/heads/\w+\n')
        self.assertFalse(err.getvalue())

        self.assertFalse(self.is_rebasing())
        self.assertEqual('Commit 4', self.get_head_subject())

    def test_real_rs_skip_conflict(self):
        """Test rs to skip a conflicting commit"""
        # Create a conflicting situation: side branch creates file1.txt
        # (which Commit 1 also creates)
        command.output('git', 'checkout', '-b', 'side', 'upstream')
        tools.write_file('file1.txt', 'side branch file1\n', binary=False)
        command.output('git', 'add', 'file1.txt')
        command.output('git', 'commit', '-m', 'Side commit')

        # Go back to main and rebase onto side (Commit 1 will conflict)
        command.output('git', 'checkout', '-')
        command.output('git', 'branch', '--set-upstream-to=side')

        # Start rebase with rp 1 - will stop at Commit 1 with conflict
        args = cmdline.parse_args(['git', 'rp', '1'])
        with terminal.capture() as (out, err):
            result = cmdgit.do_rp(args)
        # rp 1 fails due to conflict, shows conflict message
        self.assertNotEqual(0, result.return_code)
        self.assertRegex(out.getvalue(),
                         r'Rebasing \d+/\d+: conflict in [0-9a-f]+\.\.\. Commit 1\n')
        self.assertFalse(err.getvalue())

        # Should be in rebase with a conflict (AA = add/add conflict)
        self.assertTrue(self.is_rebasing())
        status = command.output('git', 'status', '--porcelain')
        self.assertIn('AA', status)  # file1.txt has add/add conflict

        # Skip the conflicting commit with rs
        args = cmdline.parse_args(['git', 'rs'])
        with terminal.capture() as (out, err):
            result = cmdgit.do_rs(args)
        self.assertEqual(0, result.return_code)
        self.assertFalse(result.stdout)
        self.assertRegex(out.getvalue(),
                         r'Successfully rebased and updated refs/heads/\w+\n')
        self.assertFalse(err.getvalue())

        # Commits 2-4 should apply cleanly, rebase completes
        self.assertFalse(self.is_rebasing())
        self.assertEqual('Commit 4', self.get_head_subject())

    def test_real_rp_stops_at_second(self):
        """Test rp 2 stops at the second commit"""
        # Start rebase with rp 2 (stop at patch 2)
        args = cmdline.parse_args(['git', 'rp', '2'])
        with terminal.capture() as (out, err):
            result = cmdgit.do_rp(args)
        self.assertEqual(0, result.return_code)
        self.assertFalse(result.stdout)
        self.assertRegex(out.getvalue(),
                         r'Rebasing \d+/\d+: stopped at [0-9a-f]+\.\.\. Commit 2\n')
        self.assertFalse(err.getvalue())

        # Should be stopped at Commit 2 (the second patch from upstream)
        self.assertTrue(self.is_rebasing())
        self.assertEqual('Commit 2', self.get_head_subject())

        # Complete the rebase with rc
        args = cmdline.parse_args(['git', 'rc'])
        with terminal.capture() as (out, err):
            result = cmdgit.do_rc(args)
        self.assertEqual(0, result.return_code)
        self.assertFalse(result.stdout)
        self.assertRegex(out.getvalue(),
                         r'Successfully rebased and updated refs/heads/\w+\n')
        self.assertFalse(err.getvalue())

        # Rebase should complete with Commit 4 at HEAD
        self.assertFalse(self.is_rebasing())
        self.assertEqual('Commit 4', self.get_head_subject())

    def test_conflict_resolution_workflow(self):
        """Test resolving conflicts with rc, rn, and rs"""
        # Start rebase with rp 1 - stops at Commit 1
        args = cmdline.parse_args(['git', 'rp', '1'])
        with terminal.capture() as (out, err):
            result = cmdgit.do_rp(args)
        self.assertEqual(0, result.return_code)
        self.assertFalse(result.stdout)
        self.assertRegex(out.getvalue(),
                         r'Rebasing 1/4: stopped at [0-9a-f]+\.\.\. Commit 1\n')
        self.assertFalse(err.getvalue())
        self.assertTrue(self.is_rebasing())
        self.assertEqual('Commit 1', self.get_head_subject())

        # Create files that Commits 2-4 will also create - causes conflicts
        for i in range(2, 5):
            tools.write_file(f'file{i}.txt', 'created during rebase\n',
                             binary=False)
        command.output('git', 'add', 'file2.txt', 'file3.txt', 'file4.txt')
        command.output('git', 'commit', '--amend', '--no-edit')

        # rc - Commit 2 conflicts on file2.txt
        args = cmdline.parse_args(['git', 'rc'])
        with terminal.capture() as (out, err):
            result = cmdgit.do_rc(args)
        self.assertNotEqual(0, result.return_code)
        self.assertRegex(out.getvalue(),
                         r'Rebasing 2/4: conflict in [0-9a-f]+\.\.\. Commit 2\n')
        self.assertFalse(err.getvalue())
        self.assertEqual('AA file2.txt\n',
                         command.output('git', 'status', '--porcelain'))

        # Resolve file2.txt and use rn - stops at Commit 2 for review
        tools.write_file('file2.txt', 'resolved 2', binary=False)
        command.output('git', 'add', 'file2.txt')
        args = cmdline.parse_args(['git', 'rn'])
        with terminal.capture() as (out, err):
            result = cmdgit.do_rn(args)
        self.assertEqual(0, result.return_code)
        # After conflict resolution, rn shows position and commit for review
        self.assertRegex(out.getvalue(),
                         r'Rebasing 2/4: review  [0-9a-f]+\.\.\. Commit 2\n')
        self.assertFalse(err.getvalue())
        self.assertTrue(self.is_rebasing())
        self.assertEqual('Commit 2', self.get_head_subject())
        self.assertEqual('resolved 2', tools.read_file('file2.txt',
                                                       binary=False))

        # rn to continue - Commit 3 conflicts on file3.txt
        args = cmdline.parse_args(['git', 'rn'])
        with terminal.capture() as (out, err):
            result = cmdgit.do_rn(args)
        self.assertNotEqual(0, result.return_code)
        self.assertRegex(out.getvalue(),
                         r'Rebasing 3/4: conflict in [0-9a-f]+\.\.\. Commit 3\n')
        self.assertFalse(err.getvalue())
        self.assertEqual('AA file3.txt\n',
                         command.output('git', 'status', '--porcelain'))

        # Resolve file3.txt and use rn - stops at Commit 3 for review
        tools.write_file('file3.txt', 'resolved 3', binary=False)
        command.output('git', 'add', 'file3.txt')
        args = cmdline.parse_args(['git', 'rn'])
        with terminal.capture() as (out, err):
            result = cmdgit.do_rn(args)
        self.assertEqual(0, result.return_code)
        # After conflict resolution, rn shows position and commit for review
        self.assertRegex(out.getvalue(),
                         r'Rebasing 3/4: review  [0-9a-f]+\.\.\. Commit 3\n')
        self.assertFalse(err.getvalue())
        self.assertTrue(self.is_rebasing())
        self.assertEqual('Commit 3', self.get_head_subject())

        # rn to continue - Commit 4 conflicts on file4.txt
        args = cmdline.parse_args(['git', 'rn'])
        with terminal.capture() as (out, err):
            result = cmdgit.do_rn(args)
        self.assertNotEqual(0, result.return_code)
        self.assertRegex(out.getvalue(),
                         r'Rebasing 4/4: conflict in [0-9a-f]+\.\.\. Commit 4\n')
        self.assertFalse(err.getvalue())
        self.assertEqual('AA file4.txt\n',
                         command.output('git', 'status', '--porcelain'))

        # Skip Commit 4 with rs
        args = cmdline.parse_args(['git', 'rs'])
        with terminal.capture() as (out, err):
            result = cmdgit.do_rs(args)
        self.assertEqual(0, result.return_code)
        self.assertFalse(result.stdout)
        self.assertRegex(out.getvalue(),
                         r'Successfully rebased and updated refs/heads/\w+\n')
        self.assertFalse(err.getvalue())

        # Rebase complete - Commit 4 was skipped so Commit 3 is at HEAD
        self.assertFalse(self.is_rebasing())
        self.assertEqual('Commit 3', self.get_head_subject())

    def test_rn_with_unstaged_then_staged(self):
        """Test rn at edit point with unstaged changes, then staged"""
        # Start rebase with rp 1 - stops at Commit 1
        args = cmdline.parse_args(['git', 'rp', '1'])
        with terminal.capture():
            result = cmdgit.do_rp(args)
        self.assertEqual(0, result.return_code)
        self.assertTrue(self.is_rebasing())
        self.assertEqual('Commit 1', self.get_head_subject())

        # Make a change but don't stage it
        tools.write_file('file1.txt', 'modified content\n', binary=False)

        # rn should fail with unstaged changes
        args = cmdline.parse_args(['git', 'rn'])
        with terminal.capture() as (_, err):
            result = cmdgit.do_rn(args)
        self.assertEqual(1, result)
        self.assertIn('Unstaged changes', err.getvalue())

        # Stage the changes
        command.output('git', 'add', 'file1.txt')

        # rn should now work - inserts break and continues
        args = cmdline.parse_args(['git', 'rn'])
        with terminal.capture() as (out, err):
            result = cmdgit.do_rn(args)
        self.assertEqual(0, result.return_code)
        # Should show review message
        self.assertRegex(out.getvalue(),
                         r'Rebasing \d+/\d+: review  [0-9a-f]+\.\.\. Commit 1\n')
        self.assertFalse(err.getvalue())
        self.assertTrue(self.is_rebasing())
        self.assertEqual('Commit 1', self.get_head_subject())

        # Check that the todo file doesn't have multiple breaks
        rebase_dir = cmdgit.get_rebase_dir()
        todo_file = os.path.join(rebase_dir, 'git-rebase-todo')
        with open(todo_file, 'r', encoding='utf-8') as inf:
            todo_content = inf.read()
        break_count = todo_content.count('break')
        self.assertEqual(0, break_count, f'Expected 0 breaks, got {break_count}')

        # Now make another change while at the break point
        tools.write_file('file1.txt', 'another modification\n', binary=False)

        # rn should fail with unstaged changes
        args = cmdline.parse_args(['git', 'rn'])
        with terminal.capture() as (_, err):
            result = cmdgit.do_rn(args)
        self.assertEqual(1, result)
        self.assertIn('Unstaged changes', err.getvalue())

        # Stage the changes
        command.output('git', 'add', 'file1.txt')

        # rn again with staged changes - should work (amends first)
        args = cmdline.parse_args(['git', 'rn'])
        with terminal.capture() as (out, err):
            result = cmdgit.do_rn(args)
        self.assertEqual(0, result.return_code)
        self.assertRegex(out.getvalue(),
                         r'Rebasing \d+/\d+: review  [0-9a-f]+\.\.\. Commit 1\n')
        self.assertFalse(err.getvalue())

        # Check todo for multiple breaks
        with open(todo_file, 'r', encoding='utf-8') as inf:
            todo_content = inf.read()
        break_count = todo_content.count('break')
        self.assertEqual(0, break_count,
                         f'Expected 0 breaks after second rn, got {break_count}')

        # Continue the rebase
        args = cmdline.parse_args(['git', 'rc'])
        with terminal.capture():
            cmdgit.do_rc(args)
        self.assertFalse(self.is_rebasing())

    def test_rn_not_rebasing(self):
        """Test rn fails when not in a rebase"""
        args = cmdline.parse_args(['git', 'rn'])
        with terminal.capture() as (_, err):
            result = cmdgit.do_rn(args)
        self.assertEqual(1, result)
        self.assertIn('Not in the middle of a git rebase', err.getvalue())

    def test_rn_with_conflicts(self):
        """Test rn fails when there are unresolved conflicts"""
        # Start rebase with rb - stops at Commit 1
        args = cmdline.parse_args(['git', 'rb'])
        self.assertEqual(0, cmdgit.do_rb(args).return_code)

        # Create file2.txt which conflicts with Commit 2
        tools.write_file('file2.txt', 'created during rebase\n', binary=False)
        command.output('git', 'add', 'file2.txt')
        command.output('git', 'commit', '--amend', '--no-edit')

        # rc - Commit 2 conflicts on file2.txt
        args = cmdline.parse_args(['git', 'rc'])
        cmdgit.do_rc(args)
        self.assertEqual('AA file2.txt\n',
                         command.output('git', 'status', '--porcelain'))

        # rn should fail with conflicts present
        args = cmdline.parse_args(['git', 'rn'])
        with terminal.capture() as (_, err):
            result = cmdgit.do_rn(args)
        self.assertEqual(1, result)
        self.assertIn('Resolve conflicts first', err.getvalue())


class TestUmanCIVars(TestBase):
    """Test CI variable building logic"""

    def test_build_ci_vars_no_ci(self):
        """Test build_ci_vars with --null flag"""
        args = make_args(null=True)
        ci_vars = control.build_ci_vars(args)
        expected = {
            'SUITES': '0',
            'PYTEST': '0',
            'WORLD': '0',
            'SJG_LAB': '',
            'SAGE_LAB': '',
        }
        self.assertEqual(expected, ci_vars)

    def test_build_ci_vars_defaults(self):
        """Test build_ci_vars with no flags (defaults)"""
        args = make_args()
        ci_vars = control.build_ci_vars(args)
        expected = {
            'SUITES': '1',
            'PYTEST': '1',
            'WORLD': '1',
            'SJG_LAB': '',
            'SAGE_LAB': '',
        }
        self.assertEqual(expected, ci_vars)

    def test_build_ci_vars_all_flag(self):
        """Test build_ci_vars with -a flag (all stages including lab)"""
        args = make_args(all=True)
        ci_vars = control.build_ci_vars(args)
        expected = {
            'SUITES': '1',
            'PYTEST': '1',
            'WORLD': '1',
            'SJG_LAB': '1',
            'SAGE_LAB': '1',
        }
        self.assertEqual(expected, ci_vars)

    def test_build_ci_vars_specific_flags(self):
        """Test build_ci_vars with specific flags"""
        args = make_args(suites=True, sjg='1')
        ci_vars = control.build_ci_vars(args)
        expected = {
            'SUITES': '1',
            'PYTEST': '0',
            'WORLD': '0',
            'SJG_LAB': '1',
            'SAGE_LAB': '',
        }
        self.assertEqual(expected, ci_vars)

    def test_build_ci_vars_sjg_lab_value(self):
        """Test build_ci_vars with sjg value"""
        args = make_args(sjg='rpi4')
        ci_vars = control.build_ci_vars(args)
        expected = {
            'SUITES': '0',
            'PYTEST': '0',
            'WORLD': '0',
            'SJG_LAB': 'rpi4',
            'SAGE_LAB': '',
        }
        self.assertEqual(expected, ci_vars)

    def test_build_ci_vars_sage_flag(self):
        """Test build_ci_vars with --sage flag"""
        args = make_args(sage='1')
        ci_vars = control.build_ci_vars(args)
        expected = {
            'SUITES': '0',
            'PYTEST': '0',
            'WORLD': '0',
            'SJG_LAB': '',
            'SAGE_LAB': '1',
        }
        self.assertEqual(expected, ci_vars)

    def test_build_ci_vars_sage_name(self):
        """Test build_ci_vars with --sage <job-name>"""
        args = make_args(sage='Raspberry Pi 4')
        ci_vars = control.build_ci_vars(args)
        expected = {
            'SUITES': '0',
            'PYTEST': '0',
            'WORLD': '0',
            'SJG_LAB': '',
            'SAGE_LAB': 'Raspberry Pi 4',
        }
        self.assertEqual(expected, ci_vars)

    def test_build_ci_vars_test_spec(self):
        """Test build_ci_vars with --test-spec flag"""
        args = make_args(test_spec='not sleep')
        ci_vars = control.build_ci_vars(args)
        expected = {
            'SUITES': '0',
            'PYTEST': '0',
            'WORLD': '0',
            'SJG_LAB': '',
            'SAGE_LAB': '',
            'TEST_SPEC': 'not sleep',
        }
        self.assertEqual(expected, ci_vars)

    def test_build_ci_vars_specific_pytest_target(self):
        """Test build_ci_vars with specific pytest target (like coreboot)"""
        args = make_args(pytest='coreboot')
        ci_vars = control.build_ci_vars(args)
        expected = {
            'SUITES': '0',
            'PYTEST': 'coreboot',
            'WORLD': '0',
            'SJG_LAB': '',
            'SAGE_LAB': '',
        }
        self.assertEqual(expected, ci_vars)

    def test_build_ci_vars_pytest_with_sjg_lab(self):
        """Test build_ci_vars combining pytest target with sjg lab"""
        args = make_args(pytest='coreboot', sjg='bbb')
        ci_vars = control.build_ci_vars(args)
        expected = {
            'SUITES': '0',
            'PYTEST': 'coreboot',
            'WORLD': '0',
            'SJG_LAB': 'bbb',
            'SAGE_LAB': '',
        }
        self.assertEqual(expected, ci_vars)

    def test_build_ci_vars_pytest_and_test_spec_separate(self):
        """Test that -p and -t flags work independently"""
        # Board name with -p should only set PYTEST, not TEST_SPEC
        args = make_args(pytest='sandbox')
        ci_vars = control.build_ci_vars(args)
        expected = {
            'SUITES': '0',
            'PYTEST': 'sandbox',
            'WORLD': '0',
            'SJG_LAB': '',
            'SAGE_LAB': '',
        }
        self.assertEqual(expected, ci_vars)
        self.assertNotIn('TEST_SPEC', ci_vars)

        # Using both -p and -t flags together
        args = make_args(pytest='coreboot', test_spec='test_ofplatdata')
        ci_vars = control.build_ci_vars(args)
        expected = {
            'SUITES': '0',
            'PYTEST': 'coreboot',
            'WORLD': '0',
            'SJG_LAB': '',
            'SAGE_LAB': '',
            'TEST_SPEC': 'test_ofplatdata',
        }
        self.assertEqual(expected, ci_vars)

    def test_build_ci_vars_job_name_targeting(self):
        """Test build_ci_vars with job name targeting"""
        args = make_args(pytest='sandbox with clang test.py')
        ci_vars = control.build_ci_vars(args)
        expected = {
            'SUITES': '0',
            'PYTEST': 'sandbox with clang test.py',
            'WORLD': '0',
            'SJG_LAB': '',
            'SAGE_LAB': '',
        }
        self.assertEqual(expected, ci_vars)

        # Should not set TEST_SPEC for job names
        self.assertNotIn('TEST_SPEC', ci_vars)

    def test_build_ci_vars_all_flags(self):
        """Test build_ci_vars with all flags enabled"""
        args = make_args(suites=True, pytest='1', world=True, sjg='1',
                         sage='1')
        ci_vars = control.build_ci_vars(args)
        expected = {
            'SUITES': '1',
            'PYTEST': '1',
            'WORLD': '1',
            'SJG_LAB': '1',
            'SAGE_LAB': '1',
        }
        self.assertEqual(expected, ci_vars)

    def test_build_commit_tags_no_skip(self):
        """Test build_commit_tags with no skip flags (all enabled)"""
        args = make_args(suites=True, pytest='1', world=True, sjg='1',
                         sage='1')
        ci_vars = control.build_ci_vars(args)
        tags = control.build_commit_tags(args, ci_vars)
        self.assertEqual('', tags)

    def test_build_commit_tags_skip_all(self):
        """Test build_commit_tags with --null flag (skip all)"""
        args = make_args(null=True)
        ci_vars = control.build_ci_vars(args)
        tags = control.build_commit_tags(args, ci_vars)
        self.assertEqual(
            '[skip-suites] [skip-pytest] [skip-world] [skip-sjg] '
            '[skip-sage]', tags)

    def test_build_commit_tags_skip_specific(self):
        """Test build_commit_tags with specific stages enabled"""
        args = make_args(suites=True)  # Only suites enabled, others skip
        ci_vars = control.build_ci_vars(args)
        tags = control.build_commit_tags(args, ci_vars)
        self.assertEqual(
            '[skip-pytest] [skip-world] [skip-sjg] [skip-sage]', tags)

    def test_build_commit_tags_skip_world_only(self):
        """Test build_commit_tags with world skipped"""
        # suites and pytest enabled, world skipped
        args = make_args(suites=True, pytest='1')
        ci_vars = control.build_ci_vars(args)
        tags = control.build_commit_tags(args, ci_vars)
        self.assertEqual('[skip-world] [skip-sjg] [skip-sage]', tags)

    def test_commit_message_tag_integration(self):
        """Test that tags are correctly integrated into commit message"""
        tags = ('[skip-suites] [skip-pytest] [skip-world] [skip-sjg] '
                '[skip-sage]')

        # Empty description with tags
        self.assertEqual(tags, control.build_desc('', tags))

        # Existing description with tags
        exp = ('This is a test commit\n\nSome details about the change\n\n'
               + tags)
        self.assertEqual(
            exp, control.build_desc(
                'This is a test commit\n\nSome details about the change', tags))

        # No tags returns original description unchanged
        self.assertEqual(
            'Test description', control.build_desc('Test description', ''))

        # Empty description with no tags
        self.assertEqual('', control.build_desc('', ''))

class TestUmanCI(TestBase):
    """Test the CI command functionality"""

    def setUp(self):
        """Set up test environment"""
        super().setUp()
        self.orig_cwd = os.getcwd()
        tout.init(tout.NOTICE)

    def tearDown(self):
        """Clean up test environment"""
        os.chdir(self.orig_cwd)
        super().tearDown()

    def _create_git_repo(self):
        """Create a temporary git repository for testing"""
        os.chdir(self.test_dir)

        # Initialise git repo
        command.run('git', 'init', **CAPTURE)
        command.run('git', 'config', 'user.name', 'Test User', **CAPTURE)
        command.run('git', 'config', 'user.email', 'test@example.com',
                    **CAPTURE)

        # Create initial commit
        tools.write_file('test.txt', b'test content')
        command.run('git', 'add', '.', **CAPTURE)
        command.run('git', 'commit', '-m', 'Initial commit', **CAPTURE)

    def test_ci_not_in_git_repo(self):
        """Test CI command fails when not in git repository"""
        # Use command.py's TEST_RESULT to simulate git failure
        def raise_git_error(**_kwargs):
            result = command.CommandResult(b'', b'not a git repo', b'', 1)
            raise command.CommandExc('git failed', result)

        command.TEST_RESULT = raise_git_error
        args = make_args()
        with self.assertRaises(command.CommandExc):
            with terminal.capture():
                control.do_ci(args)

    def test_ci_dry_run(self):
        """Test CI command shows git push in dry-run mode"""
        self._create_git_repo()

        args = make_args(dry_run=True)
        with terminal.capture() as (out, _):
            res = control.do_ci(args)
        self.assertEqual(0, res)
        self.assertEqual(
            'git push -o ci.variable=SUITES=1 -o ci.variable=PYTEST=1 '
            '-o ci.variable=WORLD=1 -o ci.variable=SJG_LAB= '
            '-o ci.variable=SAGE_LAB= ci master\n',
            out.getvalue())

    def test_ci_specific_variables(self):
        """Test CI command with specific variables"""
        self._create_git_repo()

        args = make_args(dry_run=True, suites=True, pytest='1', sjg='rpi4')
        with terminal.capture() as (out, _):
            res = control.do_ci(args)
        self.assertEqual(0, res)
        self.assertEqual(
            'git push -o ci.variable=SUITES=1 -o ci.variable=PYTEST=1 '
            '-o ci.variable=WORLD=0 -o ci.variable=SJG_LAB=rpi4 '
            '-o ci.variable=SAGE_LAB= ci master\n',
            out.getvalue())

    def test_ci_no_ci_flag(self):
        """Test CI command with --null flag sets all vars to 0"""
        self._create_git_repo()

        args = make_args(dry_run=True, null=True)
        with terminal.capture() as (out, _):
            res = control.do_ci(args)
        self.assertEqual(0, res)
        self.assertEqual(
            'git push -o ci.variable=SUITES=0 -o ci.variable=PYTEST=0 '
            '-o ci.variable=WORLD=0 -o ci.variable=SJG_LAB= '
            '-o ci.variable=SAGE_LAB= ci master\n',
            out.getvalue())

    def test_ci_custom_remote(self):
        """Test CI command with -r uses the specified remote"""
        self._create_git_repo()

        args = make_args(dry_run=True, remote='upstream')
        with terminal.capture() as (out, _):
            res = control.do_ci(args)
        self.assertEqual(0, res)
        output = out.getvalue()
        self.assertIn('upstream master', output)
        self.assertNotIn(' ci ', output)

    def test_ci_remote_from_settings(self):
        """Test CI command uses ci_remote from settings"""
        self._create_git_repo()

        args = make_args(dry_run=True)
        with mock.patch.object(settings, 'get',
                               side_effect=lambda k, **kw:
                               'origin' if k == 'ci_remote' else
                               kw.get('fallback')):
            with terminal.capture() as (out, _):
                res = control.do_ci(args)
        self.assertEqual(0, res)
        output = out.getvalue()
        self.assertIn('origin master', output)

    def test_ci_remote_auto_detect(self):
        """Test CI command auto-detects remote from upstream tracking ref"""
        self._create_git_repo()

        args = make_args(dry_run=True)
        with mock.patch.object(control, 'detect_upstream_remote',
                               return_value='us'):
            with mock.patch.object(control, 'get_remote_map',
                                   return_value={}):
                with terminal.capture() as (out, _):
                    res = control.do_ci(args)
        self.assertEqual(0, res)
        output = out.getvalue()
        self.assertIn('us master', output)

    def test_detect_upstream_remote(self):
        """Test detect_upstream_remote parses tracking ref"""
        self._create_git_repo()

        # No upstream set - should return None
        result = control.detect_upstream_remote()
        self.assertIsNone(result)

    def test_detect_upstream_remote_from_history(self):
        """Test detect_upstream_remote finds remote from commit history"""
        log_output = ('el/loada-us, dm/loada-us\n'
                      'us/next, us/WIP/23Mar2026-next\n')
        result = command.CommandResult(return_code=0, stdout=log_output)

        with mock.patch.object(command, 'output_one_line',
                               side_effect=command.CommandExc('no upstream',
                                                              None)):
            with mock.patch.object(command, 'run_one',
                                   return_value=result):
                remote = control.detect_upstream_remote()
        self.assertEqual('us', remote)

    def test_ci_remote_map(self):
        """Test CI remote mapping from upstream to push remote"""
        self._create_git_repo()

        args = make_args(dry_run=True)
        with mock.patch.object(control, 'detect_upstream_remote',
                               return_value='us'):
            with mock.patch.object(settings, 'get',
                                   side_effect=lambda k, **kw:
                                   'us:dm' if k == 'ci_remote_map' else
                                   kw.get('fallback')):
                with terminal.capture() as (out, _):
                    res = control.do_ci(args)
        self.assertEqual(0, res)
        output = out.getvalue()
        self.assertIn('dm master', output)

    def test_ci_remote_map_no_match(self):
        """Test CI remote mapping with no matching entry"""
        self._create_git_repo()

        args = make_args(dry_run=True)
        with mock.patch.object(control, 'detect_upstream_remote',
                               return_value='us'):
            with mock.patch.object(settings, 'get',
                                   side_effect=lambda k, **kw:
                                   'ub:ci' if k == 'ci_remote_map' else
                                   kw.get('fallback')):
                with terminal.capture() as (out, _):
                    res = control.do_ci(args)
        self.assertEqual(0, res)
        output = out.getvalue()
        self.assertIn('us master', output)

    def test_exec_cmd_dry_run(self):
        """Test exec_cmd in dry-run mode shows command"""
        with terminal.capture() as (out, err):
            res = control.exec_cmd(['echo', 'test'], dry_run=True)
        self.assertIsNone(res)
        self.assertEqual('echo test\n', out.getvalue())
        self.assertFalse(err.getvalue())

    def test_exec_cmd_normal(self):
        """Test exec_cmd in normal mode"""
        with terminal.capture() as (out, err):
            res = control.exec_cmd(['true'], dry_run=False)
        self.assertIsNotNone(res)
        self.assertEqual(0, res.return_code)
        self.assertFalse(out.getvalue())
        self.assertFalse(err.getvalue())

    def test_git_push_destination_parsing(self):
        """Test git_push_branch destination parsing"""
        cap = []

        def mock_exec(cmd, dry_run=False, env=None, capture=True):
            del dry_run, env, capture
            cap.append(list(cmd))
            return command.CommandResult(stdout='', return_code=0)

        with mock.patch.object(control, 'exec_cmd', mock_exec):
          with mock.patch.object(control, 'detect_upstream_remote',
                                 return_value=None):
            # Test default destination (None means use current branch name)
            args = make_args(dry_run=False, dest=None)
            with terminal.capture():
                control.git_push_branch('test-branch', args)
            self.assertEqual(['git', 'push', 'ci', 'test-branch'], cap[-1])

            # Test custom destination branch
            args = make_args(dry_run=False, dest='my-feature')
            with terminal.capture():
                control.git_push_branch('test-branch', args)
            self.assertEqual(['git', 'push', 'ci', 'test-branch:my-feature'],
                             cap[-1])

            # Test with CI variables
            args = make_args(dry_run=False, dest='feature-test')
            ci_vars = {'PYTEST': '1', 'SUITES': '0'}
            with terminal.capture():
                control.git_push_branch('test-branch', args, ci_vars=ci_vars)
            self.assertEqual(
                ['git', 'push', '-o', 'ci.variable=PYTEST=1', '-o',
                 'ci.variable=SUITES=0', 'ci', 'test-branch:feature-test'],
                cap[-1])

            # Test with force flag
            args = make_args(dry_run=False, dest='force-test', force=True)
            with terminal.capture():
                control.git_push_branch('test-branch', args)
            self.assertEqual(['git', 'push', '--force', 'ci',
                              'test-branch:force-test'], cap[-1])

            # Test with upstream flag
            args = make_args(dry_run=False, dest='upstream-test')
            with terminal.capture():
                control.git_push_branch('test-branch', args, upstream=True)
            self.assertEqual(['git', 'push', '-u', 'ci',
                              'test-branch:upstream-test'], cap[-1])


class TestUmanControl(TestBase):  # pylint: disable=too-many-public-methods
    """Test the control module functionality"""

    def setUp(self):
        """Set up test environment with fake U-Boot tree"""
        super().setUp()
        self.empty_dir = tempfile.mkdtemp()  # Empty dir (not a U-Boot tree)
        self.orig_cwd = os.getcwd()
        self.orig_usrc = os.environ.get('USRC')
        if 'USRC' in os.environ:
            del os.environ['USRC']
        os.chdir(self.test_dir)
        os.makedirs('test/py')
        tools.write_file('test/py/test.py', b'# test')

    def tearDown(self):
        """Clean up test environment"""
        os.chdir(self.orig_cwd)
        if self.orig_usrc is not None:
            os.environ['USRC'] = self.orig_usrc
        elif 'USRC' in os.environ:
            del os.environ['USRC']
        shutil.rmtree(self.empty_dir)
        super().tearDown()

    def test_run_command_ci(self):
        """Test run_command dispatches to CI correctly"""
        args = make_args(cmd='ci', dry_run=True)

        # Mock the CI function to avoid git operations
        orig_do_ci = control.do_ci
        control.do_ci = lambda args: 0

        try:
            with terminal.capture() as (out, err):
                res = control.run_command(args)
            self.assertEqual(0, res)
            self.assertFalse(out.getvalue())
            self.assertFalse(err.getvalue())
        finally:
            control.do_ci = orig_do_ci

    def test_run_command_unknown(self):
        """Test run_command with unknown command"""
        args = make_args(cmd='unknown')
        with terminal.capture() as (out, err):
            res = control.run_command(args)
        self.assertEqual(1, res)
        self.assertFalse(out.getvalue())
        self.assertEqual('Unknown command: unknown\n', err.getvalue())

    def test_invalid_pytest_value(self):
        """Test validation of invalid pytest values"""
        args = make_args(cmd='ci', pytest='invalid_board')
        with terminal.capture() as (out, err):
            res = control.run_command(args)
        self.assertEqual(1, res)
        self.assertIn('ci -p help', out.getvalue())
        self.assertEqual('Invalid pytest value: invalid_board\n',
                         err.getvalue())

    def test_invalid_sjg_value(self):
        """Test validation of invalid SJG_LAB values"""
        args = make_args(cmd='ci', sjg='invalid_lab')
        with terminal.capture() as (out, err):
            res = control.run_command(args)
        self.assertEqual(1, res)
        self.assertIn('ci -l help', out.getvalue())
        self.assertEqual('Invalid SJG_LAB value: invalid_lab\n', err.getvalue())

    def test_pytest_command(self):
        """Test pytest command execution"""
        cap = []

        def mock_subprocess_run(cmd, **_kwargs):
            cap.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        # Test basic pytest command
        args = make_args(cmd='pytest', board='sandbox')
        with mock.patch('subprocess.run', mock_subprocess_run):
            with terminal.capture():
                res = control.run_command(args)
        self.assertEqual(0, res)

        # Verify command structure
        cmd = cap[-1]
        self.assertTrue(cmd[0].endswith('/test/py/test.py'))
        self.assertIn('-B', cmd)
        self.assertIn('sandbox', cmd)
        # Default should not add --no-timeout
        self.assertNotIn('--no-timeout', cmd)

        # Test pytest with --no-timeout flag
        args = make_args(cmd='pytest', board='malta', test_spec=['test_dm'],
                         no_timeout=True)
        with mock.patch('subprocess.run', mock_subprocess_run):
            with terminal.capture():
                res = control.run_command(args)
        self.assertEqual(0, res)

        cmd = cap[-1]
        self.assertIn('malta', cmd)
        self.assertIn('-k', cmd)
        self.assertIn('test_dm', cmd)
        self.assertIn('--no-timeout', cmd)

    def test_pytest_extra_args(self):
        """Test pytest command with extra arguments after --"""
        cap = []

        def mock_subprocess_run(cmd, **_kwargs):
            cap.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        # Test extra args parsing through cmdline
        args = cmdline.parse_args(['py', '-B', 'sandbox', 'TestFsBasic',
                                   '--', '--fs-type', 'ext4'])
        self.assertEqual(['TestFsBasic'], args.test_spec)
        self.assertEqual(['--fs-type', 'ext4'], args.extra_args)

        # Test command building includes extra args
        with mock.patch('subprocess.run', mock_subprocess_run):
            with terminal.capture():
                res = control.run_command(args)
        self.assertEqual(0, res)

        cmd = cap[-1]
        self.assertIn('--fs-type', cmd)
        self.assertIn('ext4', cmd)

    def test_valid_pytest_value(self):
        """Test validation of valid pytest values"""
        args = make_args(cmd='ci', pytest='sandbox', dry_run=True)

        # Mock the CI function to avoid git operations
        orig_do_ci = control.do_ci
        control.do_ci = lambda args: 0

        try:
            with terminal.capture() as (out, err):
                res = control.run_command(args)
            self.assertEqual(0, res)
            self.assertFalse(out.getvalue())
            self.assertFalse(err.getvalue())
        finally:
            control.do_ci = orig_do_ci

    def test_valid_sjg_value(self):
        """Test validation of valid SJG_LAB values"""
        args = make_args(cmd='ci', sjg='rpi4', dry_run=True)

        # Mock the CI function to avoid git operations
        orig_do_ci = control.do_ci
        control.do_ci = lambda args: 0

        try:
            with terminal.capture() as (out, err):
                res = control.run_command(args)
            self.assertEqual(0, res)
            self.assertFalse(out.getvalue())
            self.assertFalse(err.getvalue())
        finally:
            control.do_ci = orig_do_ci

    def test_pytest_board_required(self):
        """Test that pytest requires a board"""
        orig_b = os.environ.pop('b', None)
        try:
            args = make_args(cmd='pytest', board=None)
            with terminal.capture() as (_, err):
                res = control.run_command(args)
            self.assertEqual(1, res)
            self.assertIn('Board is required', err.getvalue())
        finally:
            if orig_b is not None:
                os.environ['b'] = orig_b

    def test_pytest_board_from_env(self):
        """Test that pytest uses $b environment variable"""
        cap = []

        def mock_subprocess_run(cmd, **_kwargs):
            cap.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        orig_env = os.environ.get('b')

        try:
            os.environ['b'] = 'sandbox'
            args = make_args(cmd='pytest', board=None)
            with mock.patch('subprocess.run', mock_subprocess_run):
                with terminal.capture():
                    res = control.run_command(args)
            self.assertEqual(0, res)
            self.assertIn('sandbox', cap[-1])
        finally:
            if orig_env is not None:
                os.environ['b'] = orig_env
            elif 'b' in os.environ:
                del os.environ['b']

    def test_pytest_quiet_mode(self):
        """Test that quiet mode adds correct flags"""
        cap = []

        def mock_subprocess_run(cmd, **_kwargs):
            cap.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        args = make_args(cmd='pytest', board='sandbox', quiet=True)
        with mock.patch('subprocess.run', mock_subprocess_run):
            with terminal.capture():
                res = control.run_command(args)
        self.assertEqual(0, res)

        cmd = cap[-1]
        self.assertIn('--no-header', cmd)
        self.assertIn('--quiet-hooks', cmd)

    def test_pytest_no_full_unsupported(self):
        """Test do_pytest omits --no-full when tree lacks support"""
        calls = []

        def mock_subprocess_run(cmd, **_kwargs):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stderr=b'')

        args = make_args(cmd='pytest', board='sandbox')
        with mock.patch('subprocess.run', mock_subprocess_run):
            with mock.patch('uman_pkg.cmdpy.has_no_full', return_value=False):
                with terminal.capture():
                    res = control.run_command(args)
        self.assertEqual(0, res)
        self.assertEqual(1, len(calls))
        self.assertNotIn('--no-full', calls[0])

    def test_pytest_lto_flag(self):
        """Test -L/--lto flag for pytest"""
        args = cmdline.parse_args(['pytest', '-B', 'sandbox', '-L'])
        self.assertTrue(args.lto)

        args = cmdline.parse_args(['pytest', '-B', 'sandbox', '--lto'])
        self.assertTrue(args.lto)

    def test_pytest_lto_with_build(self):
        """Test that -L with -b passes lto to build_board()"""
        cap = []

        def mock_exec_cmd(cmd, *_args, **_kwargs):
            cap.append(cmd)
            return command.CommandResult(return_code=0)

        def mock_subprocess_run(cmd, **_kwargs):
            cap.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        args = make_args(cmd='pytest', board='sandbox', build=True, lto=True)
        with mock.patch.object(build, 'setup_uboot_dir', return_value=True):
            with mock.patch.object(build, 'exec_cmd', mock_exec_cmd):
                with mock.patch('subprocess.run', mock_subprocess_run):
                    with terminal.capture():
                        control.run_command(args)

        # First command should be buildman without -L (LTO enabled)
        self.assertNotIn('-L', cap[0])

    def test_pytest_find_flag(self):
        """Test --find flag for pytest"""
        args = cmdline.parse_args(['pytest', '-B', 'sandbox', '--find', 'video'])
        self.assertEqual('video', args.find)

    def test_pytest_build_dir_flag(self):
        """Test -o/--output-dir flag for pytest"""
        args = cmdline.parse_args(['pytest', '-B', 'sandbox', '-o', '/custom/dir'])
        self.assertEqual('/custom/dir', args.output_dir)

        args = cmdline.parse_args(['pytest', '-B', 'sandbox', '--output-dir',
                                   '/other/dir'])
        self.assertEqual('/other/dir', args.output_dir)

    def test_pytest_find_tests(self):
        """Test finding tests with -F option"""
        def mock_collect(**_kwargs):
            return command.CommandResult(
                stdout='test_ut.py::TestUt::test_dm_video\n'
                       'test_ut.py::TestUt::test_dm_gpio\n'
                       'test_ut.py::TestUt::test_fs_fat\n',
                return_code=0)

        command.TEST_RESULT = mock_collect

        args = make_args(cmd='pytest', board='sandbox', find='video')
        with terminal.capture() as (out, _):
            res = control.run_command(args)
        self.assertEqual(0, res)
        self.assertIn('test_dm_video', out.getvalue())
        self.assertNotIn('test_dm_gpio', out.getvalue())

    def test_pytest_find_no_match(self):
        """Test -F with no matching tests"""
        def mock_collect(**_kwargs):
            return command.CommandResult(
                stdout='test_ut.py::TestUt::test_dm_gpio\n',
                return_code=0)

        command.TEST_RESULT = mock_collect

        args = make_args(cmd='pytest', board='sandbox', find='nonexistent')
        with terminal.capture() as (_, err):
            res = control.run_command(args)
        self.assertEqual(1, res)
        self.assertIn("No tests matching 'nonexistent'", err.getvalue())

    def test_pytest_list_boards(self):
        """Test listing QEMU boards"""
        def mock_buildman(**_kwargs):
            return command.CommandResult(
                stdout='qemu : 2 boards\n   qemu-arm qemu-riscv64\n',
                return_code=0)

        command.TEST_RESULT = mock_buildman

        args = make_args(cmd='pytest', list_boards=True)
        with terminal.capture() as (out, _):
            res = control.run_command(args)
        self.assertEqual(0, res)
        self.assertIn('qemu-arm', out.getvalue())
        self.assertIn('qemu-riscv64', out.getvalue())

    def test_get_board_gitlab_vars(self):
        """Test parsing gitlab CI variables for a board"""
        # Create a fake .gitlab-ci.yml
        gitlab_content = '''
test_m5208:
  variables:
    TEST_PY_BD: "M5208EVBE"
    TEST_PY_ID: "--id qemu"
    TEST_PY_TEST_SPEC: "not sleep and not efi"
    OVERRIDE: "-a CONFIG_M68K_QEMU=y -a ~CONFIG_MCFTMR"
  <<: *buildman_and_testpy_dfn

test_sandbox:
  variables:
    TEST_PY_BD: "sandbox"
    TEST_PY_ID: "--id na"
'''
        gitlab_file = os.path.join(self.test_dir, '.gitlab-ci.yml')
        tools.write_file(gitlab_file, gitlab_content.encode())

        # Test get_board_test_id
        self.assertEqual('qemu',
                         cmdpy.get_board_test_id('M5208EVBE'))
        self.assertEqual('na',
                         cmdpy.get_board_test_id('sandbox'))
        self.assertEqual('na', cmdpy.get_board_test_id('unknown_board'))

        # Test get_board_test_spec
        self.assertEqual('not sleep and not efi',
                         cmdpy.get_board_test_spec('M5208EVBE'))
        self.assertIsNone(cmdpy.get_board_test_spec('sandbox'))
        self.assertIsNone(cmdpy.get_board_test_spec('unknown_board'))

        # Test get_board_override
        self.assertEqual(['CONFIG_M68K_QEMU=y', '~CONFIG_MCFTMR'],
                         cmdpy.get_board_override('M5208EVBE'))
        self.assertEqual([], cmdpy.get_board_override('sandbox'))
        self.assertEqual([], cmdpy.get_board_override('unknown_board'))

    def test_get_qemu_binary(self):
        """Test getting QEMU binary from test hooks config"""
        # Create test hooks directory structure
        hooks_dir = os.path.join(self.test_dir, 'test', 'hooks', 'bin',
                                 'travis-ci')
        os.makedirs(hooks_dir, exist_ok=True)

        # Create config file for M5208EVBE_qemu
        config_content = '''console_impl=qemu
qemu_binary="qemu-system-m68k"
qemu_machine="mcf5208evb"
'''
        config_path = os.path.join(hooks_dir, 'conf.M5208EVBE_qemu')
        tools.write_file(config_path, config_content.encode())

        # Test finding the binary
        binary = cmdpy.get_qemu_binary('M5208EVBE', 'qemu')
        self.assertEqual('qemu-system-m68k', binary)

        # Test not found
        binary = cmdpy.get_qemu_binary('unknown_board', 'na')
        self.assertIsNone(binary)

    def test_check_qemu_binary(self):
        """Test checking if QEMU binary is available"""
        # Create test hooks directory structure
        hooks_dir = os.path.join(self.test_dir, 'test', 'hooks', 'bin',
                                 'travis-ci')
        os.makedirs(hooks_dir, exist_ok=True)

        # Create config file with a binary that should exist (python3)
        config_content = '''console_impl=qemu
qemu_binary="python3"
'''
        config_path = os.path.join(hooks_dir, 'conf.testboard_qemu')
        tools.write_file(config_path, config_content.encode())

        # Test binary that exists
        binary, available = cmdpy.check_qemu_binary('testboard', 'qemu')
        self.assertEqual('python3', binary)
        self.assertTrue(available)

        # Create config file with non-existent binary
        config_content = '''console_impl=qemu
qemu_binary="nonexistent-qemu-binary-xyz"
'''
        config_path = os.path.join(hooks_dir, 'conf.missing_qemu')
        tools.write_file(config_path, config_content.encode())

        binary, available = cmdpy.check_qemu_binary('missing', 'qemu')
        self.assertEqual('nonexistent-qemu-binary-xyz', binary)
        self.assertFalse(available)

        # Test board with no config (should return None, True)
        binary, available = cmdpy.check_qemu_binary('noconfig', 'na')
        self.assertIsNone(binary)
        self.assertTrue(available)

    def test_pytest_missing_qemu_binary(self):
        """Test pytest fails early when QEMU binary is not found"""
        # Create .gitlab-ci.yml with TEST_PY_ID
        gitlab_content = '''
test_missing:
  variables:
    TEST_PY_BD: "missing_qemu_board"
    TEST_PY_ID: "--id qemu"
'''
        gitlab_file = os.path.join(self.test_dir, '.gitlab-ci.yml')
        tools.write_file(gitlab_file, gitlab_content.encode())

        # Create test hooks config with non-existent binary
        hooks_dir = os.path.join(self.test_dir, 'test', 'hooks', 'bin',
                                 'travis-ci')
        os.makedirs(hooks_dir, exist_ok=True)
        config_content = '''console_impl=qemu
qemu_binary="nonexistent-qemu-xyz"
'''
        config_path = os.path.join(hooks_dir, 'conf.missing_qemu_board_qemu')
        tools.write_file(config_path, config_content.encode())

        args = make_args(cmd='pytest', board='missing_qemu_board')
        with terminal.capture() as (_, err):
            res = control.run_command(args)

        self.assertEqual(1, res)
        self.assertIn('QEMU binary not found', err.getvalue())
        self.assertIn('nonexistent-qemu-xyz', err.getvalue())

    def test_pytest_gdb_dry_run(self):
        """Test pytest -G with dry-run prints gdb command"""
        args = make_args(cmd='pytest', board='sandbox',
                         gdb_phase='u-boot',
                         gdb=True, dry_run=True)

        # Mock subprocess.run to capture what would be run
        with terminal.capture() as (out, _):
            res = control.run_command(args)

        self.assertEqual(0, res)
        output = out.getvalue()
        self.assertIn('gdb-multiarch', output)
        self.assertIn('/tmp/b/sandbox/u-boot', output)
        self.assertIn('target remote localhost:1234', output)
        self.assertIn('handle SIGUSR2 nostop noprint pass', output)

    def test_pytest_gdb_bt_dry_run(self):
        """Test pytest --bt with dry-run adds bt and quit to gdb command"""
        args = make_args(cmd='pytest', board='sandbox',
                         gdb_phase='u-boot',
                         gdb=True, bt=True, dry_run=True)

        with terminal.capture() as (out, _):
            res = control.run_command(args)

        self.assertEqual(0, res)
        output = out.getvalue()
        self.assertIn('gdb-multiarch', output)
        self.assertIn('-ex bt -ex quit', output)

    def test_pytest_gdb_cmd_dry_run(self):
        """Test pytest --gdb-cmd with dry-run adds extra gdb commands"""
        args = make_args(cmd='pytest', board='sandbox',
                         gdb_phase='u-boot',
                         gdb=True, gdb_cmd=['info reg', 'bt'],
                         dry_run=True)

        with terminal.capture() as (out, _):
            res = control.run_command(args)

        self.assertEqual(0, res)
        output = out.getvalue()
        self.assertIn('gdb-multiarch', output)
        self.assertIn('-ex info reg -ex bt', output)

    def test_pytest_bt_implies_gdb(self):
        """Test pytest --bt implies -G"""
        args = make_args(cmd='pytest', board='sandbox',
                         bt=True, dry_run=True)

        with terminal.capture() as (out, _):
            res = control.run_command(args)

        self.assertEqual(0, res)
        output = out.getvalue()
        self.assertIn('gdb-multiarch', output)

    def test_get_uboot_dir_current(self):
        """Test get_uboot_dir finds U-Boot in current directory"""
        # setUp already created fake U-Boot tree in self.test_dir
        self.assertEqual(self.test_dir, util.get_uboot_dir())

    def test_get_uboot_dir_usrc_env(self):
        """Test get_uboot_dir uses $USRC when not in U-Boot tree"""
        os.chdir(self.empty_dir)
        os.environ['USRC'] = self.test_dir
        self.assertEqual(self.test_dir, util.get_uboot_dir())

    def test_get_uboot_dir_not_found(self):
        """Test get_uboot_dir returns None when no U-Boot tree found"""
        os.chdir(self.empty_dir)
        self.assertIsNone(util.get_uboot_dir())

    def test_setup_uboot_dir_current(self):
        """Test setup_uboot_dir when already in U-Boot tree"""
        # setUp already created fake U-Boot tree in self.test_dir
        result = util.setup_uboot_dir()
        self.assertEqual(self.test_dir, result)
        self.assertEqual(self.test_dir, os.getcwd())

    def test_setup_uboot_dir_changes_dir(self):
        """Test setup_uboot_dir changes to $USRC directory"""
        os.chdir(self.empty_dir)
        os.environ['USRC'] = self.test_dir

        with terminal.capture():
            result = util.setup_uboot_dir()

        self.assertEqual(self.test_dir, result)
        self.assertEqual(self.test_dir, os.getcwd())

    def test_setup_uboot_dir_not_found(self):
        """Test setup_uboot_dir returns None when no U-Boot tree"""
        os.chdir(self.empty_dir)
        with terminal.capture():
            result = util.setup_uboot_dir()

        self.assertIsNone(result)

    def test_pytest_not_in_uboot_tree(self):
        """Test pytest fails when not in U-Boot tree and no $USRC"""
        os.chdir(self.empty_dir)
        args = make_args(cmd='pytest', board='sandbox')
        with terminal.capture() as (_, err):
            res = control.run_command(args)
        self.assertEqual(1, res)
        self.assertIn('Not in a U-Boot tree', err.getvalue())

    def test_parse_hook_config(self):
        """Test parsing hook config files"""
        config_content = b'''# Comment line
console_impl=qemu
qemu_machine="virt"
qemu_binary=qemu-system-riscv64
qemu_extra_args="-m 1G -nographic"
qemu_kernel_args="-bios ${OPENSBI} -kernel ${U_BOOT_BUILD_DIR}/u-boot.bin"
'''
        config_path = os.path.join(self.test_dir, 'conf.test')
        tools.write_file(config_path, config_content)

        config = cmdpy.parse_hook_config(config_path)

        self.assertEqual('qemu', config['console_impl'])
        self.assertEqual('virt', config['qemu_machine'])
        self.assertEqual('qemu-system-riscv64', config['qemu_binary'])
        self.assertEqual('-m 1G -nographic', config['qemu_extra_args'])
        self.assertIn('${OPENSBI}', config['qemu_kernel_args'])

    def test_parse_hook_config_nonexistent(self):
        """Test parsing non-existent config file returns empty dict"""
        config = cmdpy.parse_hook_config('/nonexistent/path')
        self.assertEqual({}, config)

    def test_expand_vars(self):
        """Test shell variable expansion"""
        env = {
            'OPENSBI': '/path/to/opensbi.bin',
            'BUILD_DIR': '/tmp/build',
        }

        # Test simple expansion
        result = cmdpy.expand_vars('${OPENSBI}', env)
        self.assertEqual('/path/to/opensbi.bin', result)

        # Test multiple variables
        result = cmdpy.expand_vars('-bios ${OPENSBI} -dir ${BUILD_DIR}',
                                         env)
        self.assertEqual('-bios /path/to/opensbi.bin -dir /tmp/build', result)

        # Test unknown variable remains unchanged
        result = cmdpy.expand_vars('${UNKNOWN}', env)
        self.assertEqual('${UNKNOWN}', result)

    @mock.patch('uman_pkg.cmdpy.settings')
    @mock.patch('uman_pkg.cmdpy.socket')
    def test_get_qemu_command(self, mock_socket, mock_settings):
        """Test building QEMU command from config"""
        # Set up mock hostname
        mock_socket.gethostname.return_value = 'testhost'

        # Create config directory structure
        hooks_dir = os.path.join(self.test_dir, 'hooks')
        bin_dir = os.path.join(hooks_dir, 'bin')
        host_dir = os.path.join(bin_dir, 'testhost')
        os.makedirs(host_dir)

        # Create config file
        config_content = b'''console_impl=qemu
qemu_machine=virt
qemu_binary=qemu-system-riscv64
qemu_extra_args="-m 1G"
qemu_kernel_args="-kernel ${U_BOOT_BUILD_DIR}/u-boot.bin"
'''
        config_path = os.path.join(host_dir, 'conf.testboard_na')
        tools.write_file(config_path, config_content)

        # Mock settings
        mock_settings.get.side_effect = lambda key, fallback=None: {
            'test_hooks': hooks_dir,
            'build_dir': '/tmp/b',
        }.get(key, fallback)

        args = make_args(cmd='pytest', board='testboard', build_dir=None)
        result = cmdpy.get_qemu_command('testboard', args)

        self.assertIn('qemu-system-riscv64', result)
        self.assertIn('-M virt', result)
        self.assertIn('-m 1G', result)
        self.assertIn('/tmp/b/testboard/u-boot.bin', result)

    @mock.patch('uman_pkg.cmdpy.settings')
    def test_get_qemu_command_no_hooks(self, mock_settings):
        """Test get_qemu_command fails when test_hooks not configured"""
        mock_settings.get.return_value = None

        args = make_args(cmd='pytest', board='testboard')
        with terminal.capture():
            result = cmdpy.get_qemu_command('testboard', args)

        self.assertIsNone(result)

    @mock.patch('uman_pkg.cmdpy.settings')
    @mock.patch('uman_pkg.cmdpy.socket')
    def test_dry_run_shows_qemu_cmd(self, mock_socket, mock_settings):
        """Test dry-run pytest shows QEMU command line"""
        mock_socket.gethostname.return_value = 'testhost'

        # Create config directory structure
        hooks_dir = os.path.join(self.test_dir, 'hooks')
        host_dir = os.path.join(hooks_dir, 'bin', 'testhost')
        os.makedirs(host_dir)

        config_content = b'''console_impl=qemu
qemu_machine=virt
qemu_binary=qemu-system-riscv64
qemu_extra_args="-m 1G"
qemu_kernel_args="-bios ${U_BOOT_BUILD_DIR}/u-boot"
'''
        tools.write_file(
            os.path.join(host_dir, 'conf.myboard_na'),
            config_content)

        mock_settings.get.side_effect = lambda key, fallback=None: {
            'test_hooks': hooks_dir,
            'build_dir': '/tmp/b',
        }.get(key, fallback)

        args = make_args(cmd='pytest', board='myboard',
                         dry_run=True)

        with terminal.capture() as (out, _):
            res = control.run_command(args)

        self.assertEqual(0, res)
        output = out.getvalue()
        self.assertIn('qemu-system-riscv64', output)
        self.assertIn('-M virt', output)
        self.assertIn('-bios /tmp/b/myboard/u-boot', output)


class TestGitLabParser(TestBase):
    """Test GitLab CI file parsing functionality"""

    def test_gitlab_ci_parser_class(self):
        """Test GitLabCIParser class functionality"""
        # Test direct class usage
        parser = gitlab_parser.GitLabCIParser()

        # Should have properties with lists
        self.assertIsInstance(parser.roles, list)
        self.assertIsInstance(parser.boards, list)
        self.assertIsInstance(parser.job_names, list)

    def test_validate_sjg_value(self):
        """Test SJG value validation with class"""
        parser = gitlab_parser.GitLabCIParser()

        # Test special case - should always be valid
        self.assertTrue(control.validate_sjg_value('1', parser))

        # Test with real parser data
        if parser.roles:
            valid_role = parser.roles[0]
            self.assertIn(valid_role, parser.roles)

        # Invalid value should not be in roles
        self.assertNotIn('definitely_invalid_role_12345', parser.roles)

    def test_validate_sage_value(self):
        """Test SAGE value validation with class"""
        parser = gitlab_parser.GitLabCIParser()

        # Special values are always valid
        self.assertTrue(control.validate_sage_value('1', parser))
        self.assertTrue(control.validate_sage_value('', parser))
        self.assertTrue(control.validate_sage_value('help', parser))

        # Real sage job (if any) is valid
        if parser.sage_names:
            self.assertTrue(
                control.validate_sage_value(parser.sage_names[0], parser))

        # Bogus job name is not valid
        self.assertFalse(
            control.validate_sage_value(
                'definitely_invalid_sage_job_xyz', parser))

    def test_validate_pytest_value(self):
        """Test pytest value validation with class"""
        parser = gitlab_parser.GitLabCIParser()

        # Test special case - should always be valid
        self.assertTrue(control.validate_pytest_value('1', parser))

        # Test with real parser data
        if parser.boards:
            valid_board = parser.boards[0]
            self.assertIn(valid_board, parser.boards)

        # Invalid value should not be in boards
        self.assertNotIn('definitely_invalid_board_12345', parser.boards)

    def test_job_names_extraction(self):
        """Test that job names are correctly extracted"""
        parser = gitlab_parser.GitLabCIParser()

        # Should find pytest job names
        self.assertIsInstance(parser.job_names, list)

        # Should include some known job patterns if GitLab CI file exists
        job_names_str = ' '.join(parser.job_names)
        if 'test.py' in job_names_str:
            # Should have at least one job ending with test.py
            has_test_py = any(job.endswith('test.py')
                              for job in parser.job_names)
            self.assertTrue(has_test_py)

    def test_parser_consistency(self):
        """Test that parser instances return consistent data"""
        # Multiple parser instances should return the same data
        parser1 = gitlab_parser.GitLabCIParser()
        parser2 = gitlab_parser.GitLabCIParser()

        # Data should be identical
        self.assertEqual(parser1.roles, parser2.roles)
        self.assertEqual(parser1.boards, parser2.boards)
        self.assertEqual(parser1.job_names, parser2.job_names)

    def test_find_gitlab_ci_file_cwd(self):
        """Test that find_gitlab_ci_file() checks the current directory"""
        old_cwd = os.getcwd()
        orig_usrc = os.environ.get('USRC')
        try:
            if 'USRC' in os.environ:
                del os.environ['USRC']
            os.chdir(self.test_dir)

            # Create a fake .gitlab-ci.yml in the current directory
            ci_file = os.path.join(self.test_dir, '.gitlab-ci.yml')
            with open(ci_file, 'w', encoding='utf-8') as fout:
                fout.write('test:\n  script: echo hello\n')

            result = gitlab_parser.find_gitlab_ci_file()
            self.assertEqual(ci_file, result)
        finally:
            os.chdir(old_cwd)
            if orig_usrc is not None:
                os.environ['USRC'] = orig_usrc
            elif 'USRC' in os.environ:
                del os.environ['USRC']

    def test_find_gitlab_ci_file_usrc(self):
        """Test that find_gitlab_ci_file() checks $USRC"""
        old_cwd = os.getcwd()
        orig_usrc = os.environ.get('USRC')
        try:
            # cd somewhere with no .gitlab-ci.yml nearby
            empty_dir = os.path.join(self.test_dir, 'empty')
            os.makedirs(empty_dir)
            os.chdir(empty_dir)

            # Create a fake .gitlab-ci.yml in a separate directory
            src_dir = os.path.join(self.test_dir, 'src')
            os.makedirs(src_dir)
            ci_file = os.path.join(src_dir, '.gitlab-ci.yml')
            with open(ci_file, 'w', encoding='utf-8') as fout:
                fout.write('test:\n  script: echo hello\n')

            # Without USRC, should not find it
            if 'USRC' in os.environ:
                del os.environ['USRC']
            result = gitlab_parser.find_gitlab_ci_file()
            self.assertIsNone(result)

            # With USRC pointing to src_dir, should find it
            os.environ['USRC'] = src_dir
            result = gitlab_parser.find_gitlab_ci_file()
            self.assertEqual(ci_file, result)
        finally:
            os.chdir(old_cwd)
            if orig_usrc is not None:
                os.environ['USRC'] = orig_usrc
            elif 'USRC' in os.environ:
                del os.environ['USRC']


class TestUmanMergeRequest(TestBase):
    """Tests for merge request functionality"""

    def setUp(self):
        super().setUp()
        self.old_cwd = os.getcwd()
        os.chdir(self.test_dir)
        # Initialize git repo
        command.run('git', 'init', **CAPTURE)
        command.run('git', 'config', 'user.email', 'test@test.com', **CAPTURE)
        command.run('git', 'config', 'user.name', 'Test User', **CAPTURE)

    def tearDown(self):
        os.chdir(self.old_cwd)
        super().tearDown()

    def test_merge_request_parsing(self):
        """Test parsing of command line for merge request"""
        parser = cmdline.setup_parser()

        # Test --merge flag
        args = parser.parse_args(['ci', '--merge'])
        self.assertTrue(args.merge)

        # Test -m flag
        args = parser.parse_args(['ci', '-m'])
        self.assertTrue(args.merge)

        # Test default (no merge)
        args = parser.parse_args(['ci'])
        self.assertFalse(args.merge)

    def test_destination_parsing(self):
        """Test parsing of destination argument"""
        parser = cmdline.setup_parser()

        # Test default destination (None means use current branch name)
        args = parser.parse_args(['ci'])
        self.assertEqual(args.dest, None)

        # Test custom branch name
        args = parser.parse_args(['ci', '--dest', 'my-feature'])
        self.assertEqual(args.dest, 'my-feature')

        # Test branch with hash-like name
        args = parser.parse_args(['ci', '-d', 'cherry-abc123'])
        self.assertEqual(args.dest, 'cherry-abc123')

        # Test short flag
        args = parser.parse_args(['ci', '-d', 'develop'])
        self.assertEqual(args.dest, 'develop')

    @mock.patch('pickman.gitlab_api', create=True)
    @mock.patch('gitlab.Gitlab')
    @mock.patch('uman_pkg.control.extract_mr_info')
    @mock.patch('u_boot_pylib.gitutil.get_branch')
    def test_merge_request_gitlab_error(self, mock_get_branch, mock_extract,
                                        mock_gitlab_cls, mock_api):
        """Test that GitLab errors cause failure"""
        # Set up mocks
        mock_get_branch.return_value = 'test-branch'
        mock_extract.return_value = ('Test Title', 'Test Description', '')
        mock_api.get_remote_url.return_value = \
            'https://gitlab.com/user/repo.git'
        mock_api.parse_url.return_value = ('gitlab.com', 'user/repo')
        mock_api.get_token.return_value = 'fake-token'

        # Make GitLab raise an error - use real exception class
        mock_gitlab_cls.return_value.projects.get.side_effect = \
            gitlab.GitlabError('Connection failed')

        args = make_args(merge=True)
        with terminal.capture():
            result = control.do_merge_request(args)

        self.assertEqual(1, result)

    @mock.patch('pickman.gitlab_api', create=True)
    @mock.patch('gitlab.Gitlab')
    @mock.patch('uman_pkg.control.extract_mr_info')
    @mock.patch('u_boot_pylib.gitutil.get_branch')
    @mock.patch('uman_pkg.control.git_push_branch')
    def test_merge_request_update_existing(self, _mock_push, mock_get_branch,
                                           mock_extract, mock_gitlab_cls,
                                           mock_api):
        """Test updating an existing merge request"""
        # Set up mocks
        mock_get_branch.return_value = 'test-branch'
        mock_extract.return_value = ('New Title', 'New Description', '')
        mock_api.get_remote_url.return_value = \
            'https://gitlab.com/user/repo.git'
        mock_api.parse_url.return_value = ('gitlab.com', 'user/repo')
        mock_api.get_token.return_value = 'fake-token'

        # Mock existing MR
        mock_mr = mock.MagicMock()
        mock_mr.web_url = 'https://gitlab.com/user/repo/-/merge_requests/1'
        mock_project = mock.MagicMock()
        mock_project.mergerequests.list.return_value = [mock_mr]
        mock_gitlab_cls.return_value.projects.get.return_value = mock_project

        args = make_args(merge=True)
        with terminal.capture():
            result = control.do_merge_request(args)

        self.assertEqual(0, result)
        self.assertEqual('New Title', mock_mr.title)
        self.assertEqual('New Description', mock_mr.description)
        mock_mr.save.assert_called_once()


class TestSettings(TestBase):
    """Tests for settings module"""

    def setUp(self):
        super().setUp()
        self.config_file = os.path.join(self.test_dir, '.uman')
        # Reset global settings state
        settings.SETTINGS['config'] = None

    def tearDown(self):
        settings.SETTINGS['config'] = None
        super().tearDown()

    def test_get_all_creates_config(self):
        """Test that get_all creates config file if missing"""
        # Patch expanduser to use our test directory
        orig_expanduser = os.path.expanduser
        os.path.expanduser = lambda p: p.replace('~', self.test_dir)

        try:
            with terminal.capture():
                cfg = settings.get_all()
            self.assertIsNotNone(cfg)
            self.assertTrue(os.path.exists(self.config_file))
        finally:
            os.path.expanduser = orig_expanduser

    def test_get_with_fallback(self):
        """Test get returns fallback for missing keys"""
        # Create a minimal config file
        tools.write_file(self.config_file,
                         b'[DEFAULT]\nbuild_dir = /tmp/test\n')

        orig_expanduser = os.path.expanduser
        os.path.expanduser = lambda p: p.replace('~', self.test_dir)

        try:
            with terminal.capture():
                # Existing setting
                self.assertEqual('/tmp/test', settings.get('build_dir'))
                # Missing setting with fallback
                val = settings.get('missing', 'default')
                self.assertEqual('default', val)
        finally:
            os.path.expanduser = orig_expanduser

    def test_get_expands_paths(self):
        """Test that get expands ~ and env vars"""
        tools.write_file(self.config_file,
                         b'[DEFAULT]\ntest_path = ~/mydir\n')

        orig_expanduser = os.path.expanduser
        os.path.expanduser = lambda p: p.replace('~', self.test_dir)

        try:
            with terminal.capture():
                result = settings.get('test_path')
            self.assertEqual(f'{self.test_dir}/mydir', result)
        finally:
            os.path.expanduser = orig_expanduser

    def test_get_none_fallback(self):
        """Test that get returns None when fallback is None"""
        tools.write_file(self.config_file,
                         b'[DEFAULT]\nbuild_dir = /tmp/test\n')

        orig_expanduser = os.path.expanduser
        os.path.expanduser = lambda p: p.replace('~', self.test_dir)

        try:
            with terminal.capture():
                result = settings.get('missing_key', fallback=None)
            self.assertIsNone(result)
        finally:
            os.path.expanduser = orig_expanduser


class TestCcSubcommand(TestBase):  # pylint: disable=R0904
    """Tests for the cc subcommand"""

    def setUp(self):
        super().setUp()
        tout.init(tout.NOTICE)
        self.config_file = os.path.join(self.test_dir, '.uman')
        settings.SETTINGS['config'] = None

    def tearDown(self):
        settings.SETTINGS['config'] = None
        super().tearDown()

    def test_cc_parsing_defaults(self):
        """Test that cc subcommand parses with defaults"""
        args = cmdline.parse_args(['cc'])
        self.assertEqual('claude-code', args.cmd)
        self.assertEqual('noble', args.base)
        self.assertIsNone(args.name)
        self.assertFalse(args.shell)
        self.assertFalse(args.cont)
        self.assertFalse(args.ephemeral)

    def test_cc_parsing_shell(self):
        """Test cc -s flag"""
        args = cmdline.parse_args(['cc', '-s'])
        self.assertTrue(args.shell)

    def test_cc_parsing_base(self):
        """Test cc -b flag"""
        args = cmdline.parse_args(['cc', '-b', 'jammy'])
        self.assertEqual('jammy', args.base)

    def test_cc_parsing_name(self):
        """Test name as positional arg"""
        args = cmdline.parse_args(['cc', 'mybox'])
        self.assertEqual('mybox', args.name)

    def test_cc_parsing_continue(self):
        """Test cc -c flag"""
        args = cmdline.parse_args(['cc', '-c', 'mybox'])
        self.assertTrue(args.cont)
        self.assertEqual('mybox', args.name)

    def test_cc_parsing_ephemeral(self):
        """Test cc -e flag"""
        args = cmdline.parse_args(['cc', '-e'])
        self.assertTrue(args.ephemeral)

    def test_cc_parsing_restart(self):
        """Test cc -R flag"""
        args = cmdline.parse_args(['cc', '-R', 'mybox'])
        self.assertTrue(args.restart)
        self.assertEqual('mybox', args.name)

    def test_default_name_from_dir(self):
        """Test that default name comes from current directory"""
        args = cmdline.parse_args(['-n', 'cc'])

        tools.write_file(self.config_file,
                         b'[DEFAULT]\nbuild_dir = /tmp/b\n')
        orig_expanduser = os.path.expanduser
        os.path.expanduser = lambda p: p.replace('~', self.test_dir)
        try:
            with terminal.capture() as (out, _):
                cc.run(args)
            output = out.getvalue()
            # Should use current dir basename as container name
            dirname = os.path.basename(os.path.realpath(os.getcwd()))
            self.assertIn(dirname, output)
            # Permanent by default, no delete
            self.assertNotIn('lxc delete', output)
        finally:
            os.path.expanduser = orig_expanduser

    def test_gen_name(self):
        """Test gen_name returns correct format"""
        name = cc.gen_name('noble')
        self.assertTrue(name.startswith('ubuntu-noble-'))
        # Should have 4 hex chars after the last dash
        suffix = name.split('-')[-1]
        self.assertEqual(4, len(suffix))
        # All chars should be valid hex
        int(suffix, 16)

    def test_get_uman_dir(self):
        """Test get_uman_dir returns a valid path"""
        uman_dir = cc.get_uman_dir()
        self.assertTrue(os.path.isdir(uman_dir))
        # Should contain the uman_pkg directory
        self.assertTrue(
            os.path.isdir(os.path.join(uman_dir, 'uman_pkg')))

    def test_get_essential_mounts(self):
        """Test get_essential_mounts returns expected names and paths"""
        mounts = cc.get_essential_mounts('/tmp/myproject')
        names = [m[0] for m in mounts]
        self.assertIn('datadir', names)
        self.assertIn('claudejson', names)
        self.assertIn('claudedir', names)
        self.assertIn('hostbin', names)
        self.assertIn('uman', names)

        # Check datadir maps to the project source
        datadir = [m for m in mounts if m[0] == 'datadir'][0]
        self.assertEqual('/tmp/myproject', datadir[1])
        self.assertEqual(cc.PROJECT_DEST, datadir[2])

    def test_get_config_mounts_no_section(self):
        """Test get_config_mounts with no [claude-code] section"""
        tools.write_file(self.config_file,
                         b'[DEFAULT]\nbuild_dir = /tmp/b\n')
        orig_expanduser = os.path.expanduser
        os.path.expanduser = lambda p: p.replace('~', self.test_dir)
        try:
            with terminal.capture():
                mounts = cc.get_config_mounts()
            self.assertEqual([], mounts)
        finally:
            os.path.expanduser = orig_expanduser

    def test_get_config_mounts_parses(self):
        """Test get_config_mounts parses valid entries"""
        config = (b'[DEFAULT]\nbuild_dir = /tmp/b\n\n'
                  b'[claude-code]\nmounts =\n'
                  b'    tc:/opt/tc:/home/ubuntu/tc\n'
                  b'    data:/srv/data:/home/ubuntu/data\n')
        tools.write_file(self.config_file, config)
        orig_expanduser = os.path.expanduser
        os.path.expanduser = lambda p: p.replace('~', self.test_dir)
        try:
            with terminal.capture():
                mounts = cc.get_config_mounts()
            self.assertEqual(2, len(mounts))
            self.assertEqual('tc', mounts[0][0])
            self.assertEqual('/opt/tc', mounts[0][1])
            self.assertEqual('/home/ubuntu/tc', mounts[0][2])
            self.assertEqual('data', mounts[1][0])
        finally:
            os.path.expanduser = orig_expanduser

    def test_get_cli_mounts_none(self):
        """Test get_cli_mounts with no arguments"""
        self.assertEqual([], cc.get_cli_mounts(None))

    def test_get_cli_mounts_host_only(self):
        """Test get_cli_mounts with host path only"""
        mounts = cc.get_cli_mounts(['/opt/data'])
        self.assertEqual(1, len(mounts))
        self.assertEqual('data', mounts[0][0])
        self.assertEqual('/opt/data', mounts[0][1])
        self.assertEqual('/opt/data', mounts[0][2])

    def test_get_cli_mounts_host_dest(self):
        """Test get_cli_mounts with host:dest format"""
        mounts = cc.get_cli_mounts(['/opt/data:/mnt/data'])
        self.assertEqual(1, len(mounts))
        self.assertEqual('data', mounts[0][0])
        self.assertEqual('/opt/data', mounts[0][1])
        self.assertEqual('/mnt/data', mounts[0][2])

    def test_get_cli_mounts_expands_tilde(self):
        """Test get_cli_mounts maps ~ to container home"""
        mounts = cc.get_cli_mounts(['~/dev/u-boot'])
        home = os.path.expanduser('~')
        self.assertEqual(f'{home}/dev/u-boot', mounts[0][1])
        self.assertEqual('/home/ubuntu/dev/u-boot', mounts[0][2])

    def test_get_cli_mounts_dest_tilde(self):
        """Test get_cli_mounts maps ~ in dest to container home"""
        mounts = cc.get_cli_mounts(['/opt/data:~/data'])
        self.assertEqual('/opt/data', mounts[0][1])
        self.assertEqual('/home/ubuntu/data', mounts[0][2])

    def test_get_cli_mounts_multiple(self):
        """Test get_cli_mounts with multiple mounts"""
        mounts = cc.get_cli_mounts(['/opt/a', '/opt/b:/mnt/b'])
        self.assertEqual(2, len(mounts))
        self.assertEqual('a', mounts[0][0])
        self.assertEqual('/opt/a', mounts[0][1])
        self.assertEqual('b', mounts[1][0])
        self.assertEqual('/mnt/b', mounts[1][2])

    def test_get_cli_mounts_duplicate_leaf(self):
        """Test get_cli_mounts adds suffix for duplicate leaf names"""
        mounts = cc.get_cli_mounts(['/opt/data', '/mnt/data'])
        self.assertEqual(2, len(mounts))
        self.assertEqual('data', mounts[0][0])
        self.assertEqual('data2', mounts[1][0])

    def test_show_mounts(self):
        """Test show_mounts parses lxc device output"""
        yaml = ('datadir:\n'
                '  path: /home/ubuntu/project\n'
                '  source: /home/sglass/dev/myproj\n'
                '  type: disk\n'
                'hostbin:\n'
                '  path: /home/ubuntu/bin\n'
                '  source: /home/sglass/bin\n'
                '  type: disk\n')
        result = command.CommandResult(return_code=0, stdout=yaml)
        with mock.patch.object(cc, 'exec_cmd', return_value=result):
            with terminal.capture() as (out, err):
                ret = cc.show_mounts('mybox')
        self.assertEqual(0, ret)
        self.assertIn('datadir', out.getvalue())
        self.assertIn('/home/ubuntu/project', out.getvalue())
        self.assertFalse(err.getvalue())

    def test_show_mounts_not_found(self):
        """Test show_mounts with missing container"""
        result = command.CommandResult(return_code=1, stdout='')
        with mock.patch.object(cc, 'exec_cmd', return_value=result):
            with terminal.capture() as (out, err):
                ret = cc.show_mounts('nobox')
        self.assertEqual(1, ret)
        self.assertIn('not found', err.getvalue())
        self.assertFalse(out.getvalue())

    def test_mount_only(self):
        """Test -m without -s just adds the mount and exits"""
        args = cmdline.parse_args(['cc', '-m', '/opt/data', 'mybox'])
        with mock.patch.object(cc, 'container_exists', return_value=True):
            with mock.patch.object(cc, 'add_mount') as mock_add:
                with terminal.capture() as (out, err):
                    ret = cc.run(args)
        self.assertEqual(0, ret)
        mock_add.assert_called_once_with(
            'mybox', 'data', '/opt/data', '/opt/data', False)
        self.assertIn('/opt/data -> /opt/data (data)', out.getvalue())

    def test_mount_only_no_container(self):
        """Test -m fails if the container does not exist"""
        args = cmdline.parse_args(['cc', '-m', '/opt/data', 'mybox'])
        with mock.patch.object(cc, 'container_exists', return_value=False):
            with terminal.capture() as (out, err):
                ret = cc.run(args)
        self.assertEqual(1, ret)
        self.assertIn('not found', err.getvalue())

    def test_unmount(self):
        """Test -u removes a mount device"""
        args = cmdline.parse_args(['-n', 'cc', '-u', 'linux', 'mybox'])
        with terminal.capture() as (out, err):
            ret = cc.run(args)
        self.assertEqual(0, ret)
        self.assertIn('lxc config device remove', out.getvalue())
        self.assertIn('linux', out.getvalue())

    def test_unmount_missing(self):
        """Test -u fails if the device does not exist"""
        args = cmdline.parse_args(['cc', '-u', 'nosuch', 'mybox'])
        result = command.CommandResult(return_code=1, stdout='', stderr='')
        with mock.patch.object(cc, 'container_exists', return_value=True):
            with mock.patch.object(cc, 'has_mount', return_value=False):
                with terminal.capture() as (out, err):
                    ret = cc.run(args)
        self.assertEqual(1, ret)
        self.assertIn('nosuch', err.getvalue())

    def test_unmount_no_container(self):
        """Test -u fails if the container does not exist"""
        args = cmdline.parse_args(['cc', '-u', 'linux', 'mybox'])
        with mock.patch.object(cc, 'container_exists', return_value=False):
            with terminal.capture() as (out, err):
                ret = cc.run(args)
        self.assertEqual(1, ret)
        self.assertIn('not found', err.getvalue())

    def test_get_git_symlink_mount_no_symlink(self):
        """Test get_git_symlink_mount when .git is not a symlink"""
        # Create a regular .git directory
        os.makedirs(os.path.join(self.test_dir, '.git'))
        result = cc.get_git_symlink_mount(self.test_dir)
        self.assertIsNone(result)

    def test_get_git_symlink_mount_with_symlink(self):
        """Test get_git_symlink_mount when .git is a symlink"""
        # Create a target and a .git symlink
        git_target = os.path.join(self.test_dir, 'real_git')
        os.makedirs(git_target)
        git_link = os.path.join(self.test_dir, 'project')
        os.makedirs(git_link)
        os.symlink(git_target, os.path.join(git_link, '.git'))

        result = cc.get_git_symlink_mount(git_link)
        self.assertIsNotNone(result)
        self.assertEqual('dotgit', result[0])
        self.assertEqual(git_target, result[1])

    def test_dry_run(self):
        """Test run with dry_run shows lxc commands"""
        args = cmdline.parse_args(['-n', 'cc', 'test-cc'])

        # Set up minimal config
        tools.write_file(self.config_file,
                         b'[DEFAULT]\nbuild_dir = /tmp/b\n')
        orig_expanduser = os.path.expanduser
        os.path.expanduser = lambda p: p.replace('~', self.test_dir)
        try:
            with terminal.capture() as (out, _):
                cc.run(args)
            output = out.getvalue()
            self.assertIn('lxc init', output)
            self.assertIn('lxc start', output)
            # Named containers are kept
            self.assertNotIn('lxc delete', output)
            self.assertIn('test-cc', output)
            self.assertNotIn('--continue', output)
            # /tmp/b mount not present without -o
            self.assertNotIn('tmpb', output)
            # uman env written and sourced
            self.assertIn('.uman_env', output)
            self.assertIn('.bashrc', output)
            self.assertIn('.profile', output)
            # Session logging via script
            self.assertIn('script -q -c', output)
            self.assertIn('Logging to', output)
            self.assertIn('uman-logs/test-cc/', output)
        finally:
            os.path.expanduser = orig_expanduser

    def test_dry_run_continue(self):
        """Test that -c passes --continue to claude"""
        args = cmdline.parse_args(['-n', 'cc', '-c', 'test-cc'])

        tools.write_file(self.config_file,
                         b'[DEFAULT]\nbuild_dir = /tmp/b\n')
        orig_expanduser = os.path.expanduser
        os.path.expanduser = lambda p: p.replace('~', self.test_dir)
        try:
            with terminal.capture() as (out, _):
                cc.run(args)
            self.assertIn('--continue', out.getvalue())
        finally:
            os.path.expanduser = orig_expanduser

    def test_dry_run_ephemeral(self):
        """Test that ephemeral containers are deleted on exit"""
        args = cmdline.parse_args(['-n', 'cc', '-e'])

        tools.write_file(self.config_file,
                         b'[DEFAULT]\nbuild_dir = /tmp/b\n')
        orig_expanduser = os.path.expanduser
        os.path.expanduser = lambda p: p.replace('~', self.test_dir)
        try:
            with terminal.capture() as (out, _):
                cc.run(args)
            output = out.getvalue()
            self.assertIn('lxc init', output)
            self.assertIn('lxc delete', output)
        finally:
            os.path.expanduser = orig_expanduser

    def test_dry_run_restart(self):
        """Test that -R passes through in dry run"""
        args = cmdline.parse_args(['-n', 'cc', '-R', 'test-cc'])

        tools.write_file(self.config_file,
                         b'[DEFAULT]\nbuild_dir = /tmp/b\n')
        orig_expanduser = os.path.expanduser
        os.path.expanduser = lambda p: p.replace('~', self.test_dir)
        try:
            with terminal.capture() as (out, _):
                cc.run(args)
            output = out.getvalue()
            # In dry run, restart has no effect (container doesn't exist)
            # but the normal start should still appear
            self.assertIn('lxc start', output)
        finally:
            os.path.expanduser = orig_expanduser

    def test_editor_proxy(self):
        """Test editor proxy translates paths and opens host editor"""
        import socket as socket_mod

        project_src = self.test_dir
        opened = []
        orig_run = subprocess.run
        subprocess.run = lambda cmd, **kw: opened.append(cmd)

        sock_path = cc.start_editor_proxy(project_src)
        try:
            self.assertTrue(os.path.exists(sock_path))

            # Connect and send a container path
            sock = socket_mod.socket(socket_mod.AF_UNIX,
                                     socket_mod.SOCK_STREAM)
            sock.connect(sock_path)
            sock.sendall(f'{cc.PROJECT_DEST}/foo.c\n'.encode())
            resp = sock.recv(4096).decode().strip()
            sock.close()

            self.assertEqual('done', resp)
            self.assertEqual(1, len(opened))
            self.assertEqual(os.path.join(project_src, 'foo.c'),
                             opened[0][1])
        finally:
            subprocess.run = orig_run
            if os.path.exists(sock_path):
                os.unlink(sock_path)

    def test_editor_proxy_content_transfer(self):
        """Test editor proxy handles non-project paths via content"""
        import json
        import socket as socket_mod

        project_src = self.test_dir
        opened = []
        orig_run = subprocess.run
        subprocess.run = lambda cmd, **kw: opened.append(cmd)

        sock_path = cc.start_editor_proxy(project_src)
        try:
            sock = socket_mod.socket(socket_mod.AF_UNIX,
                                     socket_mod.SOCK_STREAM)
            sock.connect(sock_path)
            msg = json.dumps({'content': 'hello', 'ext': '.txt'}) + '\n'
            sock.sendall(msg.encode())
            resp_raw = b''
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                resp_raw += chunk
            sock.close()

            resp = json.loads(resp_raw.decode())
            self.assertIn('content', resp)
            self.assertEqual(1, len(opened))
        finally:
            subprocess.run = orig_run
            if os.path.exists(sock_path):
                os.unlink(sock_path)

    def test_editor_proxy_outside_project(self):
        """Test editor proxy rejects paths outside the project"""
        import socket as socket_mod

        project_src = self.test_dir
        sock_path = cc.start_editor_proxy(project_src)
        try:
            sock = socket_mod.socket(socket_mod.AF_UNIX,
                                     socket_mod.SOCK_STREAM)
            sock.connect(sock_path)
            sock.sendall(b'/etc/passwd\n')
            resp = sock.recv(4096).decode().strip()
            sock.close()

            self.assertIn('error', resp)
        finally:
            if os.path.exists(sock_path):
                os.unlink(sock_path)

    def test_run_command_cc(self):
        """Test run_command dispatches to cc correctly"""
        args = cmdline.parse_args(['-n', 'cc', 'test-cc'])
        args.col = terminal.Color()

        # Set up minimal config
        tools.write_file(self.config_file,
                         b'[DEFAULT]\nbuild_dir = /tmp/b\n')
        orig_expanduser = os.path.expanduser
        os.path.expanduser = lambda p: p.replace('~', self.test_dir)
        try:
            with terminal.capture() as (out, _):
                res = control.run_command(args)
            self.assertEqual(0, res)
            self.assertIn('lxc init', out.getvalue())
        finally:
            os.path.expanduser = orig_expanduser

    def test_cc_parsing_list(self):
        """Test cc -l flag"""
        args = cmdline.parse_args(['cc', '-l'])
        self.assertTrue(args.list_containers)

    def test_cc_parsing_delete(self):
        """Test cc -d flag"""
        args = cmdline.parse_args(['cc', '-d', 'mybox'])
        self.assertTrue(args.delete)
        self.assertEqual('mybox', args.name)

    def test_delete(self):
        """Test delete runs lxc delete"""
        args = cmdline.parse_args(['-n', 'cc', '-d', 'mybox'])
        with terminal.capture() as (out, _):
            res = cc.run(args)
        self.assertEqual(0, res)
        self.assertIn('lxc delete', out.getvalue())
        self.assertIn('mybox', out.getvalue())

    def test_delete_no_name(self):
        """Test delete without a name fails"""
        args = cmdline.parse_args(['cc', '-d'])
        with terminal.capture() as (_, _):
            res = cc.run(args)
        self.assertEqual(1, res)

    def test_cc_parsing_stop(self):
        """Test cc -S flag"""
        args = cmdline.parse_args(['cc', '-S', 'mybox'])
        self.assertTrue(args.stop)
        self.assertEqual('mybox', args.name)

    def test_stop(self):
        """Test stop runs lxc stop"""
        args = cmdline.parse_args(['-n', 'cc', '-S', 'mybox'])
        with terminal.capture() as (out, _):
            res = cc.run(args)
        self.assertEqual(0, res)
        self.assertIn('lxc stop', out.getvalue())
        self.assertIn('mybox', out.getvalue())

    def test_stop_no_name(self):
        """Test stop without a name fails"""
        args = cmdline.parse_args(['cc', '-S'])
        with terminal.capture() as (_, _):
            res = cc.run(args)
        self.assertEqual(1, res)

    def test_container_exists(self):
        """Test container_exists with missing container"""
        command.TEST_RESULT = command.CommandResult(
            return_code=1, stdout='', stderr='not found')
        self.assertFalse(cc.container_exists('no-such-container'))

    def test_container_status(self):
        """Test container_status parses lxc info output"""
        info = 'Name: test\nStatus: RUNNING\nType: container\n'
        command.TEST_RESULT = command.CommandResult(
            return_code=0, stdout=info, stderr='')
        self.assertEqual('RUNNING', cc.container_status('test'))

    def test_container_status_missing(self):
        """Test container_status with missing container"""
        command.TEST_RESULT = command.CommandResult(
            return_code=1, stdout='', stderr='not found')
        self.assertIsNone(cc.container_status('no-such'))

    def test_has_mount(self):
        """Test has_mount with existing device"""
        command.TEST_RESULT = command.CommandResult(
            return_code=0, stdout='/some/path', stderr='')
        self.assertTrue(cc.has_mount('mycontainer', 'datadir'))

    def test_has_mount_missing(self):
        """Test has_mount with absent device"""
        command.TEST_RESULT = command.CommandResult(
            return_code=1, stdout='', stderr='not found')
        self.assertFalse(cc.has_mount('mycontainer', 'nodev'))

    def test_get_project(self):
        """Test get_project returns the datadir source"""
        command.TEST_RESULT = command.CommandResult(
            return_code=0, stdout='/home/user/dev/uboot\n', stderr='')
        self.assertEqual('/home/user/dev/uboot',
                         cc.get_project('test'))

    def test_get_project_missing(self):
        """Test get_project with no datadir device"""
        command.TEST_RESULT = command.CommandResult(
            return_code=1, stdout='', stderr='not found')
        self.assertEqual('', cc.get_project('test'))

    def test_list_containers(self):
        """Test list_containers filters by datadir presence"""
        csv = ('mybox,RUNNING\n'
               'devbox,STOPPED\n'
               'other-thing,RUNNING\n')
        projects = {
            'mybox': '/home/user/dev/uboot',
            'devbox': '/home/user/dev/linux',
        }

        priv = {'mybox': 'true', 'devbox': ''}

        def mock_lxc(pipe_list, **_kwargs):
            cmd = pipe_list[0]
            if 'device' in cmd:
                name = cmd[cmd.index('get') + 1]
                proj = projects.get(name)
                if proj:
                    return command.CommandResult(
                        return_code=0, stdout=proj + '\n', stderr='')
                return command.CommandResult(
                    return_code=1, stdout='', stderr='not found')
            if 'security.privileged' in cmd:
                name = cmd[cmd.index('get') + 1]
                return command.CommandResult(
                    return_code=0, stdout=priv.get(name, '') + '\n',
                    stderr='')
            return command.CommandResult(
                return_code=0, stdout=csv, stderr='')

        command.TEST_RESULT = mock_lxc
        result = cc.list_containers()
        self.assertEqual(
            [('mybox', 'RUNNING', '/home/user/dev/uboot', True),
             ('devbox', 'STOPPED', '/home/user/dev/linux', False)],
            result)

    def test_list_containers_empty(self):
        """Test list_containers with no containers"""
        command.TEST_RESULT = command.CommandResult(
            return_code=0, stdout='', stderr='')
        self.assertEqual([], cc.list_containers())

    def test_reuse_existing(self):
        """Test that reusing a container skips init and delete"""
        args = cmdline.parse_args(['cc', 'test-cc'])

        tools.write_file(self.config_file,
                         b'[DEFAULT]\nbuild_dir = /tmp/b\n')
        orig_expanduser = os.path.expanduser
        os.path.expanduser = (
            lambda p: p.replace('~', self.test_dir))

        # Track which lxc commands are run
        cmds = []
        orig_exec = cc.exec_cmd

        def fake_exec(cmd, _dry_run=False, **_kwargs):
            cmds.append(cmd)
            return command.CommandResult(
                return_code=0, stdout='', stderr='')

        orig_exists = cc.container_exists
        orig_status = cc.container_status
        orig_has_mount = cc.has_mount
        cc.container_exists = lambda name: True
        cc.container_status = lambda name: 'RUNNING'
        cc.has_mount = lambda name, mname: True
        cc.exec_cmd = fake_exec
        try:
            with terminal.capture() as (out, _):
                cc.run(args)
            output = out.getvalue()
            self.assertIn('Reusing container', output)
            # Should not have run lxc init or lxc delete
            flat = [' '.join(c) for c in cmds]
            for c in flat:
                self.assertNotIn('lxc init', c)
                self.assertNotIn('lxc delete', c)
        finally:
            cc.container_exists = orig_exists
            cc.container_status = orig_status
            cc.has_mount = orig_has_mount
            cc.exec_cmd = orig_exec
            os.path.expanduser = orig_expanduser

    def test_get_log_path(self):
        """Test get_log_path creates directory and returns expected path"""
        orig_expanduser = os.path.expanduser
        os.path.expanduser = lambda p: p.replace('~', self.test_dir)
        try:
            path = cc.get_log_path('mybox')
            self.assertTrue(path.startswith(
                os.path.join(self.test_dir, 'files/dev/uman-logs/mybox/')))
            self.assertRegex(os.path.basename(path),
                             r'log-\d{2}\.\d{2}[a-z]{3}\.\d{2}-\d{6}\.log')
            self.assertTrue(os.path.isdir(os.path.dirname(path)))
        finally:
            os.path.expanduser = orig_expanduser

    def test_ensure_running_failure(self):
        """Test ensure_running reports an error when 'lxc start' fails"""
        bad = command.CommandResult(return_code=1, stdout='',
                                    stderr='Error: bad mount\n')
        with mock.patch.object(cc, 'lxc', return_value=bad):
            with terminal.capture() as (_, err):
                self.assertFalse(cc.ensure_running('foo', existed=False))
        self.assertIn('Failed to start', err.getvalue())

    def test_ensure_running_success(self):
        """Test ensure_running returns True when start succeeds"""
        good = command.CommandResult(return_code=0, stdout='', stderr='')
        with mock.patch.object(cc, 'lxc', return_value=good):
            self.assertTrue(cc.ensure_running('foo', existed=False))

    def test_wait_for_user_timeout(self):
        """Test wait_for_user times out instead of looping forever"""
        bad = command.CommandResult(return_code=1, stdout='',
                                    stderr='Error: container not running')
        with mock.patch.object(cc, 'exec_cmd', return_value=bad):
            with mock.patch.object(cc.time, 'sleep'):
                with mock.patch.object(cc.time, 'time',
                                       side_effect=[0, 1, 2, 999]):
                    with terminal.capture() as (_, err):
                        self.assertFalse(
                            cc.wait_for_user('foo', timeout=10))
        self.assertIn('Timed out', err.getvalue())


@unittest.skipUnless(shutil.which('lxc'), 'LXC not available')
class TestCcFunctional(unittest.TestCase):
    """Functional tests using a real LXC container"""

    NAME = 'uman-ftest'

    @classmethod
    def setUpClass(cls):
        uman_dir = cc.get_uman_dir()
        uboot_tools = os.path.realpath(os.path.expanduser(
            os.environ.get('UBOOT_TOOLS', '~/u/tools')))
        cc.create_container(cls.NAME, 'noble')
        cc.add_mount(cls.NAME, 'uman', uman_dir, uman_dir)
        cc.add_mount(cls.NAME, 'utools', uboot_tools,
                     f'{cc.UBUNTU_HOME}/u/tools')
        cc.lxc('start', cls.NAME)
        cc.wait_for_user(cls.NAME)
        cc.setup_container(cls.NAME)
        cc.setup_uman(cls.NAME)

    @classmethod
    def tearDownClass(cls):
        cc.delete_container(cls.NAME)

    def lxc_shell(self, cmd, mode):
        """Run a command inside the container in a given shell mode

        Args:
            cmd (str): Shell command string
            mode (str): 'interactive' or 'claude'

        Returns:
            str: Combined stdout+stderr
        """
        if mode == 'interactive':
            args = ['lxc', 'exec', self.NAME, '--', 'sudo', '-iu',
                    'ubuntu', 'bash', '-ic', cmd]
        else:
            args = ['lxc', 'exec', self.NAME, '--', 'sudo', '-iu',
                    'ubuntu', 'bash', '-lc', f'bash -c {cmd!r}']
        result = subprocess.run(args, capture_output=True, check=False,
                                text=True)
        return result.stdout + result.stderr

    def test_interactive_rf(self):
        """Test rf symlink is available in interactive mode"""
        out = self.lxc_shell('type rf', 'interactive')
        self.assertIn('rf is /home/ubuntu/.local/bin/rf', out)

    def test_interactive_um_func(self):
        """Test um shell function is available in interactive mode"""
        out = self.lxc_shell('type um', 'interactive')
        self.assertIn('um is a function', out)

    def test_interactive_alias(self):
        """Test am alias is available in interactive mode"""
        out = self.lxc_shell('type am', 'interactive')
        self.assertIn('am is aliased to', out)

    def test_claude_rf(self):
        """Test rf symlink is available in claude mode"""
        out = self.lxc_shell('type rf', 'claude')
        self.assertIn('rf is /home/ubuntu/.local/bin/rf', out)

    def test_claude_um_func(self):
        """Test um shell function is available in claude mode"""
        out = self.lxc_shell('type um', 'claude')
        self.assertIn('um is a function', out)

    def test_claude_bash_env(self):
        """Test BASH_ENV is set to .uman_env in claude mode"""
        out = self.lxc_shell('echo $BASH_ENV', 'claude')
        self.assertIn('/home/ubuntu/.uman_env', out)


class TestSetupSubcommand(TestBase):
    """Tests for the setup subcommand"""

    def setUp(self):
        """Set up test environment"""
        super().setUp()
        self.orig_cwd = os.getcwd()
        tout.init(tout.NOTICE)

    def tearDown(self):
        """Clean up test environment"""
        os.chdir(self.orig_cwd)
        super().tearDown()

    def test_setup_subcommand_parsing(self):
        """Test that setup subcommand is parsed correctly"""
        parser = cmdline.setup_parser()

        # Test basic setup command
        args = parser.parse_args(['setup'])
        self.assertEqual('setup', args.cmd)
        self.assertIsNone(args.component)
        self.assertFalse(args.list_components)
        self.assertFalse(args.force)

        # Test setup with component
        args = parser.parse_args(['setup', 'opensbi'])
        self.assertEqual('opensbi', args.component)

        # Test setup with --list flag
        args = parser.parse_args(['setup', '-l'])
        self.assertTrue(args.list_components)

        # Test setup with --force flag
        args = parser.parse_args(['setup', 'tfa', '-f'])
        self.assertEqual('tfa', args.component)
        self.assertTrue(args.force)

    def test_setup_list_components(self):
        """Test listing available components"""
        args = argparse.Namespace(
            cmd='setup',
            component=None,
            list_components=True,
            force=False,
            dry_run=False,
            verbose=False,
            debug=False
        )
        with terminal.capture() as (out, _):
            res = setup.do_setup(args)
        self.assertEqual(0, res)
        output = out.getvalue()
        self.assertIn('efi', output)
        self.assertIn('gcc', output)
        self.assertIn('qemu', output)
        self.assertIn('opensbi', output)
        self.assertIn('tfa', output)
        self.assertIn('xtensa', output)

    def test_setup_unknown_component(self):
        """Test setup with unknown component"""
        args = argparse.Namespace(
            cmd='setup',
            component='unknown_component',
            list_components=False,
            force=False,
            dry_run=False,
            verbose=False,
            debug=False
        )
        with terminal.capture() as (_, err):
            res = setup.do_setup(args)
        self.assertEqual(1, res)
        self.assertIn('Unknown component', err.getvalue())

    def test_setup_efi_all_installed(self):
        """Test setup_efi when all packages are installed"""
        args = argparse.Namespace(dry_run=False, force=False)
        with mock.patch('uman_pkg.setup.command.output'):
            with terminal.capture() as (out, _):
                res = setup.setup_efi(args)
        self.assertEqual(0, res)
        self.assertIn('All EFI packages are installed', out.getvalue())

    def test_setup_efi_dry_run(self):
        """Test setup_efi in dry-run mode with missing packages"""
        def mock_output(*cmd):
            """Mock command.output to simulate missing qemu-efi-riscv64"""
            if 'qemu-efi-riscv64' in cmd:
                result = command.CommandResult(return_code=1, stdout='',
                                                stderr='', exception=None)
                raise command.CommandExc('Package not found', result)

        args = argparse.Namespace(dry_run=True, force=False)
        with mock.patch('uman_pkg.setup.command.output', mock_output):
            with terminal.capture() as (out, _):
                res = setup.setup_efi(args)
        self.assertEqual(0, res)
        output = out.getvalue()
        self.assertIn('Would run:', output)
        self.assertIn('qemu-efi-riscv64', output)

    def test_setup_qemu_all_installed(self):
        """Test setup_qemu when all packages are installed"""
        args = argparse.Namespace(dry_run=False, force=False)
        with mock.patch('uman_pkg.setup.command.output'):
            with terminal.capture() as (out, _):
                res = setup.setup_qemu(args)
        self.assertEqual(0, res)
        self.assertIn('All QEMU packages are installed', out.getvalue())

    def test_setup_qemu_dry_run(self):
        """Test setup_qemu in dry-run mode with missing packages"""
        def mock_output(*cmd):
            """Mock command.output to simulate missing qemu-system-ppc"""
            if 'qemu-system-ppc' in cmd:
                result = command.CommandResult(return_code=1, stdout='',
                                                stderr='', exception=None)
                raise command.CommandExc('Package not found', result)

        args = argparse.Namespace(dry_run=True, force=False)
        with mock.patch('uman_pkg.setup.command.output', mock_output):
            with terminal.capture() as (out, _):
                res = setup.setup_qemu(args)
        self.assertEqual(0, res)
        self.assertIn('Would run:', out.getvalue())

    def test_setup_opensbi_dry_run(self):
        """Test setup_opensbi in dry-run mode"""
        args = argparse.Namespace(dry_run=True, force=False)
        with terminal.capture() as (out, _):
            res = setup.setup_opensbi(self.test_dir, args)
        self.assertEqual(0, res)
        self.assertIn('Would download OpenSBI', out.getvalue())

    def test_setup_opensbi_already_present(self):
        """Test setup_opensbi when files already exist"""
        # Create fake opensbi files
        opensbi_dir = os.path.join(self.test_dir, 'opensbi')
        os.makedirs(opensbi_dir)
        tools.write_file(os.path.join(opensbi_dir, 'fw_dynamic.bin'), b'fake')
        tools.write_file(os.path.join(opensbi_dir, 'fw_dynamic_rv32.bin'),
                         b'fake')

        args = argparse.Namespace(dry_run=False, force=False)
        with terminal.capture() as (out, _):
            res = setup.setup_opensbi(self.test_dir, args)
        self.assertEqual(0, res)
        self.assertIn('already present', out.getvalue())

    def test_setup_tfa_dry_run(self):
        """Test setup_tfa in dry-run mode"""
        args = argparse.Namespace(dry_run=True, force=False)
        with terminal.capture() as (out, _):
            res = setup.setup_tfa(self.test_dir, args)
        self.assertEqual(0, res)
        self.assertIn('Would build TF-A', out.getvalue())

    def test_setup_tfa_already_present(self):
        """Test setup_tfa when files already exist"""
        # Create fake TF-A files
        tfa_dir = os.path.join(self.test_dir, 'tfa')
        os.makedirs(tfa_dir)
        tools.write_file(os.path.join(tfa_dir, 'bl1.bin'), b'fake')
        tools.write_file(os.path.join(tfa_dir, 'fip.bin'), b'fake')

        args = argparse.Namespace(dry_run=False, force=False)
        with terminal.capture() as (out, _):
            res = setup.setup_tfa(self.test_dir, args)
        self.assertEqual(0, res)
        self.assertIn('already present', out.getvalue())

    def test_setup_xtensa_dry_run(self):
        """Test setup_xtensa in dry-run mode"""
        args = argparse.Namespace(dry_run=True, force=False)
        with terminal.capture() as (out, _):
            res = setup.setup_xtensa(self.test_dir, args)
        self.assertEqual(0, res)
        self.assertIn('Would download Xtensa', out.getvalue())

    def test_setup_xtensa_already_present(self):
        """Test setup_xtensa when toolchain already exists"""
        # Create fake xtensa toolchain
        toolchain_dir = os.path.join(
            self.test_dir, 'xtensa/2020.07/xtensa-dc233c-elf/bin')
        os.makedirs(toolchain_dir)
        tools.write_file(os.path.join(toolchain_dir, 'xtensa-dc233c-elf-gcc'),
                         b'fake')

        args = argparse.Namespace(dry_run=False, force=False)
        with terminal.capture() as (out, _):
            res = setup.setup_xtensa(self.test_dir, args)
        self.assertEqual(0, res)
        self.assertIn('already present', out.getvalue())

    def test_setup_gcc_parse(self):
        """Test parsing packages from a sample RST"""
        rst_text = """\
Some intro text

.. code-block:: bash

    sudo apt-get install gcc gcc-aarch64-linux-gnu

More text

.. code-block:: bash

    sudo apt-get install bc bison build-essential \\
      device-tree-compiler flex
"""
        packages = setup.parse_deb_packages(rst_text)
        self.assertEqual(
            ['gcc', 'gcc-aarch64-linux-gnu', 'bc', 'bison',
             'build-essential', 'device-tree-compiler', 'flex'],
            packages)

    def test_setup_gcc_dry_run(self):
        """Test dry-run shows missing packages"""
        rst_text = """\
.. code-block:: bash

    sudo apt-get install gcc gcc-aarch64-linux-gnu
"""
        rst_dir = os.path.join(self.test_dir, 'doc', 'build')
        os.makedirs(rst_dir)
        tools.write_file(os.path.join(rst_dir, 'gcc.rst'),
                         rst_text.encode('utf-8'))

        def mock_output(*cmd):
            if 'gcc-aarch64-linux-gnu' in cmd:
                result = command.CommandResult(return_code=1, stdout='',
                                                stderr='', exception=None)
                raise command.CommandExc('Package not found', result)

        args = argparse.Namespace(dry_run=True, force=False)
        with mock.patch('uman_pkg.setup.util.get_uboot_dir',
                        return_value=self.test_dir):
            with mock.patch('uman_pkg.setup.command.output', mock_output):
                with terminal.capture() as (out, _):
                    res = setup.setup_gcc(args)
        self.assertEqual(0, res)
        output = out.getvalue()
        self.assertIn('Missing gcc packages', output)
        self.assertIn('gcc-aarch64-linux-gnu', output)
        self.assertIn('Would run:', output)
        self.assertNotIn('gcc ', output.split('Missing')[0])

    def test_setup_gcc_all_installed(self):
        """Test setup_gcc when all packages are installed"""
        rst_text = """\
.. code-block:: bash

    sudo apt-get install gcc
"""
        rst_dir = os.path.join(self.test_dir, 'doc', 'build')
        os.makedirs(rst_dir)
        tools.write_file(os.path.join(rst_dir, 'gcc.rst'),
                         rst_text.encode('utf-8'))

        args = argparse.Namespace(dry_run=False, force=False)
        with mock.patch('uman_pkg.setup.util.get_uboot_dir',
                        return_value=self.test_dir):
            with mock.patch('uman_pkg.setup.command.output'):
                with terminal.capture() as (out, _):
                    res = setup.setup_gcc(args)
        self.assertEqual(0, res)
        self.assertIn('All gcc packages are installed', out.getvalue())

    def test_setup_gcc_no_uboot(self):
        """Test setup_gcc when U-Boot dir not found"""
        args = argparse.Namespace(dry_run=False, force=False)
        with mock.patch('uman_pkg.setup.util.get_uboot_dir',
                        return_value=None):
            with terminal.capture() as (_, err):
                res = setup.setup_gcc(args)
        self.assertEqual(1, res)
        self.assertIn('Cannot find U-Boot', err.getvalue())

    def test_setup_components_dict(self):
        """Test that SETUP_COMPONENTS has expected entries"""
        self.assertIn('gcc', setup.SETUP_COMPONENTS)
        self.assertIn('qemu', setup.SETUP_COMPONENTS)
        self.assertIn('opensbi', setup.SETUP_COMPONENTS)
        self.assertIn('remote', setup.SETUP_COMPONENTS)
        self.assertIn('tfa', setup.SETUP_COMPONENTS)
        self.assertIn('xtensa', setup.SETUP_COMPONENTS)

    def test_qemu_packages_dict(self):
        """Test that QEMU_PACKAGES has expected entries"""
        self.assertIn('qemu-system-arm', setup.QEMU_PACKAGES)
        self.assertIn('qemu-system-misc', setup.QEMU_PACKAGES)
        self.assertIn('qemu-system-ppc', setup.QEMU_PACKAGES)
        self.assertIn('qemu-system-x86', setup.QEMU_PACKAGES)

    def test_setup_aliases_dry_run(self):
        """Test setup_aliases in dry-run mode"""
        args = argparse.Namespace(dry_run=True, force=False, alias_dir=None)
        with terminal.capture() as (out, _):
            res = setup.setup_aliases(args)
        self.assertEqual(0, res)
        self.assertIn('Would create symlinks', out.getvalue())

    def test_setup_aliases_creates_symlinks(self):
        """Test setup_aliases creates symlinks in specified directory"""
        alias_dir = os.path.join(self.test_dir, 'aliases')
        args = argparse.Namespace(dry_run=False, force=False,
                                  alias_dir=alias_dir)
        with terminal.capture() as (out, _):
            res = setup.setup_aliases(args)
        self.assertEqual(0, res)
        self.assertIn('Created symlinks', out.getvalue())
        # Check symlinks were created
        for name in ['rf', 'rc', 'rd']:
            link_path = os.path.join(alias_dir, name)
            self.assertTrue(os.path.islink(link_path))

    def test_setup_aliases_skips_existing(self):
        """Test setup_aliases skips existing files"""
        alias_dir = os.path.join(self.test_dir, 'aliases')
        os.makedirs(alias_dir)
        # Create an existing file
        tools.write_file(os.path.join(alias_dir, 'rf'), b'existing')

        args = argparse.Namespace(dry_run=False, force=False,
                                  alias_dir=alias_dir)
        with terminal.capture() as (out, _):
            res = setup.setup_aliases(args)
        self.assertEqual(0, res)
        self.assertIn('Skipped', out.getvalue())
        self.assertIn('rf', out.getvalue())

    def test_setup_aliases_shell_hint(self):
        """Test setup_aliases shows shell function and git aliases hint"""
        alias_dir = os.path.join(self.test_dir, 'aliases')
        args = argparse.Namespace(dry_run=False, force=False,
                                  alias_dir=alias_dir)
        with terminal.capture() as (out, _):
            res = setup.setup_aliases(args)
        self.assertEqual(0, res)
        output = out.getvalue()
        self.assertIn(setup.UM_FUNC, output)
        self.assertIn('eval "$(um git -a)"', output)
        self.assertIn('.uman_env', output)

    def test_setup_aliases_shell_hint_dry_run(self):
        """Test setup_aliases shows shell hint in dry-run mode too"""
        args = argparse.Namespace(dry_run=True, force=False, alias_dir=None)
        with terminal.capture() as (out, _):
            res = setup.setup_aliases(args)
        self.assertEqual(0, res)
        output = out.getvalue()
        self.assertIn(setup.UM_FUNC, output)
        self.assertIn('eval "$(um git -a)"', output)
        self.assertIn('.uman_env', output)

    def test_setup_aliases_quiet(self):
        """Test setup_aliases suppresses notice output in quiet mode"""
        alias_dir = os.path.join(self.test_dir, 'aliases')
        args = argparse.Namespace(dry_run=False, force=False,
                                  alias_dir=alias_dir)
        tout.init(tout.WARNING)
        with terminal.capture() as (out, _):
            res = setup.setup_aliases(args)
        tout.init(tout.NOTICE)
        self.assertEqual(0, res)
        self.assertFalse(out.getvalue())

    def test_setup_remote_parsing(self):
        """Test that setup remote subcommand is parsed correctly"""
        args = cmdline.parse_args(['setup', 'remote', 'myhost'])
        self.assertEqual('setup', args.cmd)
        self.assertEqual('remote', args.component)
        self.assertEqual('myhost', args.host)

    def test_setup_remote_no_host(self):
        """Test setup remote with no hostname"""
        args = argparse.Namespace(
            cmd='setup', component='remote', host=None,
            list_components=False, force=False, dry_run=False,
            verbose=False, debug=False, alias_dir=None)
        with terminal.capture() as (_, err):
            res = setup.do_setup(args)
        self.assertEqual(1, res)
        self.assertIn('Hostname required', err.getvalue())

    def test_setup_remote_dry_run(self):
        """Test setup remote in dry-run mode"""
        args = argparse.Namespace(
            cmd='setup', component='remote', host='testhost',
            list_components=False, force=False, dry_run=True,
            verbose=False, debug=False, alias_dir=None)
        with terminal.capture() as (out, _):
            res = setup.do_setup(args)
        self.assertEqual(0, res)
        output = out.getvalue()
        self.assertIn('rsync', output)
        self.assertIn('testhost', output)
        self.assertIn('ln -sf', output)
        self.assertIn('setup aliases', output)


class TestMain(TestBase):
    """Tests for __main__.py"""

    @classmethod
    def setUpClass(cls):
        """Get the uman package directory"""
        cls.uman_dir = os.path.dirname(os.path.dirname(__file__))

    def test_embedded_pylib(self):
        """Test uman works without UBOOT_TOOLS using embedded u_boot_pylib"""
        env = os.environ.copy()
        env['UBOOT_TOOLS'] = '/nonexistent/path'
        env.pop('PYTHONPATH', None)
        env.pop('UMAN_EXTERNAL_PYLIB', None)
        result = subprocess.run(
            ['python3', '-m', 'uman_pkg', '--help'],
            capture_output=True, env=env, cwd=self.uman_dir, check=False)
        self.assertEqual(0, result.returncode)


class TestUtil(TestBase):
    """Tests for util module"""

    def setUp(self):
        super().setUp()
        tout.init(tout.NOTICE)

    def test_run_pytest_success(self):
        """Test run_pytest returns True on success"""
        moc = command.CommandResult(return_code=0)

        with mock.patch.object(util, 'get_uboot_dir', return_value='/uboot'):
            with mock.patch('os.chdir'):
                with mock.patch.object(util, 'exec_cmd',
                                       return_value=moc) as run:
                    result = util.run_pytest('test_foo')

        self.assertTrue(result)
        run.assert_called_once()
        cmd = run.call_args[0][0]
        self.assertEqual('./test/py/test.py', cmd[0])
        self.assertIn('--buildman', cmd)
        self.assertIn('-k', cmd)
        self.assertEqual('test_foo', cmd[cmd.index('-k') + 1])

    def test_run_pytest_failure(self):
        """Test run_pytest returns False on failure"""
        moc = command.CommandResult(return_code=1, stderr='Test failed')

        with mock.patch.object(util, 'get_uboot_dir', return_value='/uboot'):
            with mock.patch('os.chdir'):
                with mock.patch.object(util, 'exec_cmd', return_value=moc):
                    with terminal.capture():
                        result = util.run_pytest('test_foo')

        self.assertFalse(result)

    def test_run_pytest_no_uboot_dir(self):
        """Test run_pytest returns False when not in U-Boot tree"""
        with mock.patch.object(util, 'get_uboot_dir', return_value=None):
            with terminal.capture():
                result = util.run_pytest('test_foo')

        self.assertFalse(result)

    def test_run_pytest_dry_run(self):
        """Test run_pytest in dry-run mode"""
        with mock.patch.object(util, 'get_uboot_dir', return_value='/uboot'):
            with mock.patch('os.chdir'):
                with terminal.capture() as (out, _):
                    result = util.run_pytest('test_foo', dry_run=True)

        self.assertTrue(result)
        self.assertIn('test.py', out.getvalue())


class TestTestSubcommand(TestBase):  # pylint: disable=R0904
    """Tests for the test subcommand"""

    # C source with linker-list symbols matching U-Boot's unit test format
    TEST_ELF_SOURCE = """
/* Linker-list symbols: _u_boot_list_2_ut_<suite>_2_<test> */
char _u_boot_list_2_ut_dm_2_test_acpi __attribute__((used));
char _u_boot_list_2_ut_dm_2_test_gpio __attribute__((used));
char _u_boot_list_2_ut_env_2_test_env_basic __attribute__((used));
/* Suite end markers */
char suite_end_dm __attribute__((used));
char suite_end_env __attribute__((used));
int main(void) { return 0; }
"""

    def setUp(self):
        super().setUp()
        tout.init(tout.NOTICE)
        # Compile a test ELF with linker-list symbols
        self.test_elf = os.path.join(self.test_dir, 'test_elf')
        src_file = os.path.join(self.test_dir, 'test.c')
        tools.write_file(src_file, self.TEST_ELF_SOURCE.encode())
        command.run('gcc', '-o', self.test_elf, src_file)

    def test_test_subcommand_parsing(self):
        """Test that test subcommand is parsed correctly"""
        parser = cmdline.setup_parser()

        # Test basic test command
        args = parser.parse_args(['test'])
        self.assertEqual('test', args.cmd)
        self.assertEqual([], args.tests)
        self.assertFalse(args.list_tests)
        self.assertFalse(args.list_suites)
        self.assertFalse(args.flattree_too)
        self.assertFalse(args.test_verbose)

        # Test with test names
        args = parser.parse_args(['test', 'dm', 'env'])
        self.assertEqual(['dm', 'env'], args.tests)

        # Test with -l flag
        args = parser.parse_args(['test', '-l'])
        self.assertTrue(args.list_tests)

        # Test with -s flag
        args = parser.parse_args(['test', '-s'])
        self.assertTrue(args.list_suites)

        # Test with --flattree-too flag
        args = parser.parse_args(['test', '--flattree-too'])
        self.assertTrue(args.flattree_too)

        # Test with -V flag
        args = parser.parse_args(['test', '-V'])
        self.assertTrue(args.test_verbose)

        # Test with -r flag
        args = parser.parse_args(['test', '-r'])
        self.assertTrue(args.results)

    def test_test_alias(self):
        """Test that 't' alias works for test"""
        args = cmdline.parse_args(['t'])
        self.assertEqual('test', args.cmd)

        args = cmdline.parse_args(['t', 'dm'])
        self.assertEqual('test', args.cmd)
        self.assertEqual(['dm'], args.tests)

    def test_get_sandbox_path_exists(self):
        """Test get_sandbox_path when sandbox exists"""
        sandbox_path = os.path.join(self.test_dir, 'sandbox', 'u-boot')
        os.makedirs(os.path.dirname(sandbox_path))
        tools.write_file(sandbox_path, b'fake executable')
        with mock.patch.object(settings, 'get', return_value=self.test_dir):
            self.assertEqual(sandbox_path, cmdtest.get_sandbox_path())

    def test_get_sandbox_path_not_exists(self):
        """Test get_sandbox_path when sandbox does not exist"""
        with mock.patch.object(settings, 'get', return_value=self.test_dir):
            self.assertIsNone(cmdtest.get_sandbox_path())

    def test_get_suites_from_nm(self):
        """Test that suites are extracted from nm output"""
        suites = cmdtest.get_suites_from_nm(self.test_elf)
        self.assertEqual(['dm', 'env'], suites)

    def test_get_tests_from_nm_all(self):
        """Test that all tests are extracted from nm output"""
        tests = cmdtest.get_tests_from_nm(self.test_elf)
        self.assertEqual([
            ('dm', 'test_acpi'),
            ('dm', 'test_gpio'),
            ('env', 'test_env_basic'),
        ], tests)

    def test_get_tests_from_nm_filtered(self):
        """Test that tests can be filtered by suite"""
        tests = cmdtest.get_tests_from_nm(self.test_elf, suite='dm')
        self.assertEqual([('dm', 'test_acpi'), ('dm', 'test_gpio')], tests)

        tests = cmdtest.get_tests_from_nm(self.test_elf, suite='env')
        self.assertEqual([('env', 'test_env_basic')], tests)

    def test_do_test_no_sandbox(self):
        """Test do_test fails gracefully when sandbox not found"""
        args = cmdline.parse_args(['test'])
        with mock.patch.object(cmdtest, 'get_sandbox_path', return_value=None):
            with terminal.capture() as out:
                result = cmdtest.do_test(args)
        self.assertEqual(1, result)
        self.assertIn('Sandbox not found', out[1].getvalue())

    def test_do_test_list_suites(self):
        """Test -s flag lists all test suites"""
        args = cmdline.parse_args(['test', '-s'])
        with mock.patch.object(cmdtest, 'get_sandbox_path',
                               return_value=self.test_elf):
            with terminal.capture() as out:
                result = cmdtest.do_test(args)
        self.assertEqual(0, result)
        stdout = out[0].getvalue()
        self.assertIn('dm', stdout)
        self.assertIn('env', stdout)

    def test_do_test_list_tests(self):
        """Test -l flag lists all tests"""
        args = cmdline.parse_args(['test', '-l'])
        with mock.patch.object(cmdtest, 'get_sandbox_path',
                               return_value=self.test_elf):
            with terminal.capture() as out:
                result = cmdtest.do_test(args)
        self.assertEqual(0, result)
        stdout = out[0].getvalue()
        self.assertIn('dm.test_acpi', stdout)
        self.assertIn('dm.test_gpio', stdout)
        self.assertIn('env.test_env_basic', stdout)

    def test_do_test_list_tests_by_suite(self):
        """Test -l with suite name filters tests"""
        args = cmdline.parse_args(['test', '-l', 'dm'])
        with mock.patch.object(cmdtest, 'get_sandbox_path',
                               return_value=self.test_elf):
            with terminal.capture() as out:
                result = cmdtest.do_test(args)
        self.assertEqual(0, result)
        stdout = out[0].getvalue()
        self.assertIn('dm.test_acpi', stdout)
        self.assertIn('dm.test_gpio', stdout)
        self.assertNotIn('env.test_env_basic', stdout)

    @mock.patch.object(cmdtest, 'has_emit_result', return_value=True)
    @mock.patch.object(cmdtest, 'has_no_flat', return_value=True)
    def test_build_ut_cmd_no_tests(self, *_):
        """Test build_ut_cmd with all specs"""
        cmd = cmdtest.build_ut_cmd('/sb', [('all', None)])
        self.assertEqual(['/sb', '-T', '-F', '-c', 'ut -E all'], cmd)

    @mock.patch.object(cmdtest, 'has_emit_result', return_value=True)
    def test_build_ut_cmd_full(self, _):
        """Test build_ut_cmd with full flag (both tree types)"""
        cmd = cmdtest.build_ut_cmd('/sb', [('dm', None)], full=True)
        self.assertEqual(['/sb', '-T', '-c', 'ut -E dm'], cmd)

    @mock.patch.object(cmdtest, 'has_emit_result', return_value=True)
    @mock.patch.object(cmdtest, 'has_no_flat', return_value=True)
    def test_build_ut_cmd_verbose(self, *_):
        """Test build_ut_cmd with verbose flag"""
        cmd = cmdtest.build_ut_cmd('/sb', [('dm', None)], verbose=True)
        self.assertEqual(['/sb', '-T', '-F', '-v', '-c', 'ut -E dm'], cmd)

    @mock.patch.object(cmdtest, 'has_emit_result', return_value=True)
    def test_build_ut_cmd_all_flags(self, _):
        """Test build_ut_cmd with all flags"""
        cmd = cmdtest.build_ut_cmd('/sb', [('dm', None)],
                                   full=True, verbose=True)
        self.assertEqual(['/sb', '-T', '-v', '-c', 'ut -E dm'], cmd)

    @mock.patch.object(cmdtest, 'has_emit_result', return_value=True)
    @mock.patch.object(cmdtest, 'has_no_flat', return_value=True)
    def test_build_ut_cmd_suite(self, *_):
        """Test build_ut_cmd with suite name"""
        cmd = cmdtest.build_ut_cmd('/sb', [('dm', None)])
        self.assertEqual(['/sb', '-T', '-F', '-c', 'ut -E dm'], cmd)

    @mock.patch.object(cmdtest, 'has_emit_result', return_value=True)
    @mock.patch.object(cmdtest, 'has_no_flat', return_value=True)
    def test_build_ut_cmd_specific_test(self, *_):
        """Test build_ut_cmd with specific test (suite.test)"""
        cmd = cmdtest.build_ut_cmd('/sb', [('dm', 'test_one')])
        self.assertEqual(['/sb', '-T', '-F', '-c', 'ut -E dm test_one'], cmd)

    @mock.patch.object(cmdtest, 'has_emit_result', return_value=True)
    @mock.patch.object(cmdtest, 'has_no_flat', return_value=True)
    def test_build_ut_cmd_multiple_tests(self, *_):
        """Test build_ut_cmd with multiple test specifications"""
        cmd = cmdtest.build_ut_cmd('/sb', [('dm', None), ('env', None)])
        self.assertEqual(['/sb', '-T', '-F', '-c', 'ut -E dm; ut -E env'], cmd)

    @mock.patch.object(cmdtest, 'has_no_flat', return_value=True)
    def test_build_ut_cmd_legacy(self, _):
        """Test build_ut_cmd with legacy flag omits -E"""
        cmd = cmdtest.build_ut_cmd('/sb', [('dm', None)], legacy=True)
        self.assertEqual(['/sb', '-T', '-F', '-c', 'ut dm'], cmd)

    @mock.patch.object(cmdtest, 'has_emit_result', return_value=True)
    @mock.patch.object(cmdtest, 'has_no_flat', return_value=True)
    def test_build_ut_cmd_manual(self, *_):
        """Test build_ut_cmd with manual flag"""
        cmd = cmdtest.build_ut_cmd('/sb', [('dm', None)], manual=True)
        self.assertEqual(['/sb', '-T', '-F', '-c', 'ut -E -m dm'], cmd)

    @mock.patch.object(cmdtest, 'has_emit_result', return_value=True)
    @mock.patch.object(cmdtest, 'has_no_flat', return_value=False)
    def test_build_ut_cmd_no_flat_unsupported(self, *_):
        """Test build_ut_cmd omits -F when source tree lacks support"""
        cmd = cmdtest.build_ut_cmd('/sb', [('dm', None)])
        self.assertEqual(['/sb', '-T', '-c', 'ut -E dm'], cmd)

    @mock.patch.object(cmdtest, 'has_emit_result', return_value=False)
    def test_build_ut_cmd_no_emit_unsupported(self, _):
        """Test build_ut_cmd omits -E when source tree lacks support"""
        cmd = cmdtest.build_ut_cmd('/sb', [('dm', None)])
        self.assertEqual(['/sb', '-T', '-c', 'ut dm'], cmd)

    @mock.patch.object(cmdtest, 'has_emit_result', return_value=True)
    @mock.patch.object(cmdtest, 'has_no_flat', return_value=True)
    def test_build_ut_cmd_malloc_dump(self, *_):
        """Test build_ut_cmd expands %d in malloc_dump filename"""
        cmd = cmdtest.build_ut_cmd('/sb', [('dm', None)],
                                   malloc_dump='/tmp/dump-%d.txt')
        self.assertEqual(
            ['/sb', '-T', '--malloc_dump', '/tmp/dump-0.txt',
             '-F', '-c', 'ut -E dm'], cmd)

    def test_run_tests_basic(self):
        """Test run_tests executes sandbox correctly"""
        cap = []

        def mock_run(*cmd_args, **_kwargs):
            cap.append(cmd_args)
            return command.CommandResult(return_code=0,
                                         stdout='Result: PASS dm_test\n')

        args = cmdline.parse_args(['test', 'dm'])
        col = terminal.Color()
        with mock.patch.object(cmdtest, 'has_emit_result', return_value=True):
            with mock.patch.object(cmdtest, 'has_no_flat', return_value=True):
                with mock.patch.object(command, 'run_one', mock_run):
                    with mock.patch.object(cmdtest, 'ensure_dm_init_files',
                                           return_value=True):
                        with terminal.capture():
                            result = cmdtest.run_tests(
                                '/sb', [('dm', None)], args, col)
        self.assertEqual(0, result)
        self.assertEqual(('/sb', '-T', '-F', '-c', 'ut -E dm'), cap[0])

    def test_run_tests_full(self):
        """Test run_tests with --flattree-too flag (both tree types)"""
        cap = []

        def mock_run(*cmd_args, **_kwargs):
            cap.append(cmd_args)
            return command.CommandResult(return_code=0,
                                         stdout='Result: PASS dm_test\n')

        args = cmdline.parse_args(['test', '--flattree-too', 'dm'])
        col = terminal.Color()
        with mock.patch.object(cmdtest, 'has_emit_result', return_value=True):
            with mock.patch.object(command, 'run_one', mock_run):
                with mock.patch.object(cmdtest, 'ensure_dm_init_files',
                                       return_value=True):
                    with terminal.capture():
                        result = cmdtest.run_tests(
                            '/sb', [('dm', None)], args, col)
        self.assertEqual(0, result)
        self.assertEqual(('/sb', '-T', '-c', 'ut -E dm'), cap[0])

    def test_run_tests_verbose(self):
        """Test run_tests with verbose flag"""
        cap = []

        def mock_run(*cmd_args, **_kwargs):
            cap.append(cmd_args)
            return command.CommandResult(return_code=0,
                                         stdout='Result: PASS dm_test\n')

        args = cmdline.parse_args(['test', '-V', 'dm'])
        col = terminal.Color()
        with mock.patch.object(cmdtest, 'has_emit_result', return_value=True):
            with mock.patch.object(cmdtest, 'has_no_flat', return_value=True):
                with mock.patch.object(command, 'run_one', mock_run):
                    with mock.patch.object(cmdtest, 'ensure_dm_init_files',
                                           return_value=True):
                        with terminal.capture():
                            result = cmdtest.run_tests(
                                '/sb', [('dm', None)], args, col)
        self.assertEqual(0, result)
        self.assertEqual(('/sb', '-T', '-F', '-v', '-c', 'ut -E dm'), cap[0])

    def test_parse_legacy_results_all_pass(self):
        """Test parse_legacy_results with all passing tests"""
        output = '''
Test: dm_test_first ... ok
Test: dm_test_second ... ok
Test: dm_test_third ... ok
'''
        res = cmdtest.parse_legacy_results(output)
        self.assertEqual(3, res.passed)
        self.assertEqual(0, res.failed)
        self.assertEqual(0, res.skipped)

    def test_parse_legacy_results_mixed(self):
        """Test parse_legacy_results with mixed results"""
        output = '''
Test: dm_test_first ... ok
Test: dm_test_second ... FAILED
Test: dm_test_third ... SKIPPED
Test: dm_test_fourth ... ok
'''
        res = cmdtest.parse_legacy_results(output)
        self.assertEqual(2, res.passed)
        self.assertEqual(1, res.failed)
        self.assertEqual(1, res.skipped)

    def test_parse_results_legacy(self):
        """Test parse_legacy_results with old-style Test: lines"""
        output = '''
Test: dm_test_first ... ok
Test: dm_test_second ... FAILED
Test: dm_test_third ... SKIPPED
'''
        res = cmdtest.parse_legacy_results(output)
        self.assertEqual(1, res.passed)
        self.assertEqual(1, res.failed)
        self.assertEqual(1, res.skipped)

        # parse_results returns None for old-style output
        self.assertIsNone(cmdtest.parse_results(output))

    def test_parse_results_only_result_lines(self):
        """Test parse_results ignores Test: lines (only Result: lines)"""
        output = '''
Test: dm_test_first ... ok
Result: PASS dm_test_second
Test: dm_test_third ... FAILED
Result: SKIP dm_test_fourth
'''
        res = cmdtest.parse_results(output)
        self.assertEqual(1, res.passed)
        self.assertEqual(0, res.failed)
        self.assertEqual(1, res.skipped)

    def test_parse_summary_basic(self):
        """Test parse_summary with standard output"""
        output = 'Tests run: 540, 11595 ms, average: 21 ms, failures: 7'
        res = cmdtest.parse_summary(output)
        self.assertEqual(533, res.passed)
        self.assertEqual(7, res.failed)
        self.assertEqual(0, res.skipped)

    def test_parse_summary_no_failures(self):
        """Test parse_summary with no failures"""
        output = 'Tests run: 1, 4 ms, average: 4 ms, failures: 0'
        res = cmdtest.parse_summary(output)
        self.assertEqual(1, res.passed)
        self.assertEqual(0, res.failed)

    def test_parse_summary_empty(self):
        """Test parse_summary with no summary line"""
        self.assertIsNone(cmdtest.parse_summary(''))

    def test_parse_results_empty(self):
        """Test parse_results with empty output returns None"""
        self.assertIsNone(cmdtest.parse_results(''))
        self.assertIsNone(cmdtest.parse_legacy_results(''))

    def test_parse_results_named_tuple(self):
        """Test parse_results returns TestCounts named tuple"""
        output = 'Result: PASS test1\nResult: FAIL test2\n'
        res = cmdtest.parse_results(output)
        self.assertIsInstance(res, cmdtest.TestCounts)
        self.assertEqual(1, res.passed)
        self.assertEqual(1, res.failed)
        self.assertEqual(0, res.skipped)

    def test_parse_results_leak_detail(self):
        """Test parse_results counts real leak-detail lines"""
        # Real leak detail: <addr> <size-hex> <caller>
        output = (
            'Test: my_test\n'
            'Leak: 1 alloc\n'
            '  169f5668  1000a0  sandbox_mmc_probe:194\n'
            'Result: PASS my_test\n'
        )
        res = cmdtest.parse_results(output)
        self.assertEqual(1, res.leaked)
        self.assertEqual(0x1000a0, res.leak_bytes)

    def test_parse_results_leak_detail_false_match(self):
        """Test parse_results ignores non-leak lines that look hex-ish"""
        # Lines from rsvmem, GPT partition dumps, host info -- these
        # previously matched the regex and inflated the leak total
        output = (
            'Test: fdt_rsvmem\n'
            '      0    0000000000074656    0000000000000009\n'
            '      1    0000000000001234    0000000000005678\n'
            '165  fd 42 d1 6a 20 30 27 98\n'
            '  3x 24.2K  1  Basic data\n'
            '  2x 32.3K  512 test2  /tmp/b/x/2MB.ext2.img\n'
            'Result: PASS fdt_rsvmem\n'
        )
        res = cmdtest.parse_results(output)
        self.assertEqual(0, res.leaked)
        self.assertEqual(0, res.leak_bytes)

    def test_parse_legacy_results_show_results(self):
        """Test parse_legacy_results with show_results flag"""
        output = '''
Test: dm_test_first ... ok
Test: dm_test_second ... FAILED
Test: dm_test_third ... SKIPPED
'''
        col = terminal.Color()
        with terminal.capture() as (out, _):
            res = cmdtest.parse_legacy_results(output, show_results=True,
                                               col=col)
        self.assertEqual(1, res.passed)
        self.assertEqual(1, res.failed)
        self.assertEqual(1, res.skipped)
        stdout = out.getvalue()
        self.assertIn('PASS: dm_test_first', stdout)
        self.assertIn('FAIL: dm_test_second', stdout)
        self.assertIn('SKIP: dm_test_third', stdout)

    def test_format_duration_seconds(self):
        """Test format_duration with seconds only"""
        self.assertEqual('0.00s', util.format_duration(0))
        self.assertEqual('1.50s', util.format_duration(1.5))
        self.assertEqual('59.99s', util.format_duration(59.99))

    def test_format_duration_minutes(self):
        """Test format_duration with minutes"""
        self.assertEqual('1m 0.0s', util.format_duration(60))
        self.assertEqual('1m 30.0s', util.format_duration(90))
        self.assertEqual('5m 30.5s', util.format_duration(330.5))

    def test_run_tests_shows_summary(self):
        """Test run_tests shows results summary"""
        output = '''
Result: PASS dm_test_first
Result: PASS dm_test_second
'''

        def mock_run(*_args, **_kwargs):
            return command.CommandResult(return_code=0, stdout=output)

        args = cmdline.parse_args(['test', 'dm'])
        col = terminal.Color()
        with mock.patch.object(command, 'run_one', mock_run):
            with mock.patch.object(cmdtest, 'ensure_dm_init_files',
                                   return_value=True):
                with terminal.capture() as (out, err):
                    result = cmdtest.run_tests('/path/to/sandbox',
                                               [('dm', None)], args, col)
        self.assertEqual(0, result)
        self.assertFalse(err.getvalue())
        stdout = out.getvalue()
        self.assertIn('2 passed', stdout)
        # Zero counts are omitted from the summary
        self.assertNotIn('failed', stdout)
        self.assertNotIn('skipped', stdout)

    def test_run_tests_shows_output_when_no_results(self):
        """Test run_tests shows output when no results detected"""
        output = '''
U-Boot banner here
Missing required argument 'fs_image' for test 'pxe_test_sysboot'
'''

        def mock_run(*_args, **_kwargs):
            return command.CommandResult(return_code=1, stdout=output)

        args = cmdline.parse_args(['test', 'pxe'])
        col = terminal.Color()
        with mock.patch.object(command, 'run_one', mock_run):
            with mock.patch.object(cmdtest, 'ensure_dm_init_files',
                                   return_value=True):
                with terminal.capture() as (out, err):
                    result = cmdtest.run_tests('/path/to/sandbox',
                                               [('pxe', None)], args, col)
        self.assertEqual(1, result)
        self.assertIn('No results detected', err.getvalue())
        # Error message should be shown in output
        self.assertIn('Missing required argument', out.getvalue())

    def test_run_tests_parses_summary(self):
        """Test run_tests uses summary line when -E is unavailable"""
        output = 'Tests run: 10, 100 ms, average: 10 ms, failures: 2'

        def mock_run(*_args, **_kwargs):
            return command.CommandResult(return_code=1, stdout=output)

        args = cmdline.parse_args(['test', 'dm'])
        col = terminal.Color()
        with mock.patch.object(cmdtest, 'has_emit_result', return_value=False):
            with mock.patch.object(command, 'run_one', mock_run):
                with mock.patch.object(cmdtest, 'ensure_dm_init_files',
                                       return_value=True):
                    with terminal.capture() as (out, err):
                        result = cmdtest.run_tests(
                            '/sb', [('dm', None)], args, col)
        self.assertEqual(1, result)
        self.assertIn('8 passed', out.getvalue())
        self.assertIn('2 failed', out.getvalue())

    def test_run_tests_detects_segfault(self):
        """Test run_tests detects segfault (SIGSEGV) and resets terminal"""
        col = terminal.Color()
        system_calls = []

        def mock_run(*_args, **_kwargs):
            return command.CommandResult(return_code=-11, stdout='Test: foo\n')

        def mock_system(cmd):
            system_calls.append(cmd)
            return 0

        args = cmdline.parse_args(['test', 'dm'])
        with mock.patch.object(command, 'run_one', mock_run):
            with mock.patch.object(cmdtest, 'ensure_dm_init_files',
                                   return_value=True):
                with mock.patch('os.system', mock_system):
                    with terminal.capture() as (out, err):
                        result = cmdtest.run_tests('/path/to/sandbox',
                                                   [('dm', None)], args, col)
        self.assertEqual(-11, result)
        self.assertIn('crashed', err.getvalue())
        self.assertIn('SIGSEGV', err.getvalue())
        self.assertEqual(['tset'], system_calls)

    def test_run_tests_detects_crash_128_plus(self):
        """Test run_tests detects crash with 128+signal return code"""
        col = terminal.Color()

        def mock_run(*_args, **_kwargs):
            return command.CommandResult(return_code=139, stdout='Test: foo\n')

        args = cmdline.parse_args(['test', 'dm'])
        with mock.patch.object(command, 'run_one', mock_run):
            with mock.patch.object(cmdtest, 'ensure_dm_init_files',
                                   return_value=True):
                with terminal.capture() as (out, err):
                    result = cmdtest.run_tests('/path/to/sandbox',
                                               [('dm', None)], args, col)
        self.assertEqual(139, result)
        self.assertIn('crashed', err.getvalue())
        self.assertIn('SIGSEGV', err.getvalue())

    def test_do_test_runs_tests(self):
        """Test do_test runs tests when no list flags"""
        cap = []

        def mock_run(*cmd_args, **_kwargs):
            cap.append(cmd_args)
            return command.CommandResult(return_code=0,
                                         stdout='Result: PASS dm_test\n')

        def mock_resolve(_sandbox, specs):
            return specs, []

        args = cmdline.parse_args(['test', 'dm'])
        args.col = terminal.Color()
        with mock.patch.object(cmdtest, 'has_emit_result', return_value=True):
            with mock.patch.object(cmdtest, 'has_no_flat', return_value=True):
                with mock.patch.object(cmdtest, 'get_sandbox_path',
                                       return_value='/sb'):
                    with mock.patch.object(cmdtest, 'resolve_specs',
                                           mock_resolve):
                        with mock.patch.object(cmdtest, 'validate_specs',
                                               return_value=[]):
                            with mock.patch.object(
                                    cmdtest, 'ensure_dm_init_files',
                                    return_value=True):
                                with mock.patch.object(command, 'run_one',
                                                       mock_run):
                                    with terminal.capture():
                                        result = cmdtest.do_test(args)
        self.assertEqual(0, result)
        self.assertEqual(('/sb', '-T', '-F', '-c', 'ut -E dm'), cap[0])

    def test_count_tests_suite(self):
        """Test count_tests counts tests in a suite"""
        tests = [('dm', 'test_a'), ('dm', 'test_b')]
        with mock.patch.object(cmdtest, 'get_tests_from_nm',
                               return_value=tests) as mock_nm:
            result = cmdtest.count_tests('/sb', [('dm', None)])
        self.assertEqual(2, result)
        mock_nm.assert_called_once_with('/sb', 'dm')

    def test_count_tests_all(self):
        """Test count_tests counts all tests"""
        tests = [('dm', 'a'), ('dm', 'b'), ('env', 'c')]
        with mock.patch.object(cmdtest, 'get_tests_from_nm',
                               return_value=tests):
            result = cmdtest.count_tests('/sb', [('all', None)])
        self.assertEqual(3, result)

    def test_count_tests_pattern(self):
        """Test count_tests counts tests matching a pattern"""
        tests = [('dm', 'gpio'), ('dm', 'gpio_irq'), ('dm', 'i2c')]
        with mock.patch.object(cmdtest, 'get_tests_from_nm',
                               return_value=tests):
            result = cmdtest.count_tests('/sb', [('dm', 'gpio')])
        self.assertEqual(1, result)

    def test_progress_with_total(self):
        """Test Progress shows total in output"""
        prog = cmdtest.Progress(emit_result=True, total=10)
        self.assertEqual(10, prog.total)

    def test_progress_with_emit(self):
        """Test Progress counts Result: lines with -E"""
        prog = cmdtest.Progress(emit_result=True)
        prog.update(None, b'Result: PASS test1\n')
        prog.update(None, b'Result: FAIL test2\n')
        prog.update(None, b'Result: SKIP test3\n')
        prog.update(None, b'Result: PASS test4\n')
        prog.finish()
        self.assertEqual(2, prog.passed)
        self.assertEqual(1, prog.failed)
        self.assertEqual(1, prog.skipped)

    def test_progress_without_emit(self):
        """Test Progress counts Test: and failure lines without -E"""
        prog = cmdtest.Progress(emit_result=False)
        prog.update(None, b'Test: acpi: acpi.c\n')
        prog.update(None, b'Test: gpio: gpio.c\n')
        prog.update(None,
                     b"Test 'gpio' failed 1 times\nTest: host: host.c\n")
        prog.finish()
        self.assertEqual(2, prog.passed)
        self.assertEqual(1, prog.failed)
        self.assertEqual(0, prog.skipped)

    def test_progress_partial_lines(self):
        """Test Progress handles data split across chunks"""
        prog = cmdtest.Progress(emit_result=True)
        prog.update(None, b'Result: PA')
        prog.update(None, b'SS test1\nResult:')
        prog.update(None, b' FAIL test2\n')
        prog.finish()
        self.assertEqual(1, prog.passed)
        self.assertEqual(1, prog.failed)

    def test_progress_no_output(self):
        """Test Progress with no test output"""
        prog = cmdtest.Progress(emit_result=True)
        prog.finish()
        self.assertEqual(0, prog.passed)
        self.assertEqual(0, prog.failed)
        self.assertEqual(0, prog.skipped)

    def test_progress_last_test_passes(self):
        """Test Progress counts the last test as passed at finish"""
        prog = cmdtest.Progress(emit_result=False)
        prog.update(None, b'Test: foo: foo.c\n')
        self.assertTrue(prog.pending)
        prog.finish()
        self.assertEqual(1, prog.passed)
        self.assertFalse(prog.pending)

    def test_parse_one_test_suite(self):
        """Test parse_one_test with suite name"""
        self.assertEqual(('dm', None), cmdtest.parse_one_test('dm'))

    def test_parse_one_test_suite_dot_test(self):
        """Test parse_one_test with suite.test format"""
        self.assertEqual(('dm', 'test_acpi'),
                         cmdtest.parse_one_test('dm.test_acpi'))

    def test_parse_one_test_full_name(self):
        """Test parse_one_test with full test name (suite_test_name)"""
        self.assertEqual(('dm', 'acpi'), cmdtest.parse_one_test('dm_test_acpi'))

    def test_parse_one_test_test_prefix(self):
        """Test parse_one_test with test_ prefix"""
        self.assertEqual((None, 'something'),
                         cmdtest.parse_one_test('test_something'))

    def test_parse_one_test_pytest_name(self):
        """Test parse_one_test with pytest-style ut_ prefix"""
        self.assertEqual(('bootstd', 'bootflow_cmd_menu'),
                         cmdtest.parse_one_test('ut_bootstd_bootflow_cmd_menu'))

    def test_parse_test_specs_empty(self):
        """Test parse_test_specs with no tests"""
        self.assertEqual([('all', None)], cmdtest.parse_test_specs([]))

    def test_parse_test_specs_all(self):
        """Test parse_test_specs with 'all'"""
        self.assertEqual([('all', None)], cmdtest.parse_test_specs(['all']))

    def test_parse_test_specs_single(self):
        """Test parse_test_specs with single suite"""
        self.assertEqual([('dm', None)], cmdtest.parse_test_specs(['dm']))

    def test_parse_test_specs_pattern(self):
        """Test parse_test_specs with suite and glob pattern"""
        self.assertEqual([('dm', 'video*')],
                         cmdtest.parse_test_specs(['dm', 'video*']))

    def test_parse_test_specs_multiple(self):
        """Test parse_test_specs with multiple suites"""
        self.assertEqual([('dm', None), ('env', None)],
                         cmdtest.parse_test_specs(['dm', 'env']))

    def test_resolve_specs_with_suite(self):
        """Test resolve_specs passes through specs with known suite"""
        all_tests = [('dm', 'test_acpi'), ('env', 'test_basic')]
        specs = [('dm', None), ('env', 'basic')]

        with mock.patch.object(cmdtest, 'get_tests_from_nm',
                               return_value=all_tests):
            resolved, unmatched = cmdtest.resolve_specs(
                '/path/to/sandbox', specs)

        self.assertEqual(specs, resolved)
        self.assertEqual([], unmatched)

    def test_resolve_specs_unknown_suite_finds_test(self):
        """Test resolve_specs finds test when parsed suite doesn't exist"""
        # 'editenv_test_base' parses as suite='editenv', pattern='base'
        # but 'editenv' isn't a suite - the test is in 'bootstd'
        all_tests = [('bootstd', 'editenv_test_base'),
                     ('bootstd', 'cedit_base'),
                     ('dm', 'test_acpi')]

        with mock.patch.object(cmdtest, 'get_tests_from_nm',
                               return_value=all_tests):
            resolved, unmatched = cmdtest.resolve_specs(
                '/path/to/sandbox', [('editenv', 'base')])

        self.assertEqual([('bootstd', 'editenv_test_base')], resolved)
        self.assertEqual([], unmatched)

    def test_resolve_specs_finds_suite(self):
        """Test resolve_specs finds suite for pattern-only spec"""
        all_tests = [('dm', 'test_acpi'), ('dm', 'test_gpio'),
                     ('env', 'test_env_basic')]

        with mock.patch.object(cmdtest, 'get_tests_from_nm',
                               return_value=all_tests):
            resolved, unmatched = cmdtest.resolve_specs(
                '/path/to/sandbox', [(None, 'acpi')])

        self.assertEqual([('dm', 'acpi')], resolved)
        self.assertEqual([], unmatched)

    def test_resolve_specs_unmatched(self):
        """Test resolve_specs returns unmatched for unknown pattern"""
        all_tests = [('dm', 'test_acpi')]

        with mock.patch.object(cmdtest, 'get_tests_from_nm',
                               return_value=all_tests):
            resolved, unmatched = cmdtest.resolve_specs(
                '/path/to/sandbox', [(None, 'nonexistent')])

        self.assertEqual([], resolved)
        self.assertEqual([(None, 'nonexistent')], unmatched)

    def test_resolve_specs_partial_across_suites(self):
        """Test partial-name matching returns actual tests, not glob

        Bug: 'efi' previously generated patterns like 'bootstd_test_efi*'
        which excluded tests that contain 'efi' but don't start with the
        prefix (e.g. 'bootstd_test_bootflow_efi'). Resolve should return
        the actual matching test names instead.
        """
        all_tests = [
            ('bootstd', 'bootstd_test_bootflow_efi'),
            ('bootstd', 'bootstd_test_other'),
            ('common', 'common_test_efidebug'),
            ('efi_unicoll', 'efi_unicoll_test_a'),
            ('efi_unicoll', 'efi_unicoll_test_b'),
            ('dm', 'test_acpi'),
        ]

        with mock.patch.object(cmdtest, 'get_tests_from_nm',
                               return_value=all_tests):
            resolved, unmatched = cmdtest.resolve_specs(
                '/path/to/sandbox', [('efi', None)])

        # All four tests with 'efi' in their name (or in their suite name)
        # should be returned with their full test names
        expected = [
            ('bootstd', 'bootstd_test_bootflow_efi'),
            ('common', 'common_test_efidebug'),
            ('efi_unicoll', 'efi_unicoll_test_a'),
            ('efi_unicoll', 'efi_unicoll_test_b'),
        ]
        self.assertEqual(expected, resolved)
        self.assertEqual([], unmatched)

    def test_validate_specs_all(self):
        """Test validate_specs accepts 'all' without checking"""
        result = cmdtest.validate_specs('/path/to/sandbox', [('all', None)])
        self.assertEqual([], result)

    def test_validate_specs_valid_suite(self):
        """Test validate_specs accepts valid suite"""
        all_tests = [('dm', 'test_acpi'), ('dm', 'test_gpio')]

        with mock.patch.object(cmdtest, 'get_tests_from_nm',
                               return_value=all_tests):
            result = cmdtest.validate_specs('/path/to/sandbox',
                                            [('dm', None)])

        self.assertEqual([], result)

    def test_validate_specs_valid_pattern(self):
        """Test validate_specs accepts valid suite with pattern"""
        all_tests = [('dm', 'test_acpi'), ('dm', 'test_gpio')]

        with mock.patch.object(cmdtest, 'get_tests_from_nm',
                               return_value=all_tests):
            result = cmdtest.validate_specs('/path/to/sandbox',
                                            [('dm', 'acpi')])

        self.assertEqual([], result)

    def test_validate_specs_invalid_suite(self):
        """Test validate_specs returns unmatched for invalid suite"""
        all_tests = [('dm', 'test_acpi')]

        with mock.patch.object(cmdtest, 'get_tests_from_nm',
                               return_value=all_tests):
            result = cmdtest.validate_specs('/path/to/sandbox',
                                            [('nonexistent', None)])

        self.assertEqual([('nonexistent', None)], result)

    def test_validate_specs_invalid_pattern(self):
        """Test validate_specs returns unmatched for invalid pattern"""
        all_tests = [('dm', 'test_acpi')]

        with mock.patch.object(cmdtest, 'get_tests_from_nm',
                               return_value=all_tests):
            result = cmdtest.validate_specs('/path/to/sandbox',
                                            [('dm', 'nonexistent')])

        self.assertEqual([('dm', 'nonexistent')], result)

    def test_get_section_info(self):
        """Test parsing readelf output for section info"""
        readelf_output = '''
Section Headers:
  [Nr] Name              Type             Address           Offset
       Size              EntSize          Flags  Link  Info  Align
  [ 0]                   NULL             0000000000000000  00000000
       0000000000000000  0000000000000000           0     0     0
  [21] .data.rel.ro      PROGBITS         0000000001234000  00034000
       0000000000010000  0000000000000000  WA       0     0     32
'''
        result = mock.MagicMock(stdout=readelf_output)
        with mock.patch.object(command, 'run_one', return_value=result):
            addr, offset = cmdtest.get_section_info('/path/to/sandbox')
        self.assertEqual(0x01234000, addr)
        self.assertEqual(0x00034000, offset)

    def test_get_section_info_not_found(self):
        """Test get_section_info when section is missing"""
        result = mock.MagicMock(stdout='No .data.rel.ro section')
        with mock.patch.object(command, 'run_one', return_value=result):
            addr, offset = cmdtest.get_section_info('/path/to/sandbox')
        self.assertIsNone(addr)
        self.assertIsNone(offset)

    def test_predict_test_count_live_tree(self):
        """Test predict_test_count for live tree (default)"""
        flags_data = [
            ('test_a', 0),                      # No flags - runs once
            ('test_b', cmdtest.UTF_DM),         # Runs once (no flat)
            ('test_c', cmdtest.UTF_FLAT_TREE),  # Skipped on live tree
        ]
        patcher = mock.patch.object(cmdtest, 'get_test_flags',
                                    return_value=flags_data)
        with patcher:
            count = cmdtest.predict_test_count('/path/to/sandbox', 'dm')
        self.assertEqual(2, count)

    def test_predict_test_count_full(self):
        """Test predict_test_count with full=True (both tree types)"""
        flags_data = [
            ('test_a', 0),                      # No flags - runs once
            ('test_b', cmdtest.UTF_DM),         # Runs twice
            ('test_c', cmdtest.UTF_FLAT_TREE),  # Runs once (flat only)
            ('test_d', cmdtest.UTF_LIVE_TREE),  # Runs once
            ('video_test', cmdtest.UTF_DM),     # Runs once (skip flat)
        ]
        patcher = mock.patch.object(cmdtest, 'get_test_flags',
                                    return_value=flags_data)
        with patcher:
            count = cmdtest.predict_test_count('/path/to/sandbox', 'dm',
                                               full=True)
        # test_a(1) + test_b(2) + test_c(1) + test_d(1) + video(1) = 6
        self.assertEqual(6, count)

    def test_needs_dm_init_dm_suite(self):
        """Test needs_dm_init returns True for dm suite"""
        self.assertTrue(cmdtest.needs_dm_init([('dm', None)]))

    def test_needs_dm_init_all_suite(self):
        """Test needs_dm_init returns True for all tests"""
        self.assertTrue(cmdtest.needs_dm_init([('all', None)]))

    def test_needs_dm_init_other_suite(self):
        """Test needs_dm_init returns False for non-dm suite"""
        self.assertFalse(cmdtest.needs_dm_init([('env', None)]))

    def test_needs_dm_init_host_test(self):
        """Test needs_dm_init returns True for host tests"""
        self.assertTrue(cmdtest.needs_dm_init([('cmd', 'cmd_host')]))

    def test_ensure_dm_init_files_exists(self):
        """Test ensure_dm_init_files returns True if files exist"""
        with mock.patch.object(os.path, 'exists', return_value=True):
            result = cmdtest.ensure_dm_init_files()
        self.assertTrue(result)

    def test_ensure_dm_init_files_pytest_fails(self):
        """Test ensure_dm_init_files fails if run_pytest fails"""
        with mock.patch.object(os.path, 'exists', return_value=False):
            with mock.patch.object(cmdtest, 'run_pytest', return_value=False):
                with terminal.capture():
                    result = cmdtest.ensure_dm_init_files()
        self.assertFalse(result)

    def test_run_tests_gdb(self):
        """Test run_tests with -g launches gdb-multiarch"""
        cap = []

        def mock_run(cmd, **kwargs):
            cap.append((cmd, kwargs))
            return mock.MagicMock(returncode=0)

        args = cmdline.parse_args(['test', '-g', 'dm'])
        col = terminal.Color()
        with mock.patch.object(cmdtest, 'has_emit_result',
                               return_value=True):
            with mock.patch.object(cmdtest, 'has_no_flat',
                                   return_value=True):
                with mock.patch.object(cmdtest, 'ensure_dm_init_files',
                                       return_value=True):
                    with mock.patch('subprocess.run', mock_run):
                        with terminal.capture():
                            result = cmdtest.run_tests(
                                '/sb', [('dm', None)], args, col)
        self.assertEqual(0, result)
        cmd = cap[0][0]
        self.assertEqual('gdb-multiarch', cmd[0])
        self.assertIn('/sb', cmd)
        self.assertIn('run -T -F -c \'ut -E dm\'', cmd[-1])

    def test_run_tests_gdb_dry_run(self):
        """Test run_tests with -g and -n shows gdb command"""
        args = cmdline.parse_args(['-n', 'test', '-g', 'dm'])
        col = terminal.Color()
        with mock.patch.object(cmdtest, 'has_emit_result',
                               return_value=True):
            with mock.patch.object(cmdtest, 'has_no_flat',
                                   return_value=True):
                with mock.patch.object(cmdtest, 'ensure_dm_init_files',
                                       return_value=True):
                    with terminal.capture() as (out, err):
                        result = cmdtest.run_tests(
                            '/sb', [('dm', None)], args, col)
        self.assertEqual(0, result)
        output = out.getvalue()
        self.assertIn('gdb-multiarch', output)
        self.assertIn('/sb', output)

    def test_run_tests_gdb_bt(self):
        """Test run_tests with --bt adds backtrace and quit commands"""
        cap = []

        def mock_run(cmd, **kwargs):
            cap.append((cmd, kwargs))
            return mock.MagicMock(returncode=0)

        args = cmdline.parse_args(['test', '--bt', 'dm'])
        col = terminal.Color()
        with mock.patch.object(cmdtest, 'has_emit_result',
                               return_value=True):
            with mock.patch.object(cmdtest, 'has_no_flat',
                                   return_value=True):
                with mock.patch.object(cmdtest, 'ensure_dm_init_files',
                                       return_value=True):
                    with mock.patch('subprocess.run', mock_run):
                        with terminal.capture():
                            result = cmdtest.run_tests(
                                '/sb', [('dm', None)], args, col)
        self.assertEqual(0, result)
        cmd = cap[0][0]
        self.assertEqual('gdb-multiarch', cmd[0])
        self.assertEqual(['-ex', 'bt', '-ex', 'quit'], cmd[-4:])

    def test_run_tests_gdb_cmd(self):
        """Test run_tests with --gdb-cmd adds extra gdb commands"""
        cap = []

        def mock_run(cmd, **kwargs):
            cap.append((cmd, kwargs))
            return mock.MagicMock(returncode=0)

        args = cmdline.parse_args(['test', '--gdb-cmd', 'info reg',
                                   '--gdb-cmd', 'bt', 'dm'])
        col = terminal.Color()
        with mock.patch.object(cmdtest, 'has_emit_result',
                               return_value=True):
            with mock.patch.object(cmdtest, 'has_no_flat',
                                   return_value=True):
                with mock.patch.object(cmdtest, 'ensure_dm_init_files',
                                       return_value=True):
                    with mock.patch('subprocess.run', mock_run):
                        with terminal.capture():
                            result = cmdtest.run_tests(
                                '/sb', [('dm', None)], args, col)
        self.assertEqual(0, result)
        cmd = cap[0][0]
        self.assertEqual('gdb-multiarch', cmd[0])
        self.assertEqual(['-ex', 'info reg', '-ex', 'bt'], cmd[-4:])


class TestPytestCTest(TestBase):
    """Tests for the pytest -C (C test) functionality"""

    def setUp(self):
        tout.init(tout.WARNING)
        self.test_dir = tempfile.mkdtemp()
        self.orig_config = settings.SETTINGS['config']
        settings.SETTINGS['config'] = None

    def tearDown(self):
        shutil.rmtree(self.test_dir)
        settings.SETTINGS['config'] = self.orig_config
        super().tearDown()

    def test_find_test(self):
        """Test finding Python test file from spec"""
        # Create a mock U-Boot tree with test file
        test_fs_dir = os.path.join(self.test_dir, 'test/py/tests/test_fs')
        os.makedirs(test_fs_dir)
        test_file = os.path.join(test_fs_dir, 'test_ext4l.py')
        tools.write_file(test_file, b'# test file')

        # Test with class:method format
        path, cls, method = cmdpy.find_test(self.test_dir,
                                            'TestExt4l:test_unlink')
        self.assertEqual(test_file, path)
        self.assertEqual('TestExt4l', cls)
        self.assertEqual('test_unlink', method)

        # Test with just class name
        path, cls, method = cmdpy.find_test(self.test_dir, 'ext4l')
        self.assertEqual(test_file, path)
        self.assertIsNone(method)

        # Test not found
        path, cls, method = cmdpy.find_test(self.test_dir,
                                            'NonExistent:test_foo')
        self.assertIsNone(path)
        self.assertIsNone(cls)
        self.assertIsNone(method)

    def test_find_test_camel_case(self):
        """Test finding test file with CamelCase class name"""
        # Create test file with snake_case name
        test_dir = os.path.join(self.test_dir, 'test/py/tests')
        os.makedirs(test_dir, exist_ok=True)
        test_file = os.path.join(test_dir, 'test_pxe_parser.py')
        tools.write_file(test_file, b'# test file')

        # Test CamelCase class name maps to snake_case file
        path, cls, method = cmdpy.find_test(self.test_dir,
                                            'TestPxeParser:test_pxe_ipappend')
        self.assertEqual(test_file, path)
        self.assertEqual('TestPxeParser', cls)
        self.assertEqual('test_pxe_ipappend', method)

    def test_camel_to_snake(self):
        """Test CamelCase to snake_case conversion"""
        self.assertEqual('pxe_parser', cmdpy.camel_to_snake('PxeParser'))
        self.assertEqual('ext4l', cmdpy.camel_to_snake('Ext4l'))
        self.assertEqual('my_long_name', cmdpy.camel_to_snake('MyLongName'))
        self.assertEqual('simple', cmdpy.camel_to_snake('Simple'))

    def test_get_fixture_paths(self):
        """Test extracting fixture paths from a test file"""
        test_content = b'''
@pytest.fixture
def ext4_image(self, u_boot_config):
    image_path = os.path.join(u_boot_config.persistent_data_dir,
                              'ext4l_test.img')
    yield image_path
'''
        test_file = os.path.join(self.test_dir, 'test_ext4l.py')
        tools.write_file(test_file, test_content)

        kwargs = [('fs_image', 'ext4_image')]
        fixtures = ['ext4_image']
        with mock.patch.object(settings, 'get', return_value='/tmp/b'):
            paths, reason = cmdpy.get_fixture_paths(test_file, kwargs, fixtures)
        self.assertEqual('/tmp/b/sandbox/persistent-data/ext4l_test.img',
                         paths['fs_image'])
        self.assertIsNone(reason)

    def test_get_fixture_paths_fshelper(self):
        """Test get_fixture_paths handles FsHelper pattern"""
        test_content = b'''
from fs_helper import FsHelper

@pytest.fixture
def pxe_image(u_boot_config):
    fsh = FsHelper(u_boot_config, 'vfat', 4, prefix='pxe_test')
    cfg_path = create_extlinux_conf(fsh.srcdir, labels)
    return fsh.path, cfg_path
'''
        test_file = os.path.join(self.test_dir, 'test_pxe.py')
        tools.write_file(test_file, test_content)

        kwargs = [('fs_image', 'fs_img'), ('cfg_path', 'cfg_path')]
        fixtures = ['pxe_image']
        with mock.patch.object(settings, 'get', return_value='/tmp/b'):
            paths, reason = cmdpy.get_fixture_paths(test_file, kwargs, fixtures)
        self.assertEqual('/tmp/b/sandbox/persistent-data/pxe_test.vfat.img',
                         paths['fs_image'])
        self.assertEqual('/extlinux/extlinux.conf', paths['cfg_path'])
        self.assertIsNone(reason)

    def test_find_run_ut_call(self):
        """Test finding run_ut() call in method AST"""
        source = '''
class TestFoo:
    def test_with_ut(self, ubman):
        ubman.run_ut('dm', 'dm_test_foo')

    def test_without_ut(self):
        pass
'''
        tree = ast.parse(source)
        cls = tree.body[0]

        # Method with run_ut() call
        method = cls.body[0]
        call = cmdpy.find_run_ut_call(method)
        self.assertIsNotNone(call)
        self.assertEqual('run_ut', call.func.attr)

        # Method without run_ut() call
        method = cls.body[1]
        call = cmdpy.find_run_ut_call(method)
        self.assertIsNone(call)

    def test_parse_c_test_call(self):
        """Test parsing a C test-command from a real test file"""
        uboot_dir = cmdpy.get_uboot_dir()
        if not uboot_dir:
            self.skipTest('Not in a U-Boot tree')

        test_file = os.path.join(uboot_dir,
                                 'test/py/tests/test_fs/test_ext4l.py')
        source = tools.read_file(test_file, binary=False)

        info = cmdpy.parse_c_test_call(source, 'TestExt4l', 'test_probe')
        self.assertIsNotNone(info.suite)
        self.assertEqual('fs', info.suite)
        self.assertEqual('fs_test_ext4l_probe_norun', info.c_test)
        self.assertEqual([('fs_image', 'ext4_image')], info.kwargs)
        self.assertEqual(['ext4_image'], info.fixtures)

    def test_c_test_flag_parsing(self):
        """Test -C flag is parsed correctly"""
        args = cmdline.parse_args(['pytest', '-C', 'TestExt4l:test_unlink'])
        self.assertTrue(args.c_test)
        self.assertEqual(['TestExt4l:test_unlink'], args.test_spec)

    @mock.patch.object(cmdpy, 'get_uboot_dir')
    def test_run_c_test_no_spec(self, mock_uboot_dir):
        """Test run_c_test fails without test spec"""
        mock_uboot_dir.return_value = self.test_dir
        args = argparse.Namespace(test_spec=None, dry_run=False,
                                  show_cmd=False, show_output=False,
                                  build=False, lto=False)
        with terminal.capture() as (_out, err):
            ret = cmdpy.run_c_test(args)
        self.assertEqual(1, ret)
        self.assertIn('Test spec required', err.getvalue())

    @mock.patch.object(cmdpy, 'get_uboot_dir')
    @mock.patch.object(cmdpy, 'get_sandbox_path')
    def test_run_c_test_method_required(self, mock_sandbox, mock_uboot_dir):
        """Test run_c_test fails without method name"""
        mock_uboot_dir.return_value = self.test_dir
        mock_sandbox.return_value = '/path/to/sandbox'

        # Create test file without method
        test_fs_dir = os.path.join(self.test_dir, 'test/py/tests/test_fs')
        os.makedirs(test_fs_dir)
        test_file = os.path.join(test_fs_dir, 'test_ext4l.py')
        tools.write_file(test_file, b'# test')

        args = argparse.Namespace(test_spec=['ext4l'], dry_run=False,
                                  show_cmd=False, show_output=False,
                                  build=False, lto=False)
        with terminal.capture() as (_out, err):
            ret = cmdpy.run_c_test(args)
        self.assertEqual(1, ret)
        self.assertIn('Method name required', err.getvalue())

    @mock.patch.object(cmdpy, 'get_uboot_dir')
    @mock.patch.object(cmdpy, 'get_sandbox_path')
    @mock.patch.object(cmdpy, 'get_fixture_paths')
    @mock.patch.object(cmdpy, 'exec_cmd')
    def test_run_c_test_show_output(self, mock_exec, mock_fixture,
                                    mock_sandbox, mock_uboot_dir):
        """Test run_c_test with -s flag shows output live"""
        mock_uboot_dir.return_value = self.test_dir
        mock_sandbox.return_value = '/path/to/sandbox'

        # Create test file with run_ut call
        test_fs_dir = os.path.join(self.test_dir, 'test/py/tests/test_fs')
        os.makedirs(test_fs_dir)
        test_file = os.path.join(test_fs_dir, 'test_ext4l.py')
        test_content = '''
class TestExt4l:
    def test_unlink(self):
        ubman.run_ut('ext4l', 'fs_test_ext4l_unlink', fs_image=ext4_image)
'''
        tools.write_file(test_file, test_content.encode())

        # Mock fixture paths to an existing file
        fixture_path = os.path.join(self.test_dir, 'ext4l.img')
        tools.write_file(fixture_path, b'')
        mock_fixture.return_value = ({'fs_image': fixture_path}, None)

        mock_exec.return_value = command.CommandResult(return_code=0)

        args = argparse.Namespace(test_spec=['TestExt4l:test_unlink'],
                                  dry_run=False, show_cmd=False,
                                  show_output=True, build=False, lto=False)
        ret = cmdpy.run_c_test(args)
        self.assertEqual(0, ret)

        # Check exec_cmd was called with capture=False
        mock_exec.assert_called_once()
        call_kwargs = mock_exec.call_args[1]
        self.assertFalse(call_kwargs['capture'])

    @mock.patch.object(cmdpy, 'get_uboot_dir')
    @mock.patch.object(cmdpy, 'get_sandbox_path')
    @mock.patch.object(cmdpy, 'get_fixture_paths')
    @mock.patch.object(cmdpy, 'exec_cmd')
    def test_run_c_test_summary(self, mock_exec, mock_fixture,
                                mock_sandbox, mock_uboot_dir):
        """Test run_c_test shows summary and only shows output on failure"""
        mock_uboot_dir.return_value = self.test_dir
        mock_sandbox.return_value = '/path/to/sandbox'

        # Create test file with run_ut call
        test_fs_dir = os.path.join(self.test_dir, 'test/py/tests/test_fs')
        os.makedirs(test_fs_dir)
        test_file = os.path.join(test_fs_dir, 'test_ext4l.py')
        test_content = '''
class TestExt4l:
    def test_unlink(self):
        ubman.run_ut('ext4l', 'fs_test_ext4l_unlink', fs_image=ext4_image)
'''
        tools.write_file(test_file, test_content.encode())

        # Mock fixture paths to an existing file
        fixture_path = os.path.join(self.test_dir, 'ext4l.img')
        tools.write_file(fixture_path, b'')
        mock_fixture.return_value = ({'fs_image': fixture_path}, None)

        # Test with PASS result - no output shown
        mock_exec.return_value = command.CommandResult(
            return_code=0, stdout='Result: PASS: test\nTest output\n')

        args = argparse.Namespace(test_spec=['TestExt4l:test_unlink'],
                                  dry_run=False, show_cmd=False,
                                  show_output=False, build=False, lto=False)
        with terminal.capture() as (out, _err):
            ret = cmdpy.run_c_test(args)
        self.assertEqual(0, ret)
        output = out.getvalue()
        self.assertIn('1 passed', output)
        # Zero counts are omitted from the summary
        self.assertNotIn('failed', output)
        self.assertNotIn('Test output', output)

        # Test with FAIL result - output shown
        mock_exec.return_value = command.CommandResult(
            return_code=1, stdout='Result: FAIL: test\nTest output\n')

        with terminal.capture() as (out, _err):
            ret = cmdpy.run_c_test(args)
        self.assertEqual(1, ret)
        output = out.getvalue()
        self.assertIn('1 failed', output)
        self.assertIn('Test output', output)


class TestPytestPollute(TestBase):
    """Tests for the pytest --pollute functionality"""

    def setUp(self):
        super().setUp()
        self.orig_dir = os.getcwd()
        tout.init(tout.WARNING)

    def tearDown(self):
        os.chdir(self.orig_dir)
        super().tearDown()

    def test_pollute_flag_parsing(self):
        """Test --pollute flag is parsed correctly"""
        args = cmdline.parse_args(['pytest', '-B', 'sandbox',
                                   '--pollute', 'test_foo'])
        self.assertEqual('test_foo', args.pollute)
        self.assertEqual('sandbox', args.board)

    def test_collect_tests_parsing(self):
        """Test parsing of --collect-only output"""
        collect_output = '''test_ut.py::TestUt::test_dm
test_ut.py::TestUt::test_env
test_fs.py::TestFs::test_ext4
<Module test_other.py>
'''
        with mock.patch.object(command, 'run_pipe') as mock_run:
            mock_run.return_value = mock.Mock(
                return_code=0,
                stdout=collect_output,
                stderr='')
            args = argparse.Namespace(board='sandbox', build_dir=None,
                                      test_spec=None, build=False, flattree_too=False, output_dir=None)
            tests = cmdpy.collect_tests(args)

        self.assertEqual(3, len(tests))
        self.assertEqual('test_ut.py::TestUt::test_dm', tests[0])
        self.assertEqual('test_ut.py::TestUt::test_env', tests[1])
        self.assertEqual('test_fs.py::TestFs::test_ext4', tests[2])

    def test_pollute_run_uses_k_with_names(self):
        """Test pollute_run uses -k with extracted test names"""
        captured_cmd = []

        def mock_popen(cmd, **_kwargs):
            captured_cmd.extend(cmd)
            proc = mock.Mock()
            proc.stdout.read.return_value = b''  # pylint: disable=no-member
            proc.returncode = 0
            return proc

        with mock.patch('subprocess.Popen', mock_popen):
            args = argparse.Namespace(board='sandbox', build_dir=None,
                                      lto=False, test_spec=None,
                                      flattree_too=False, output_dir=None)
            env = {}
            tests = ['tests/test_ut.py::test_ut[ut_dm_foo]',
                     'tests/test_ut.py::test_ut[ut_dm_bar]']
            target = 'tests/test_ut.py::test_ut[ut_dm_target]'
            cmdpy.pollute_run(tests, target, args, env)

        # Uses -k with extracted test names
        self.assertIn('-k', captured_cmd)
        idx = captured_cmd.index('-k')
        spec = captured_cmd[idx + 1]
        self.assertEqual('(ut_dm_foo or ut_dm_bar or ut_dm_target)', spec)

    def test_node_to_name(self):
        """Test node_to_name extracts test name from node ID"""
        # Parameterized test
        self.assertEqual('ut_dm_foo',
            cmdpy.node_to_name('tests/test_ut.py::test_ut[ut_dm_foo]'))
        # Non-parameterized test
        self.assertEqual('test_bar',
            cmdpy.node_to_name('tests/test_foo.py::TestClass::test_bar'))
        # Simple name
        self.assertEqual('test_simple', cmdpy.node_to_name('test_simple'))

    def test_pollute_run_uses_pollute_build_dir(self):
        """Test pollute_run uses -pollute suffix for build dir"""
        captured_cmd = []

        def mock_popen(cmd, **_kwargs):
            captured_cmd.extend(cmd)
            proc = mock.Mock()
            proc.stdout.read.return_value = b''  # pylint: disable=no-member
            proc.returncode = 0
            return proc

        with mock.patch('subprocess.Popen', mock_popen):
            with mock.patch.object(settings, 'get', return_value='/tmp/b'):
                args = argparse.Namespace(board='sandbox', build_dir=None,
                                          lto=False, test_spec=None,
                                          flattree_too=False, output_dir=None)
                cmdpy.pollute_run([], 'test_target', args, {})

        self.assertIn('--build-dir', captured_cmd)
        idx = captured_cmd.index('--build-dir')
        self.assertEqual('/tmp/b/sandbox-pollute', captured_cmd[idx + 1])

    def test_collect_tests_no_full_flag(self):
        """Test collect_tests adds --no-full when full=False"""
        with mock.patch.object(command, 'run_pipe') as mock_run:
            mock_run.return_value = mock.Mock(
                return_code=0, stdout='', stderr='')
            args = argparse.Namespace(board='sandbox', build_dir=None,
                                      test_spec=None, build=False, flattree_too=False, output_dir=None)
            with mock.patch('uman_pkg.cmdpy.has_no_full', return_value=True):
                cmdpy.collect_tests(args)

        cmd = mock_run.call_args[0][0][0]
        self.assertIn('--no-full', cmd)

    def test_collect_tests_full_flag(self):
        """Test collect_tests omits --no-full when flattree_too=True"""
        with mock.patch.object(command, 'run_pipe') as mock_run:
            mock_run.return_value = mock.Mock(
                return_code=0, stdout='', stderr='')
            args = argparse.Namespace(board='sandbox', build_dir=None,
                                      test_spec=None, build=False,
                                      flattree_too=True, output_dir=None)
            cmdpy.collect_tests(args)

        cmd = mock_run.call_args[0][0][0]
        self.assertNotIn('--no-full', cmd)

    def test_collect_tests_no_full_unsupported(self):
        """Test collect_tests omits --no-full when tree lacks support"""
        with mock.patch.object(command, 'run_pipe') as mock_run:
            mock_run.return_value = mock.Mock(
                return_code=0, stdout='', stderr='')
            args = argparse.Namespace(board='sandbox', build_dir=None,
                                      test_spec=None, build=False,
                                      flattree_too=False, output_dir=None)
            with mock.patch('uman_pkg.cmdpy.has_no_full', return_value=False):
                cmdpy.collect_tests(args)

        cmd = mock_run.call_args[0][0][0]
        self.assertNotIn('--no-full', cmd)

    def test_pollute_run_no_full_flag(self):
        """Test pollute_run adds --no-full when full=False"""
        captured_cmd = []

        def mock_popen(cmd, **_kwargs):
            captured_cmd.extend(cmd)
            proc = mock.Mock()
            proc.stdout.read.return_value = b''  # pylint: disable=no-member
            proc.returncode = 0
            return proc

        with mock.patch('subprocess.Popen', mock_popen):
            with mock.patch('uman_pkg.cmdpy.has_no_full', return_value=True):
                args = argparse.Namespace(board='sandbox', build_dir=None,
                                          lto=False, test_spec=None,
                                          flattree_too=False, output_dir=None)
                cmdpy.pollute_run([], 'test_target', args, {})

        self.assertIn('--no-full', captured_cmd)

    def test_pollute_run_full_flag(self):
        """Test pollute_run omits --no-full when flattree_too=True"""
        captured_cmd = []

        def mock_popen(cmd, **_kwargs):
            captured_cmd.extend(cmd)
            proc = mock.Mock()
            proc.stdout.read.return_value = b''  # pylint: disable=no-member
            proc.returncode = 0
            return proc

        with mock.patch('subprocess.Popen', mock_popen):
            args = argparse.Namespace(board='sandbox', build_dir=None,
                                      lto=False, test_spec=None,
                                      flattree_too=True, output_dir=None)
            cmdpy.pollute_run([], 'test_target', args, {})

        self.assertNotIn('--no-full', captured_cmd)

    def test_pollute_build_to_pollute_dir(self):
        """Test --pollute -b builds to pollute directory"""
        cap = []

        def mock_exec_cmd(cmd, *_args, **_kwargs):
            cap.append(cmd)
            return command.CommandResult(return_code=0)

        def mock_collect(**_kwargs):
            return command.CommandResult(
                stdout='test_ut.py::TestUt::test_dm_foo\n'
                       'test_ut.py::TestUt::test_dm_bar\n',
                return_code=0)

        command.TEST_RESULT = mock_collect

        args = make_args(cmd='pytest', board='sandbox', build=True,
                         pollute='test_dm_foo')
        with mock.patch.object(build, 'setup_uboot_dir', return_value=True):
            with mock.patch.object(cmdpy, 'get_uboot_dir',
                                   return_value=self.test_dir):
                with mock.patch.object(cmdpy, 'exec_cmd', mock_exec_cmd):
                    with terminal.capture():
                        control.run_command(args)

        # First command should be buildman to pollute directory
        self.assertIn('buildman', cap[0])
        self.assertIn('-o', cap[0])
        idx = cap[0].index('-o')
        self.assertIn('-pollute', cap[0][idx + 1])

    def test_pollute_build_respects_lto(self):
        """Test --pollute -b -L respects LTO flag"""
        cap = []

        def mock_exec_cmd(cmd, *_args, **_kwargs):
            cap.append(cmd)
            return command.CommandResult(return_code=0)

        def mock_collect(**_kwargs):
            return command.CommandResult(
                stdout='test_ut.py::TestUt::test_dm_foo\n',
                return_code=0)

        command.TEST_RESULT = mock_collect

        # With lto=True, -L should NOT be in buildman command
        args = make_args(cmd='pytest', board='sandbox', build=True,
                         lto=True, pollute='test_dm_foo')
        with mock.patch.object(build, 'setup_uboot_dir', return_value=True):
            with mock.patch.object(cmdpy, 'get_uboot_dir',
                                   return_value=self.test_dir):
                with mock.patch.object(cmdpy, 'exec_cmd', mock_exec_cmd):
                    with terminal.capture():
                        control.run_command(args)

        self.assertNotIn('-L', cap[0])


class TestPytestHooks(TestBase):
    """Tests for pytest hooks hostname handling"""

    def setUp(self):
        super().setUp()
        tout.init(tout.WARNING)
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir)
        super().tearDown()

    @mock.patch('uman_pkg.cmdpy.socket')
    def test_ensure_hooks_host(self, mock_socket):
        """Test ensure_hooks_host creates symlink for unknown hostname"""
        mock_socket.gethostname.return_value = 'container-xyz'

        hooks_bin = os.path.join(self.test_dir, 'bin')
        travis_dir = os.path.join(hooks_bin, 'travis-ci')
        os.makedirs(travis_dir)

        with terminal.capture() as (out, err):
            cmdpy.ensure_hooks_host(hooks_bin)

        link = os.path.join(hooks_bin, 'container-xyz')
        self.assertTrue(os.path.islink(link))
        self.assertEqual('travis-ci', os.readlink(link))
        self.assertFalse(err.getvalue())

    @mock.patch('uman_pkg.cmdpy.socket')
    def test_ensure_hooks_host_exists(self, mock_socket):
        """Test ensure_hooks_host does nothing when hostname dir exists"""
        mock_socket.gethostname.return_value = 'myhost'

        hooks_bin = os.path.join(self.test_dir, 'bin')
        host_dir = os.path.join(hooks_bin, 'myhost')
        os.makedirs(host_dir)

        with terminal.capture() as (out, err):
            cmdpy.ensure_hooks_host(hooks_bin)

        self.assertFalse(os.path.islink(host_dir))
        self.assertTrue(os.path.isdir(host_dir))
        self.assertFalse(out.getvalue())
        self.assertFalse(err.getvalue())

    @mock.patch('uman_pkg.cmdpy.settings')
    @mock.patch('uman_pkg.cmdpy.socket')
    def test_get_board_config_fallback(self, mock_socket, mock_settings):
        """Test get_board_config falls back to travis-ci"""
        mock_socket.gethostname.return_value = 'container-xyz'

        hooks_dir = os.path.join(self.test_dir, 'hooks')
        travis_dir = os.path.join(hooks_dir, 'bin', 'travis-ci')
        os.makedirs(travis_dir)

        config_content = b'console_impl=qemu\nqemu_binary=qemu-system-arm\n'
        tools.write_file(
            os.path.join(travis_dir, 'conf.testboard_na'),
            config_content)

        mock_settings.get.side_effect = lambda key, fallback=None: {
            'test_hooks': hooks_dir,
        }.get(key, fallback)

        result = cmdpy.get_board_config('testboard')

        self.assertEqual('qemu', result['console_impl'])
        self.assertEqual('qemu-system-arm', result['qemu_binary'])


class TestPytestId(TestBase):
    """Tests for --id flag and PYTHONPATH setup"""

    def setUp(self):
        super().setUp()
        tout.init(tout.WARNING)

    @mock.patch('uman_pkg.cmdpy.settings')
    def test_pytest_env_id_pythonpath(self, mock_settings):
        """Test that pytest_env() adds hooks to PYTHONPATH when id is set"""
        mock_settings.get.return_value = None

        # Create the hooks directory
        hooks_py = os.path.join(self.test_dir, 'test/hooks/py/travis-ci')
        os.makedirs(hooks_py)
        # Also need test/hooks/bin for the PATH setup
        os.makedirs(os.path.join(self.test_dir, 'test/hooks/bin/travis-ci'))

        with mock.patch.object(cmdpy, 'get_uboot_dir',
                               return_value=self.test_dir):
            # Without id, no PYTHONPATH
            env = cmdpy.pytest_env('sandbox')
            self.assertNotIn('PYTHONPATH', env)

            # With id, hooks dir added to PYTHONPATH
            env = cmdpy.pytest_env('sandbox', test_py_id='na')
            self.assertIn('PYTHONPATH', env)
            self.assertIn(hooks_py, env['PYTHONPATH'])


class TestPytestSpec(TestBase):
    """Tests for pytest test-spec handling"""

    def setUp(self):
        super().setUp()
        tout.init(tout.WARNING)

    def _build_args(self, test_spec=None):
        return make_args(
            cmd='pytest', board='qemu_arm64_spl',
            test_spec=test_spec or [], build=False, output_dir=None,
            test_py_id=None, why_skip=False,
            quiet=False, show_output=False, no_timeout=False,
            setup_only=False, persist=False, gdb_phase=None, gdbserver=None,
            timing=None, exitfirst=False, malloc_dump=None, leak_check=False,
            show_leaks=10, flattree_too=False)

    def test_user_spec_overrides_gitlab(self):
        """Test that a user-supplied -t spec replaces the gitlab default"""
        with mock.patch.object(cmdpy, 'get_board_test_spec',
                               return_value='test_passage'):
            with mock.patch.object(cmdpy, 'get_board_test_id',
                                   return_value='qemu'):
                with mock.patch.object(cmdpy, 'has_no_full',
                                       return_value=False):
                    args = self._build_args(test_spec=['help'])
                    cmd = cmdpy.build_pytest_cmd(args)
        self.assertIn('-k', cmd)
        spec = cmd[cmd.index('-k') + 1]
        self.assertEqual('help', spec)

    def test_no_user_spec_uses_gitlab(self):
        """Test that gitlab spec is used when user provides no -t"""
        with mock.patch.object(cmdpy, 'get_board_test_spec',
                               return_value='test_passage'):
            with mock.patch.object(cmdpy, 'get_board_test_id',
                                   return_value='qemu'):
                with mock.patch.object(cmdpy, 'has_no_full',
                                       return_value=False):
                    args = self._build_args()
                    cmd = cmdpy.build_pytest_cmd(args)
        self.assertIn('-k', cmd)
        spec = cmd[cmd.index('-k') + 1]
        self.assertEqual('(test_passage)', spec)


class TestDockerTest(TestBase):
    """Test the docker (d) subcommand"""

    GITLAB_CI = '''default:
  image: ${MIRROR_DOCKER}/sjg20/u-boot-ci-runner:jammy-20250404-abc123

.buildman_and_testpy_template:
  before_script:
    - git config --global --add safe.directory "${CI_PROJECT_DIR}"
    - ln -s travis-ci test/hooks/bin/`hostname`
    - ln -s travis-ci test/hooks/py/`hostname`
    - python3 -m venv /tmp/venv;
      . /tmp/venv/bin/activate;
      pip install -r test/py/requirements.txt
  script:
    - export UBOOT_TRAVIS_BUILD_DIR=/tmp/${TEST_PY_BD}
    - tools/buildman/buildman -o ${UBOOT_TRAVIS_BUILD_DIR} -w -E -W -e
        --board ${TEST_PY_BD} ${OVERRIDE}
    - cp /opt/grub/grub_x86.efi $UBOOT_TRAVIS_BUILD_DIR/
    - if [[ -n "${TEST_SPEC}" ]]; then
        SPEC="${TEST_SPEC}";
      else
        SPEC="${TEST_PY_TEST_SPEC}";
      fi
    - export PATH=/opt/qemu/bin:test/hooks/bin:${PATH};
      ./test/py/test.py -ra --bd ${TEST_PY_BD}
        ${SPEC:+"-k ${SPEC}"}
        --build-dir "$UBOOT_TRAVIS_BUILD_DIR"
        ${TEST_PY_EXTRA}

stages:
  - build
'''

    def setUp(self):
        super().setUp()
        tout.init(tout.NOTICE)
        self.orig_cwd = os.getcwd()
        self.orig_usrc = os.environ.get('USRC')
        if 'USRC' in os.environ:
            del os.environ['USRC']

        # Create a fake U-Boot tree
        os.chdir(self.test_dir)
        os.makedirs('test/py')
        tools.write_file('test/py/test.py', b'# test')
        tools.write_file('.gitlab-ci.yml',
                         self.GITLAB_CI.encode(), binary=True)

    def tearDown(self):
        os.chdir(self.orig_cwd)
        if self.orig_usrc is not None:
            os.environ['USRC'] = self.orig_usrc
        elif 'USRC' in os.environ:
            del os.environ['USRC']
        super().tearDown()

    def test_load_ci_yaml(self):
        """Test loading .gitlab-ci.yml"""
        data = cmddocker.load_ci_yaml(self.test_dir)
        self.assertIsNotNone(data)
        self.assertIn('default', data)

    def test_load_ci_yaml_missing(self):
        """Test load_ci_yaml with missing file"""
        with terminal.capture() as (out, err):
            data = cmddocker.load_ci_yaml(self.orig_cwd)
        self.assertIsNone(data)

    def test_get_ci_image(self):
        """Test parsing Docker image from parsed YAML"""
        data = cmddocker.load_ci_yaml(self.test_dir)
        image = cmddocker.get_ci_image(data)
        self.assertEqual(
            'docker.io/sjg20/u-boot-ci-runner:jammy-20250404-abc123',
            image)

    def test_get_ci_image_no_match(self):
        """Test get_ci_image when image is not found"""
        with terminal.capture() as (out, err):
            image = cmddocker.get_ci_image({'stages': ['build']})
        self.assertIsNone(image)
        self.assertEqual('Cannot find image in .gitlab-ci.yml\n',
                         err.getvalue())

    def test_get_ci_script(self):
        """Test extracting script from parsed YAML"""
        data = cmddocker.load_ci_yaml(self.test_dir)
        before, script = cmddocker.get_ci_script(data)
        self.assertTrue(len(before) > 0)
        self.assertTrue(len(script) > 0)

    def test_build_script(self):
        """Test script generation without test spec"""
        data = cmddocker.load_ci_yaml(self.test_dir)
        script = cmddocker.build_script(data, 'sandbox', None)
        self.assertIn('--board sandbox', script)
        self.assertIn('UBOOT_TRAVIS_BUILD_DIR=/tmp/sandbox', script)
        self.assertIn('cp /opt/grub/grub_x86.efi', script)

    def test_build_script_test_spec(self):
        """Test script generation with test spec"""
        data = cmddocker.load_ci_yaml(self.test_dir)
        script = cmddocker.build_script(data, 'sandbox',
                                        'test_ofplatdata')
        self.assertIn('test_ofplatdata', script)

    def test_build_script_gdb(self):
        """Test script generation with gdbserver wrapper"""
        data = cmddocker.load_ci_yaml(self.test_dir)
        script = cmddocker.build_script(data, 'sandbox', None, gdb=True)
        self.assertIn('apt-get', script)
        self.assertIn('mv $bd/u-boot $bd/u-boot.real', script)
        self.assertIn('gdbserver :1234', script)
        self.assertIn('chmod +x $bd/u-boot', script)

    def test_build_script_board(self):
        """Test script generation with a different board"""
        data = cmddocker.load_ci_yaml(self.test_dir)
        script = cmddocker.build_script(data, 'sandbox_flattree', None)
        self.assertIn('--board sandbox_flattree', script)
        self.assertIn('UBOOT_TRAVIS_BUILD_DIR=/tmp/sandbox_flattree',
                      script)

    def test_run_dry_run(self):
        """Test dry-run shows docker command"""
        args = cmdline.parse_args(['-n', 'docker'])
        with terminal.capture() as (out, err):
            res = cmddocker.run(args)
        self.assertEqual(0, res)
        output = out.getvalue()
        self.assertIn('docker run --rm', output)
        self.assertIn('--user', output)
        self.assertIn('HOME=/tmp', output)
        self.assertIn('/etc/passwd:/etc/passwd:ro', output)
        self.assertIn('docker.io/sjg20/u-boot-ci-runner', output)
        self.assertFalse(err.getvalue())

    def test_run_dry_run_with_spec(self):
        """Test dry-run with test spec"""
        args = cmdline.parse_args(['-n', 'd', 'test_ofplatdata'])
        with terminal.capture() as (out, err):
            res = cmddocker.run(args)
        self.assertEqual(0, res)
        output = out.getvalue()
        self.assertIn('test_ofplatdata', output)
        self.assertFalse(err.getvalue())

    def test_run_dry_run_interactive(self):
        """Test dry-run in interactive mode"""
        args = cmdline.parse_args(['-n', 'docker', '-I'])
        with terminal.capture() as (out, err):
            res = cmddocker.run(args)
        self.assertEqual(0, res)
        output = out.getvalue()
        self.assertIn('docker run --rm', output)
        # Interactive mode should just run bash, not bash -c <script>
        self.assertNotIn('bash -c', output)
        self.assertFalse(err.getvalue())

    def test_run_dry_run_custom_image(self):
        """Test dry-run with custom image"""
        args = cmdline.parse_args(
            ['-n', 'd', '-i', 'myimage:latest'])
        with terminal.capture() as (out, err):
            res = cmddocker.run(args)
        self.assertEqual(0, res)
        self.assertIn('myimage:latest', out.getvalue())
        self.assertFalse(err.getvalue())

    def test_run_no_uboot(self):
        """Test run when not in a U-Boot tree"""
        os.chdir(self.orig_cwd)
        args = cmdline.parse_args(['docker'])
        with terminal.capture() as (out, err):
            res = cmddocker.run(args)
        self.assertEqual(1, res)

    def test_run_dry_run_gdb(self):
        """Test dry-run with gdbserver wrapper for u-boot"""
        args = cmdline.parse_args(['-n', 'docker', '-g'])
        self.assertEqual('u-boot', args.gdb_phase)
        with terminal.capture() as (out, err):
            res = cmddocker.run(args)
        self.assertEqual(0, res)
        output = out.getvalue()
        self.assertIn('--user 0:0', output)
        self.assertIn('--cap-add=SYS_PTRACE', output)
        self.assertIn('-p 1234:1234', output)
        self.assertIn('apt-get', output)
        self.assertIn('u-boot.real', output)
        self.assertIn('gdbserver :1234', output)
        self.assertIn('um py -G -B sandbox', output)
        self.assertNotIn('--gdbserver', output)

    def test_run_dry_run_gdb_spl(self):
        """Test dry-run with gdbserver for SPL"""
        args = cmdline.parse_args(['-n', 'docker', '--gdb-phase', 'spl'])
        self.assertEqual('spl', args.gdb_phase)
        with terminal.capture() as (out, err):
            res = cmddocker.run(args)
        self.assertEqual(0, res)
        output = out.getvalue()
        self.assertIn('--gdbserver localhost:1234', output)
        self.assertNotIn('u-boot.real', output)

    def test_cmdline_defaults(self):
        """Test default argument values"""
        args = cmdline.parse_args(['docker'])
        self.assertEqual('sandbox', args.board)
        self.assertEqual([], args.test_spec)
        self.assertIsNone(args.gdb_phase)
        self.assertIsNone(args.image)
        self.assertFalse(args.interactive)

    def test_cmdline_alias(self):
        """Test d alias resolves to docker"""
        args = cmdline.parse_args(['d'])
        self.assertEqual('docker', args.cmd)

    def test_control_dispatch(self):
        """Test control.run_command dispatches to docker-test"""
        args = cmdline.parse_args(['-n', 'docker'])
        with terminal.capture() as (out, err):
            res = control.run_command(args)
        self.assertEqual(0, res)
        self.assertIn('docker run', out.getvalue())
