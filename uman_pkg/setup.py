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
from u_boot_pylib import tools
from u_boot_pylib import tout

from uman_pkg import cmdgit
from uman_pkg import settings
from uman_pkg import util


# Available components for setup command
SETUP_COMPONENTS = {
    'aliases': 'Create symlinks for git action commands',
    'remote': 'Deploy uman to a remote machine via SSH',
    'adi-ldr': 'ldr tool for Analog Devices boards',
    'efi': 'QEMU EFI firmware for ARM, ARM64, RISC-V and x86',
    'gcc': 'GCC cross-compiler and build dependencies',
    'qemu': 'QEMU emulators for all architectures',
    'qemu-build': 'Build QEMU from source (for MicroBlaze etc.)',
    'opensbi': 'OpenSBI firmware for RISC-V',
    'tfa': 'ARM Trusted Firmware for QEMU SBSA',
    'xtensa': 'Xtensa dc233c toolchain',
}

QEMU_REPO = 'https://gitlab.com/qemu-project/qemu.git'

# Targets needed for U-Boot testing
QEMU_TARGETS = ('arm-softmmu,aarch64-softmmu,i386-softmmu,x86_64-softmmu,'
                'riscv32-softmmu,riscv64-softmmu,ppc-softmmu,m68k-softmmu,'
                'xtensa-softmmu,microblaze-softmmu')

UM_FUNC = 'um() { b="$b" USRC="$USRC" command um "$@"; }'


def show_shell_hint():
    """Show the shell function hint for setting up the 'um' wrapper"""
    tout.notice('')
    tout.notice('Add this to ~/.bashrc (or source ~/.uman_env) to pass'
                ' shell variables to uman:')
    tout.notice('')
    tout.notice(f'    {UM_FUNC}')
    tout.notice('')
    tout.notice('Add this for simple git aliases (ga, gf, etc.):')
    tout.notice('')
    tout.notice('    eval "$(um git -a)"')


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

    # Find uman executable - prefer the one next to this file
    uman_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             'uman')
    if not os.path.exists(uman_path):
        uman_path = shutil.which('um') or shutil.which('uman')
    if not uman_path:
        tout.error('Cannot find uman executable')
        return 1

    uman_path = os.path.abspath(uman_path)

    aliases = [a.short for a in cmdgit.GIT_ACTIONS] + ['cg', 'uman']

    if args.dry_run:
        tout.notice(f'Would create symlinks in {alias_dir} -> {uman_path}')
        for name in aliases:
            tout.notice(f'  {name}')
        show_shell_hint()
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
    show_shell_hint()
    return 0


def parse_deb_packages(rst_text):
    """Parse Debian package names from RST text containing apt-get commands

    Args:
        rst_text (str): RST file contents

    Returns:
        list of str: Package names found in apt-get install commands
    """
    packages = []
    lines = rst_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if 'apt-get install' in line:
            pos = line.index('apt-get install') + len('apt-get install')
            rest = line[pos:]
            while rest.endswith('\\'):
                rest = rest[:-1]
                i += 1
                if i < len(lines):
                    rest += ' ' + lines[i].strip()
            packages.extend(rest.split())
        i += 1
    return packages


def setup_gcc(args):
    """Check and install GCC build-dependency packages from U-Boot docs

    Reads doc/build/gcc.rst from the U-Boot tree, parses the Debian
    apt-get install blocks and installs any missing packages.

    Args:
        args (argparse.Namespace): Command line arguments

    Returns:
        int: Exit code (0 for success, non-zero for failure)
    """
    uboot_dir = util.get_uboot_dir()
    if not uboot_dir:
        tout.error('Cannot find U-Boot source directory')
        return 1

    rst_path = os.path.join(uboot_dir, 'doc', 'build', 'gcc.rst')
    if not os.path.exists(rst_path):
        tout.error(f'Cannot find {rst_path}')
        return 1

    packages = parse_deb_packages(tools.read_file(rst_path, binary=False))

    if not packages:
        tout.error('No packages found in gcc.rst')
        return 1

    # Check which packages are missing
    missing = []
    for pkg in packages:
        try:
            command.output('dpkg', '-s', pkg)
        except command.CommandExc:
            missing.append(pkg)

    if not missing:
        tout.notice('All gcc packages are installed')
        return 0

    tout.notice(f'Missing gcc packages: {" ".join(missing)}')
    install_cmd = ['sudo', 'apt-get', 'install', '-y'] + missing

    if args.dry_run:
        tout.notice(f'Would run: {" ".join(install_cmd)}')
        return 0

    tout.notice('Installing missing packages (may require sudo password)...')
    result = command.run_pipe([install_cmd], capture=False,
                              raise_on_error=False)
    if result.return_code:
        tout.error('Failed to install gcc packages')
        tout.notice(f'Try running manually: {" ".join(install_cmd)}')
        return 1

    tout.notice('gcc packages installed')
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


