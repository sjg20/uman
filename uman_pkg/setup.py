# SPDX-License-Identifier: GPL-2.0+
# Copyright 2025 Canonical Ltd
# Written by Simon Glass <simon.glass@canonical.com>

"""Setup command for downloading and building firmware blobs

This module handles the 'setup' subcommand which downloads and builds
various firmware components needed for U-Boot testing.
"""

import os
import shutil
import tempfile

# pylint: disable=import-error
from u_boot_pylib import command
from u_boot_pylib import tout

from uman_pkg import cmdgit
from uman_pkg import settings


# Available components for setup command
SETUP_COMPONENTS = {
    'aliases': 'Create symlinks for git action commands',
    'qemu': 'QEMU emulators for all architectures',
    'opensbi': 'OpenSBI firmware for RISC-V',
    'tfa': 'ARM Trusted Firmware for QEMU SBSA',
    'xtensa': 'Xtensa dc233c toolchain',
}

def setup_aliases(args):
    """Create symlinks for git action commands

    Args:
        args (argparse.Namespace): Command line arguments
            args.alias_dir: Directory to create symlinks in

    Returns:
        int: Exit code (0 for success, non-zero for failure)
    """
    alias_dir = getattr(args, 'alias_dir', None)
    if not alias_dir:
        alias_dir = os.path.expanduser('~/bin')
        tout.notice(f'Using default directory: {alias_dir}')

    alias_dir = os.path.expanduser(alias_dir)

    # Find uman executable
    uman_path = shutil.which('um') or shutil.which('uman')
    if not uman_path:
        # Try to find it relative to this file
        this_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        uman_path = os.path.join(this_dir, 'um')
        if not os.path.exists(uman_path):
            tout.error('Cannot find uman executable')
            return 1

    uman_path = os.path.abspath(uman_path)

    aliases = [a.short for a in cmdgit.GIT_ACTIONS] + ['cg']

    if args.dry_run:
        tout.notice(f'Would create symlinks in {alias_dir} -> {uman_path}')
        for name in aliases:
            tout.notice(f'  {name}')
        return 0

    # Create directory if needed
    os.makedirs(alias_dir, exist_ok=True)

    created = []
    skipped = []
    for name in aliases:
        link_path = os.path.join(alias_dir, name)
        if os.path.exists(link_path) or os.path.islink(link_path):
            if args.force:
                os.remove(link_path)
            else:
                skipped.append(name)
                continue
        os.symlink(uman_path, link_path)
        created.append(name)

    if created:
        tout.notice(f'Created symlinks: {" ".join(created)}')
    if skipped:
        tout.notice(f'Skipped (already exist): {" ".join(skipped)}')
        if not args.force:
            tout.notice('Use --force to overwrite')

    tout.notice(f'Symlinks in {alias_dir} point to {uman_path}')
    return 0


# QEMU packages needed for testing
QEMU_PACKAGES = {
    'qemu-system-arm': ['qemu_arm', 'qemu_arm_spl', 'qemu_arm64',
                        'qemu_arm64_acpi', 'qemu_arm64_lwip',
                        'qemu_arm64_spl', 'qemu-arm-sbsa'],
    'qemu-system-misc': ['qemu-riscv32', 'qemu-riscv32_smode',
                         'qemu-riscv32_spl', 'qemu-riscv64',
                         'qemu-riscv64_smode', 'qemu-riscv64_smode_acpi',
                         'qemu-riscv64_spl', 'qemu-xtensa-dc233c'],
    'qemu-system-mips': ['malta', 'maltael', 'malta64', 'malta64el'],
    'qemu-system-ppc': ['qemu-ppce500'],
    'qemu-system-x86': ['qemu-x86', 'qemu-x86_64'],
}

def setup_qemu(args):
    """Check and install QEMU packages

    Args:
        args (argparse.Namespace): Command line arguments

    Returns:
        int: Exit code (0 for success, non-zero for failure)
    """
    # Check which packages are missing
    missing = []
    for package in QEMU_PACKAGES:
        try:
            command.output('dpkg', '-s', package)
        except command.CommandExc:
            missing.append(package)

    if not missing:
        tout.notice('All QEMU packages are installed')
        return 0

    tout.notice(f'Missing QEMU packages: {" ".join(missing)}')
    install_cmd = ['sudo', 'apt-get', 'install', '-y'] + missing

    if args.dry_run:
        tout.notice(f'Would run: {" ".join(install_cmd)}')
        return 0

    # Try to install missing packages
    tout.notice('Installing missing packages (may require sudo password)...')
    result = command.run_pipe([install_cmd], capture=False,
                              raise_on_error=False)
    if result.return_code:
        tout.error('Failed to install QEMU packages')
        tout.notice(f'Try running manually: {" ".join(install_cmd)}')
        return 1

    tout.notice('QEMU packages installed')
    return 0


