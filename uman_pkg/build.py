# SPDX-License-Identifier: GPL-2.0+
# Copyright 2025 Canonical Ltd
# Written by Simon Glass <simon.glass@canonical.com>

"""Build command for building U-Boot

This module handles the 'build' subcommand which builds U-Boot for a
specified board using buildman.
"""

import os
import shutil
import sys

# pylint: disable=import-error
from u_boot_pylib import command
from u_boot_pylib import tout

from uman_pkg import settings
from uman_pkg.util import exec_cmd, setup_uboot_dir


# ELF files to process (relative to build directory)
ELF_TARGETS = [
    'u-boot',
    'spl/u-boot-spl',
    'tpl/u-boot-tpl',
    'vpl/u-boot-vpl',
]


def get_execs(build_dir):
    """Iterate over ELF targets that exist in the build directory

    Args:
        build_dir (str): Path to build directory

    Yields:
        str: Full path to each existing ELF file
    """
    for target in ELF_TARGETS:
        elf_path = os.path.join(build_dir, target)
        if os.path.exists(elf_path):
            yield elf_path


def get_cross_tool(board, tool):
    """Get a cross-compiled tool for a board

    Args:
        board (str): Board name
        tool (str): Tool name (e.g. 'objdump', 'nm', 'size')

    Returns:
        str: Cross-compiled tool name (e.g. 'arm-linux-gnueabi-objdump')
    """
    prefix = buildman_output('-A', '--boards', board)
    return f'{prefix}{tool}'


def run_objdump(build_dir, board, args):
    """Run objdump on built ELF files to create disassembly

    Args:
        build_dir (str): Path to build directory
        board (str): Board name (for cross toolchain)
        args (argparse.Namespace): Arguments from cmdline

    Returns:
        int: Number of files disassembled
    """
    objdump = get_cross_tool(board, 'objdump')

    count = 0
    for elf_path in get_execs(build_dir):
        dis_path = f'{elf_path}.dis'
        tout.info(f'Disassembling {elf_path}')
        if not args.dry_run:
            result = command.run_one(objdump, '-d', '-S', elf_path)
            with open(dis_path, 'w', encoding='utf-8') as outf:
                outf.write(result.stdout)
        count += 1
    return count


def show_size(build_dir, args):
    """Show size information for built ELF files

    Args:
        build_dir (str): Path to build directory
        args (argparse.Namespace): Arguments from cmdline
    """
    elf_files = list(get_execs(build_dir))
    if not elf_files:
        tout.warning('No ELF files found')
        return

    result = exec_cmd(['size'] + elf_files, args.dry_run)
    if result:
        print(result.stdout)


def get_dir(board):
    """Get the build directory for a board

    Args:
        board (str): Board name

    Returns:
        str: Path to the build directory
    """
    base_dir = settings.get('build_dir', '/tmp/b')
    return os.path.join(base_dir, board)


def get_buildman():
    """Get the path to buildman

    Uses UBOOT_TOOLS env var if set, otherwise falls back to PATH.

    Returns:
        str: Path to buildman executable
    """
    uboot_tools = os.environ.get('UBOOT_TOOLS')
    if uboot_tools:
        return os.path.join(os.path.expanduser(uboot_tools),
                           'buildman', 'buildman')
    return 'buildman'


def buildman(*args, env=None, dry_run=False, capture=True):
    """Run buildman with arguments

    Args:
        *args: Arguments to pass to buildman
        env (dict): Optional environment variables
        dry_run (bool): If True, just show command without running
        capture (bool): Whether to capture output (default True)

    Returns:
        CommandResult or None: Result with return_code, stdout, stderr,
            or None if dry_run
    """
    cmd = [get_buildman()] + list(args)
    return exec_cmd(cmd, dry_run=dry_run, env=env, capture=capture)


def buildman_output(*args):
    """Run buildman and return its output

    Args:
        *args: Arguments to pass to buildman

    Returns:
        str: Command output (stdout), stripped of trailing whitespace

    Raises:
        command.CommandExc: If the command fails
    """
    return command.output(get_buildman(), *args).strip()


def get_cmd(args, board, build_dir):
    """Build the full buildman command

    Args:
        args (argparse.Namespace): Arguments from cmdline
        board (str): Board name to build
        build_dir (str): Path to build directory

    Returns:
        list: Full buildman command including 'buildman' as first element
    """
    return ['buildman'] + get_buildman_args(args, board, build_dir)