# Build dependencies for QEMU from source
QEMU_BUILD_DEPS = [
    'git', 'build-essential', 'ninja-build', 'pkg-config',
    'libglib2.0-dev', 'libpixman-1-dev', 'libslirp-dev',
]


def run_logged(cmd, log, desc, cwd=None):
    """Run a command, appending output to a log file

    Args:
        cmd (list): Command and arguments
        log (file): Open log file to append to
        desc (str): Description for error messages
        cwd (str or None): Working directory

    Returns:
        bool: True on success
    """
    log.write(f'## {desc}\n$ {" ".join(cmd)}\n')
    log.flush()
    result = command.run_pipe(
        [cmd], capture=True, capture_stderr=True,
        raise_on_error=False, cwd=cwd)
    if result.stdout:
        log.write(result.stdout)
    if result.stderr:
        log.write(result.stderr)
    if result.return_code:
        tout.error(f'{desc} failed (see {log.name})')
        return False
    return True


def setup_qemu_build(args):
    """Clone and build QEMU from source

    Clones the QEMU git repo to ~/dev/qemu, builds it with the targets
    needed for U-Boot testing. Output is logged to ~/dev/qemu/build.log

    Args:
        args (argparse.Namespace): Command line arguments

    Returns:
        int: Exit code (0 for success, non-zero for failure)
    """
    qemu_dir = os.path.expanduser('~/dev/qemu')
    build_dir = os.path.join(qemu_dir, 'build')
    log_path = os.path.join(qemu_dir, 'build.log')

    if args.dry_run:
        tout.notice(f'Would clone QEMU to {qemu_dir} and build')
        return 0

    # Install build dependencies
    missing = []
    for pkg in QEMU_BUILD_DEPS:
        try:
            command.output('dpkg', '-s', pkg)
        except command.CommandExc:
            missing.append(pkg)
    if missing:
        tout.notice(f'Installing build deps: {" ".join(missing)}')
        result = command.run_pipe(
            [['sudo', 'apt-get', 'install', '-y'] + missing],
            capture=False, raise_on_error=False)
        if result.return_code:
            tout.error('Failed to install build dependencies')
            return 1

    # Clone or update (log to parent dir until clone creates qemu_dir)
    parent = os.path.dirname(qemu_dir)
    os.makedirs(parent, exist_ok=True)
    tmp_log = os.path.join(parent, 'qemu-build.log')
    need_clone = not os.path.isdir(os.path.join(qemu_dir, '.git'))

    with open(tmp_log, 'w', encoding='utf-8') as log:
        if not need_clone:
            tout.progress('Updating QEMU source')
            if not run_logged(
                    ['git', '-C', qemu_dir, 'pull', '--ff-only'],
                    log, 'git pull'):
                tout.warning('git pull failed; building existing checkout')
        else:
            tout.progress('Cloning QEMU')
            if not run_logged(
                    ['git', 'clone', '--depth=1', QEMU_REPO, qemu_dir],
                    log, 'git clone'):
                return 1

    # Move log into qemu dir now that it exists
    shutil.move(tmp_log, log_path)

    with open(log_path, 'a', encoding='utf-8') as log:

        # Configure
        os.makedirs(build_dir, exist_ok=True)
        tout.progress('Configuring QEMU')
        if not run_logged(
                [os.path.join(qemu_dir, 'configure'),
                 f'--target-list={QEMU_TARGETS}',
                 '--disable-docs', '--disable-user'],
                log, 'configure', cwd=build_dir):
            return 1

        # Build
        jobs = os.cpu_count() or 4
        tout.progress(f'Building QEMU ({jobs} jobs)')
        if not run_logged(
                ['make', f'-j{jobs}'],
                log, 'make', cwd=build_dir):
            return 1

    tout.clear_progress()
    tout.notice(f'QEMU built in {build_dir} (log: {log_path})')
    return 0


