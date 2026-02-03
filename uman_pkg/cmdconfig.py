# SPDX-License-Identifier: GPL-2.0+
# Copyright 2025 Canonical Ltd
# Written by Simon Glass <simon.glass@canonical.com>

"""Config command for examining U-Boot configuration

This module handles the 'config' subcommand which provides tools for
examining U-Boot .config files.
"""

import os
import re
import shutil

# pylint: disable=import-error
from u_boot_pylib import tout

from uman_pkg import build as build_mod
from uman_pkg.util import exec_cmd, get_uboot_dir


def get_config_path(board, output_dir=None):
    """Get the path to the .config file for a board

    Args:
        board (str): Board name
        output_dir (str): Build directory override, or None for default

    Returns:
        str: Path to the .config file
    """
    bdir = output_dir or build_mod.get_dir(board)
    return os.path.join(bdir, '.config')


def do_grep(args):
    """Grep the .config file for a pattern

    Args:
        args (argparse.Namespace): Arguments from cmdline

    Returns:
        int: Exit code (0 for success, non-zero for failure)
    """
    board = args.board or os.environ.get('b')
    if not board:
        tout.error('Board is required: use -B BOARD or set $b')
        return 1

    if args.build and not build_mod.build_board(
            board, args.dry_run, args.lto,
            adjust_cfg=args.adjust_cfg,
            force_reconfig=args.force_reconfig, fresh=args.fresh,
            jobs=args.jobs, trace=args.trace,
            trace_early=not args.no_trace_early,
            output_dir=args.output_dir):
        return 1

    config_path = get_config_path(board, args.output_dir)
    if not os.path.exists(config_path):
        tout.error(f'Config file not found: {config_path}')
        tout.error(f'Build the board first: um b {board}')
        return 1

    pattern = args.grep
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        tout.error(f'Invalid regex pattern: {exc}')
        return 1

    with open(config_path, 'r', encoding='utf-8') as inf:
        for line in inf:
            if regex.search(line):
                print(line.rstrip())

    return 0


def do_sync(args, use_meld=False):
    """Resync the defconfig from current .config

    Builds with 'cfg' target, runs savedefconfig, and either copies back to
    configs/<board>_defconfig or opens meld for comparison.

    Args:
        args (argparse.Namespace): Arguments from cmdline
        use_meld (bool): If True, open meld instead of copying

    Returns:
        int: Exit code (0 for success, non-zero for failure)
    """
    board = args.board or os.environ.get('b')
    if not board:
        tout.error('Board is required: use -B BOARD or set $b')
        return 1

    if args.build and not build_mod.build_board(
            board, args.dry_run, args.lto,
            adjust_cfg=args.adjust_cfg,
            force_reconfig=args.force_reconfig, fresh=args.fresh,
            jobs=args.jobs, trace=args.trace,
            trace_early=not args.no_trace_early,
            output_dir=args.output_dir):
        return 1

    uboot_dir = get_uboot_dir()
    if not uboot_dir:
        tout.error('Not in a U-Boot tree and $USRC not set')
        return 1

    build_dir = args.output_dir or build_mod.get_dir(board)
    defconfig_path = os.path.join(uboot_dir, 'configs', f'{board}_defconfig')

    # Change to U-Boot directory for make
    orig_dir = os.getcwd()
    os.chdir(uboot_dir)
    try:
        # Step 1: Run defconfig to set up .config
        tout.info(f'Running {board}_defconfig...')
        cmd = ['make', '-s', f'{board}_defconfig', f'O={build_dir}']
        result = exec_cmd(cmd, args.dry_run, capture=False)
        if result and result.return_code != 0:
            tout.error('defconfig failed')
            return result.return_code

        # Step 2: Run savedefconfig
        tout.info('Running savedefconfig...')
        cmd = ['make', '-s', f'O={build_dir}', 'savedefconfig']
        result = exec_cmd(cmd, args.dry_run, capture=False)
        if result and result.return_code != 0:
            tout.error('savedefconfig failed')
            return result.return_code

        # Step 3: Show diff/meld or copy defconfig back
        src = os.path.join(build_dir, 'defconfig')

        if use_meld:
            # Open meld to compare
            tout.info(f'Opening meld: {defconfig_path} vs {src}')
            cmd = ['meld', defconfig_path, src]
            result = exec_cmd(cmd, args.dry_run, capture=False)
            return result.return_code if result else 0

        if not args.dry_run and os.path.exists(defconfig_path):
            # Show diff between old and new
            diff_cmd = ['diff', '-u', '--color=always', defconfig_path, src]
            diff_result = exec_cmd(diff_cmd, capture=True)
            if diff_result and diff_result.stdout:
                print(diff_result.stdout)
            elif diff_result and diff_result.return_code == 0:
                tout.notice('No changes to defconfig')
                return 0

        tout.info(f'Copying {src} -> {defconfig_path}')
        if not args.dry_run:
            shutil.copy(src, defconfig_path)

        tout.notice('Defconfig synced')
        return 0
    finally:
        os.chdir(orig_dir)


def run(args):
    """Handle config command

    Args:
        args (argparse.Namespace): Arguments from cmdline

    Returns:
        int: Exit code
    """
    if args.grep:
        return do_grep(args)

    if args.meld:
        return do_sync(args, use_meld=True)

    if args.sync:
        return do_sync(args)

    tout.error('No action specified (use -g PATTERN, -m, or -s)')
    return 1