def get_buildman_args(args, board, build_dir):
    """Build the buildman arguments

    Args:
        args (argparse.Namespace): Arguments from cmdline
        board (str): Board name to build
        build_dir (str): Path to build directory

    Returns:
        list: Arguments for buildman (not including buildman itself)
    """
    if args.in_tree:
        bm_args = ['-i', '--boards', board]
    else:
        bm_args = ['-I', '-w', '--boards', board, '-o', build_dir]
    if not args.lto:
        bm_args.insert(0, '-L')
    if args.target:
        bm_args.extend(['--target', args.target])
    if args.jobs:
        bm_args.extend(['-j', str(args.jobs)])
    if args.force_reconfig:
        bm_args.append('-C')
    if args.adjust_cfg:
        for cfg in args.adjust_cfg:
            bm_args.extend(['-a', cfg])
    return bm_args


def build_board(board, dry_run=False, lto=False, adjust_cfg=None,
                force_reconfig=False, fresh=False, jobs=None, trace=False,
                trace_early=True, output_dir=None):
    """Build U-Boot for a board

    Args:
        board (str): Board name to build
        dry_run (bool): If True, just show command without running
        lto (bool): If True, enable LTO (Link Time Optimization)
        adjust_cfg (list): Kconfig adjustments to apply
        force_reconfig (bool): Force reconfiguration
        fresh (bool): Delete build dir first
        jobs (int): Number of parallel jobs
        trace (bool): Enable function tracing
        trace_early (bool): Enable TRACE_EARLY when trace is True (default True)

    Returns:
        bool: True if build succeeded, False otherwise
    """
    if not setup_uboot_dir():
        return False

    build_dir = output_dir or get_dir(board)

    if fresh and os.path.exists(build_dir):
        tout.progress(f'Removing build directory: {build_dir}')
        if not dry_run:
            shutil.rmtree(build_dir)

    tout.progress(f'Building {board}')

    bm_args = ['-I', '-w', '--boards', board, '-o', build_dir]
    if not lto:
        bm_args.insert(0, '-L')
    if force_reconfig:
        bm_args.append('-C')
    if jobs:
        bm_args.extend(['-j', str(jobs)])
    if adjust_cfg:
        for cfg in adjust_cfg:
            bm_args.extend(['-a', cfg])

    env = None
    if trace:
        bm_args.extend(['-a', 'TRACE'])
        if trace_early:
            bm_args.extend(['-a', 'TRACE_EARLY'])
        env = os.environ.copy()
        env['FTRACE'] = '1'

    result = buildman(*bm_args, dry_run=dry_run, env=env, capture=False)
    tout.clear_progress()

    if result is None:  # dry-run
        return True

    if result.return_code != 0:
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        tout.error('Build failed')
        return False
    return True


def try_build(board, build_dir):
    """Try to build a board, returning success/failure

    Args:
        board (str): Board name to build
        build_dir (str): Path to build directory

    Returns:
        bool: True if build succeeded, False otherwise
    """
    result = buildman('-I', '-w', '-L', '-W', '-C', '--boards', board,
                      '-o', build_dir)
    return result.return_code == 0