# EFI firmware packages for QEMU
EFI_PACKAGES = [
    'ovmf',
    'ovmf-ia32',
    'qemu-efi-aarch64',
    'qemu-efi-arm',
    'qemu-efi-riscv64',
]


def setup_efi(args):
    """Check and install QEMU EFI firmware packages

    Args:
        args (argparse.Namespace): Command line arguments

    Returns:
        int: Exit code (0 for success, non-zero for failure)
    """
    missing = []
    for package in EFI_PACKAGES:
        try:
            command.output('dpkg', '-s', package)
        except command.CommandExc:
            missing.append(package)

    if not missing:
        tout.notice('All EFI packages are installed')
        return 0

    tout.notice(f'Missing EFI packages: {" ".join(missing)}')
    install_cmd = ['sudo', 'apt-get', 'install', '-y'] + missing

    if args.dry_run:
        tout.notice(f'Would run: {" ".join(install_cmd)}')
        return 0

    tout.notice('Installing missing packages (may require sudo password)...')
    result = command.run_pipe([install_cmd], capture=False,
                              raise_on_error=False)
    if result.return_code:
        tout.error('Failed to install EFI packages')
        tout.notice(f'Try running manually: {" ".join(install_cmd)}')
        return 1

    tout.notice('EFI packages installed')
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


# ldr tool for Analog Devices boards
ADI_LDR_REPO = 'https://github.com/analogdevicesinc/adsp-ldr.git'
ADI_LDR_VER = 'v1.0.2'

# CROSS_COMPILE prefixes used by supported ADI platforms
ADI_LDR_PREFIXES = ('arm-linux-gnueabi-ldr', 'aarch64-linux-ldr')


def setup_adi_ldr(args):
    """Clone and build the ldr tool for Analog Devices boards

    Clones adsp-ldr to ~/dev/adi-adsp-ldr, builds it with meson inside
    a Python venv and creates prefixed symlinks that match
    $(CROSS_COMPILE) on supported platforms. Output is logged to
    ~/dev/adi-adsp-ldr/build.log

    Args:
        args (argparse.Namespace): Command line arguments

    Returns:
        int: Exit code (0 for success, non-zero for failure)
    """
    ldr_dir = os.path.expanduser('~/dev/adi-adsp-ldr')
    build_dir = os.path.join(ldr_dir, 'build')
    log_path = os.path.join(ldr_dir, 'build.log')

    if os.path.exists(os.path.join(build_dir, 'ldr')) and not args.force:
        tout.notice(f'ldr already built: {build_dir}/ldr')
        tout.notice('Use --force to rebuild')
        return 0

    if args.dry_run:
        tout.notice(f'Would clone adsp-ldr {ADI_LDR_VER} to {ldr_dir}'
                    ' and build')
        return 0

    os.makedirs(os.path.dirname(ldr_dir), exist_ok=True)
    tmp_log = os.path.join(os.path.dirname(ldr_dir), 'adi-ldr-build.log')
    venv = os.path.join(ldr_dir, 'venv', 'bin')

    # Build the list of steps to run, then execute under one log handle
    steps = []
    if not os.path.isdir(os.path.join(ldr_dir, '.git')):
        steps.append((['git', 'clone', '--depth=1', '-b', ADI_LDR_VER,
                       ADI_LDR_REPO, ldr_dir], None, 'git clone'))
    if not os.path.exists(f'{venv}/meson'):
        steps.append((['python3', '-m', 'venv',
                       os.path.dirname(venv)], None, 'python -m venv'))
        steps.append(([f'{venv}/pip', 'install', 'meson'], None,
                      'pip install meson'))
    if not os.path.exists(os.path.join(build_dir, 'build.ninja')):
        steps.append(([f'{venv}/meson', 'setup', 'build'], ldr_dir,
                      'meson setup'))
    steps.append(([f'{venv}/meson', 'compile'], build_dir, 'meson compile'))

    # Log to a temp file until ldr_dir exists from the clone, then move
    failed = False
    with open(tmp_log, 'w', encoding='utf-8') as log:
        for cmd, cwd, desc in steps:
            tout.progress(desc)
            if not run_logged(cmd, log, desc, cwd=cwd):
                failed = True
                break
    if os.path.isdir(ldr_dir):
        shutil.move(tmp_log, log_path)
    if failed:
        return 1

    for name in ADI_LDR_PREFIXES:
        link = os.path.join(ldr_dir, name)
        if os.path.exists(link) or os.path.islink(link):
            os.remove(link)
        os.symlink('build/ldr', link)

    tout.clear_progress()
    tout.notice(f'ldr built: {build_dir}/ldr (log: {log_path})')
    tout.notice(f'Add to PATH: {ldr_dir}')
    return 0