# OpenSBI release URL and version
OPENSBI_VER = '1.3.1'
OPENSBI_URL = (f'https://github.com/riscv-software-src/opensbi/releases/'
               f'download/v{OPENSBI_VER}/opensbi-{OPENSBI_VER}-rv-bin.tar.xz')

# TF-A repository
TFA_REPO = 'https://git.trustedfirmware.org/TF-A/trusted-firmware-a.git'

# Xtensa toolchain
XTENSA_URL = ('https://github.com/foss-xtensa/toolchain/releases/download/'
              '2020.07/x86_64-2020.07-xtensa-dc233c-elf.tar.gz')


def setup_opensbi(blobs_dir, args):
    """Download pre-built OpenSBI firmware for both rv32 and rv64

    Args:
        blobs_dir (str): Directory to store firmware
        args (argparse.Namespace): Command line arguments

    Returns:
        int: Exit code (0 for success, non-zero for failure)
    """
    opensbi_dir = os.path.join(blobs_dir, 'opensbi')
    output_rv64 = os.path.join(opensbi_dir, 'fw_dynamic.bin')
    output_rv32 = os.path.join(opensbi_dir, 'fw_dynamic_rv32.bin')

    # Check if already downloaded
    if (os.path.exists(output_rv64) and os.path.exists(output_rv32)
            and not args.force):
        tout.notice(f'OpenSBI already present: {output_rv64}')
        tout.notice(f'OpenSBI already present: {output_rv32}')
        tout.notice('Use --force to re-download')
        return 0

    # Create directory
    os.makedirs(opensbi_dir, exist_ok=True)

    if args.dry_run:
        tout.notice(f'Would download OpenSBI v{OPENSBI_VER}')
        return 0

    # Download and extract
    tout.notice(f'Downloading OpenSBI v{OPENSBI_VER}...')

    with tempfile.TemporaryDirectory() as tmpdir:
        # Download and extract tarball
        wget_cmd = ['wget', '-q', '-O', '-', OPENSBI_URL]
        tar_cmd = ['tar', '-C', tmpdir, '-xJ']

        command.run_pipe([wget_cmd, tar_cmd], capture=True)

        # Copy firmware files
        subdir = f'opensbi-{OPENSBI_VER}-rv-bin/share/opensbi'
        extract_dir = os.path.join(tmpdir, subdir)

        fw64_src = os.path.join(extract_dir,
                                'lp64/generic/firmware/fw_dynamic.bin')
        fw32_src = os.path.join(extract_dir,
                                'ilp32/generic/firmware/fw_dynamic.bin')

        if not os.path.exists(fw64_src):
            tout.error(f'64-bit firmware not found: {fw64_src}')
            return 1
        if not os.path.exists(fw32_src):
            tout.error(f'32-bit firmware not found: {fw32_src}')
            return 1

        shutil.copy(fw64_src, output_rv64)
        shutil.copy(fw32_src, output_rv32)

    tout.notice(f'OpenSBI rv64: {output_rv64}')
    tout.notice(f'OpenSBI rv32: {output_rv32}')
    return 0


def setup_tfa(blobs_dir, args):
    """Build ARM Trusted Firmware for QEMU SBSA

    Args:
        blobs_dir (str): Directory to store firmware
        args (argparse.Namespace): Command line arguments

    Returns:
        int: Exit code (0 for success, non-zero for failure)
    """
    tfa_dir = os.path.join(blobs_dir, 'tfa')
    output_bl1 = os.path.join(tfa_dir, 'bl1.bin')
    output_fip = os.path.join(tfa_dir, 'fip.bin')

    # Check if already built
    if (os.path.exists(output_bl1) and os.path.exists(output_fip)
            and not args.force):
        tout.notice(f'TF-A already present: {output_bl1}')
        tout.notice(f'TF-A already present: {output_fip}')
        tout.notice('Use --force to rebuild')
        return 0

    if args.dry_run:
        tout.notice('Would build TF-A for QEMU SBSA')
        return 0

    # Create directory
    os.makedirs(tfa_dir, exist_ok=True)

    # Clone or update TF-A
    tfa_src = os.path.join(tfa_dir, 'src')
    if os.path.exists(tfa_src):
        tout.notice('Updating TF-A source...')
        command.run_pipe([['git', '-C', tfa_src, 'pull']], capture=True)
    else:
        tout.notice('Cloning TF-A...')
        command.run_pipe([['git', 'clone', '--depth=1', TFA_REPO, tfa_src]],
                         capture=True)

    # Build TF-A for qemu_sbsa
    tout.notice('Building TF-A for QEMU SBSA...')
    make_cmd = [
        'make', '-C', tfa_src, '-j', str(os.cpu_count() or 4),
        'CROSS_COMPILE=aarch64-linux-gnu-',
        'PLAT=qemu_sbsa',
        'ARM_LINUX_KERNEL_AS_BL33=1',
        'DEBUG=1',
        'all', 'fip'
    ]
    command.run_pipe([make_cmd], capture=True)

    # Copy firmware files
    build_dir = os.path.join(tfa_src, 'build/qemu_sbsa/debug')
    bl1_src = os.path.join(build_dir, 'bl1.bin')
    fip_src = os.path.join(build_dir, 'fip.bin')

    if not os.path.exists(bl1_src):
        tout.error(f'bl1.bin not found: {bl1_src}')
        return 1
    if not os.path.exists(fip_src):
        tout.error(f'fip.bin not found: {fip_src}')
        return 1

    shutil.copy(bl1_src, output_bl1)
    shutil.copy(fip_src, output_fip)

    tout.notice(f'TF-A bl1: {output_bl1}')
    tout.notice(f'TF-A fip: {output_fip}')
    return 0