def do_bisect(board, build_dir):
    """Bisect to find first failing commit

    Assumes the current commit fails to build and upstream is good.

    Args:
        board (str): Board name to build
        build_dir (str): Path to build directory

    Returns:
        int: Exit code (0 for success, 1 for error)
    """
    # Check for rebase in progress
    result = command.run_one('git', 'status', capture=True)
    if 'rebase in progress' in result.stdout:
        tout.error('Rebase in progress, cannot bisect')
        return 1

    # Get current branch name (or commit if detached)
    try:
        orig_branch = command.output_one_line('git', 'symbolic-ref', '--short',
                                              'HEAD')
    except command.CommandExc:
        orig_branch = None

    # Get upstream commit
    try:
        upstream = command.output_one_line('git', 'rev-parse', '@{u}')
    except command.CommandExc:
        tout.error('Cannot find upstream branch')
        return 1

    head = command.output_one_line('git', 'rev-parse', 'HEAD')
    tout.notice(f'Bisecting {board} between upstream and HEAD')
    tout.info(f'  Upstream: {upstream[:12]}')
    tout.info(f'  HEAD:     {head[:12]}')

    # Verify current commit fails
    tout.notice('Verifying HEAD fails to build...')
    if try_build(board, build_dir):
        tout.error('HEAD builds successfully, nothing to bisect')
        return 1
    tout.info('  HEAD: bad (fails to build)')

    # Verify upstream builds
    tout.notice('Verifying upstream builds...')
    command.run_one('git', 'checkout', upstream)
    if not try_build(board, build_dir):
        command.run_one('git', 'checkout', head)
        tout.error('Upstream fails to build, cannot bisect')
        return 1
    tout.info('  Upstream: good (builds)')

    # Go back to HEAD for bisect
    command.run_one('git', 'checkout', head)

    # Start bisect
    command.run_one('git', 'bisect', 'start')
    command.run_one('git', 'bisect', 'bad', head)
    command.run_one('git', 'bisect', 'good', upstream)

    # Run bisect
    tout.notice('Running bisect...')
    step = 1
    while True:
        current = command.output_one_line('git', 'rev-parse', 'HEAD')
        subject = command.output_one_line('git', 'log', '-1', '--format=%s')
        tout.progress(f'  Step {step}: {current[:12]} {subject[:50]}',
                      trailer='')

        if try_build(board, build_dir):
            result = command.run_one('git', 'bisect', 'good', capture=True)
        else:
            result = command.run_one('git', 'bisect', 'bad', capture=True)
        tout.clear_progress()

        # Check if bisect is done - parse commit from output
        for line in result.stdout.splitlines():
            if 'is the first bad commit' in line:
                bad_commit = line.split()[0]
                break
        else:
            step += 1
            continue
        break

    # Get the subject for the bad commit
    subject = command.output_one_line('git', 'log', '-1', '--format=%s',
                                       bad_commit)

    # Clean up and return to original branch
    command.run_one('git', 'bisect', 'reset')
    if orig_branch:
        command.run_one('git', 'checkout', orig_branch)

    # Report result
    tout.notice(f'\nFirst bad commit: {bad_commit[:12]} {subject}')
    return 0


def run(args):
    """Handle build command - build U-Boot for a board

    Args:
        args (argparse.Namespace): Arguments from cmdline

    Returns:
        int: Exit code
    """
    board = args.board or args.board_opt or os.environ.get('b')
    if not board:
        tout.error('Board is required: use -B BOARD or set $b')
        return 1

    if not setup_uboot_dir():
        return 1

    build_dir = args.output_dir or get_dir(board)

    if args.bisect:
        return do_bisect(board, build_dir)

    if args.fresh and os.path.exists(build_dir):
        if not args.dry_run:
            shutil.rmtree(build_dir)

    bm_args = get_buildman_args(args, board, build_dir)

    env = None
    if args.trace or args.gprof or args.werror:
        env = os.environ.copy()
        if args.trace:
            bm_args.extend(['-a', 'TRACE'])
            if not args.no_trace_early:
                bm_args.extend(['-a', 'TRACE_EARLY'])
            env['FTRACE'] = '1'
        if args.gprof:
            env['GPROF'] = '1'
        if args.werror:
            env['KCFLAGS'] = '-Werror'
    if args.debug:
        bm_args.extend(['-a', 'CC_OPTIMIZE_FOR_DEBUG'])

    result = buildman(*bm_args, env=env, dry_run=args.dry_run, capture=False)

    if result is None:  # dry-run
        if args.objdump:
            run_objdump(build_dir, board, args)
        if args.size:
            show_size(build_dir, args)
        return 0

    if result.return_code != 0:
        # Buildman returns 101 for warnings even if build succeeded
        if result.return_code == 101:
            elf_path = os.path.join(build_dir, 'u-boot')
            if os.path.exists(elf_path):
                if args.fail_on_warning:
                    tout.error('Build succeeded but produced warnings')
                    return result.return_code
                tout.warning('Build succeeded with warnings')
            else:
                tout.info('Build failed')
                return result.return_code
        else:
            if result.stderr:
                print(result.stderr, file=sys.stderr)
            tout.info('Build failed')
            return result.return_code

    if args.objdump:
        count = run_objdump(build_dir, board, args)
        tout.notice(f'Disassembled {count} file(s)')

    if args.size:
        show_size(build_dir, args)

    tout.info('Build complete')
    return 0