RSYNC_EXCLUDES = [
    '.git/', '__pycache__/', '*.pyc', '*.pyo', '.pytest_cache/',
    '.benchmarks/', '.claude/', '.hypothesis/', 'mmc*.img', 'spi.bin', 'um',
]

REMOTE_UMAN_DIR = '~/dev/uman'
REMOTE_BIN = '~/bin'


def setup_remote(args):
    """Deploy uman to a remote machine via SSH

    Rsyncs the uman repo, creates ~/bin/um symlink, and runs
    setup aliases on the remote.

    Args:
        args (argparse.Namespace): Command line arguments
            args.host: SSH hostname (e.g. 'user@host' or 'host')

    Returns:
        int: Exit code (0 for success, non-zero for failure)
    """
    host = args.host
    if not host:
        tout.error('Hostname required: um setup remote <hostname>')
        return 1

    uman_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Step 1: rsync the uman repo to the remote
    rsync_cmd = ['rsync', '-az', '--delete']
    for excl in RSYNC_EXCLUDES:
        rsync_cmd += ['--exclude', excl]
    rsync_cmd += [uman_dir + '/', f'{host}:{REMOTE_UMAN_DIR}/']

    tout.notice(f'Syncing uman to {host}:{REMOTE_UMAN_DIR}...')
    if args.dry_run:
        tout.notice(f'  {" ".join(rsync_cmd)}')
    else:
        mkdir_cmd = ['ssh', host,
                     f'mkdir -p {REMOTE_UMAN_DIR} {REMOTE_BIN}']
        command.run_pipe([mkdir_cmd], capture=True)
        command.run_pipe([rsync_cmd], capture=False, raise_on_error=True)

    # Step 2: Create ~/bin/um symlink on the remote
    link = f'{REMOTE_UMAN_DIR}/uman_pkg/uman'
    ln_cmd = ['ssh', host, f'ln -sf {link} {REMOTE_BIN}/um']
    tout.notice(f'Creating symlink {REMOTE_BIN}/um on {host}...')
    if args.dry_run:
        tout.notice(f'  {" ".join(ln_cmd)}')
    else:
        command.run_pipe([ln_cmd], capture=True)

    # Step 3: Run setup aliases on the remote
    setup_cmd = ['ssh', host, f'{REMOTE_BIN}/um setup aliases -f']
    tout.notice(f'Setting up aliases on {host}...')
    if args.dry_run:
        tout.notice(f'  {" ".join(setup_cmd)}')
    else:
        command.run_pipe([setup_cmd], capture=False, raise_on_error=True)

    tout.notice(f'Remote setup complete on {host}')
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
    if not args.component:
        tout.notice('Available components:')
        for name, desc in SETUP_COMPONENTS.items():
            tout.notice(f'  {name}: {desc}')
        tout.notice("Use 'um setup <component>' or 'um setup all'")
        return 0

    if args.component == 'all':
        components = [c for c in SETUP_COMPONENTS if c != 'remote']
    elif args.component not in SETUP_COMPONENTS:
        tout.error(f'Unknown component: {args.component}')
        tout.notice('Use --list to see available components')
        return 1
    else:
        components = [args.component]

    # Dispatch table for component setup functions
    setup_funcs = {
        'aliases': lambda: setup_aliases(args),
        'adi-ldr': lambda: setup_adi_ldr(args),
        'efi': lambda: setup_efi(args),
        'gcc': lambda: setup_gcc(args),
        'qemu': lambda: setup_qemu(args),
        'qemu-build': lambda: setup_qemu_build(args),
        'opensbi': lambda: setup_opensbi(blobs_dir, args),
        'tfa': lambda: setup_tfa(blobs_dir, args),
        'xtensa': lambda: setup_xtensa(blobs_dir, args),
        'remote': lambda: setup_remote(args),
    }

    # Build each component
    for component in components:
        tout.notice(f'Setting up {component}...')
        result = setup_funcs[component]()
        if result:
            return result

    tout.notice('Setup complete')
    return 0