def setup_xtensa(blobs_dir, args):
    """Download Xtensa dc233c toolchain

    Args:
        blobs_dir (str): Directory to store toolchain
        args (argparse.Namespace): Command line arguments

    Returns:
        int: Exit code (0 for success, non-zero for failure)
    """
    xtensa_dir = os.path.join(blobs_dir, 'xtensa')
    toolchain_dir = os.path.join(xtensa_dir, '2020.07/xtensa-dc233c-elf')
    gcc_path = os.path.join(toolchain_dir, 'bin/xtensa-dc233c-elf-gcc')

    # Check if already installed
    if os.path.exists(gcc_path) and not args.force:
        tout.notice(f'Xtensa toolchain already present: {toolchain_dir}')
        tout.notice('Use --force to re-download')
        return 0

    if args.dry_run:
        tout.notice('Would download Xtensa dc233c toolchain')
        return 0

    # Create directory
    os.makedirs(xtensa_dir, exist_ok=True)

    # Download and extract
    tout.notice('Downloading Xtensa dc233c toolchain...')
    wget_cmd = ['wget', '-q', '-O', '-', XTENSA_URL]
    tar_cmd = ['tar', '-C', xtensa_dir, '-xz']

    command.run_pipe([wget_cmd, tar_cmd], capture=True)

    if not os.path.exists(gcc_path):
        tout.error(f'Toolchain not found after extraction: {gcc_path}')
        return 1

    # Update ~/.buildman with toolchain prefix
    buildman_file = os.path.expanduser('~/.buildman')
    tc_prefix = os.path.join(toolchain_dir, 'bin/xtensa-dc233c-elf-')

    # Check if already configured
    if os.path.exists(buildman_file):
        with open(buildman_file, 'r', encoding='utf-8') as fil:
            content = fil.read()
        if 'xtensa = ' in content:
            tout.notice('Xtensa already configured in ~/.buildman')
        elif '[toolchain-prefix]' in content:
            # Add to existing section
            new_content = content.replace(
                '[toolchain-prefix]',
                f'[toolchain-prefix]\nxtensa = {tc_prefix}',
                1)
            with open(buildman_file, 'w', encoding='utf-8') as fil:
                fil.write(new_content)
            tout.notice('Added xtensa toolchain to ~/.buildman')
        else:
            # Create new section
            with open(buildman_file, 'a', encoding='utf-8') as fil:
                fil.write(f'\n[toolchain-prefix]\nxtensa = {tc_prefix}\n')
            tout.notice('Added xtensa toolchain to ~/.buildman')
    else:
        with open(buildman_file, 'w', encoding='utf-8') as fil:
            fil.write(f'[toolchain-prefix]\nxtensa = {tc_prefix}\n')
        tout.notice('Created ~/.buildman with xtensa toolchain')

    tout.notice(f'Xtensa toolchain: {toolchain_dir}')
    return 0


def do_setup(args):
    """Handle setup command - build firmware blobs

    Args:
        args (argparse.Namespace): Arguments from cmdline

    Returns:
        int: Exit code
    """
    if args.list_components:
        tout.notice('Available components:')
        for name, desc in SETUP_COMPONENTS.items():
            tout.notice(f'  {name}: {desc}')
        return 0

    blobs_dir = settings.get('blobs_dir', '~/dev/blobs')

    # Determine which components to build
    if args.component:
        if args.component not in SETUP_COMPONENTS:
            tout.error(f'Unknown component: {args.component}')
            tout.notice('Use --list to see available components')
            return 1
        components = [args.component]
    else:
        components = list(SETUP_COMPONENTS.keys())

    # Dispatch table for component setup functions
    setup_funcs = {
        'aliases': lambda: setup_aliases(args),
        'qemu': lambda: setup_qemu(args),
        'opensbi': lambda: setup_opensbi(blobs_dir, args),
        'tfa': lambda: setup_tfa(blobs_dir, args),
        'xtensa': lambda: setup_xtensa(blobs_dir, args),
    }

    # Build each component
    for component in components:
        tout.notice(f'Setting up {component}...')
        result = setup_funcs[component]()
        if result:
            return result

    tout.notice('Setup complete')
    return 0
