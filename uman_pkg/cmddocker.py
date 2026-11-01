# SPDX-License-Identifier: GPL-2.0+
# Copyright 2025 Canonical Ltd
# Written by Simon Glass <simon.glass@canonical.com>

"""Docker command for running U-Boot tests in a CI Docker container

This module handles the 'docker' subcommand which runs U-Boot pytest
tests inside the same Docker image used by CI.
"""

import os

# pylint: disable=import-error
from u_boot_pylib import command
from u_boot_pylib import tout

from uman_pkg import gitlab_parser
from uman_pkg import util


# Template key in .gitlab-ci.yml
CI_TEMPLATE = '.buildman_and_testpy_template'

# CI variable substitutions (variable name -> default value)
CI_SUBS = {
    'CI_PROJECT_DIR': '/source',
    'OVERRIDE': '',
    'BUILD_ENV': '',
    'TEST_PY_ID': '',
    'TEST_PY_EXTRA': '',
}


def load_ci_yaml(uboot_dir):
    """Load and parse .gitlab-ci.yml from a U-Boot tree

    Args:
        uboot_dir (str): Path to U-Boot source directory

    Returns:
        dict: Parsed YAML data, or None if not found
    """
    ci_path = os.path.join(uboot_dir, '.gitlab-ci.yml')
    if not os.path.exists(ci_path):
        tout.error(f'Cannot find {ci_path}')
        return None

    with open(ci_path, 'r', encoding='utf-8') as inf:
        return gitlab_parser.load_yaml(inf)


def get_ci_image(data):
    """Extract the CI Docker image from parsed .gitlab-ci.yml data

    Looks for the default image and expands ${MIRROR_DOCKER} to
    docker.io.

    Args:
        data (dict): Parsed YAML data

    Returns:
        str: Full Docker image path, or None if not found
    """
    image = data.get('image')
    if not image:
        default = data.get('default', {})
        image = default.get('image') if isinstance(default, dict) else None
    if not image:
        tout.error('Cannot find image in .gitlab-ci.yml')
        return None

    return image.replace('${MIRROR_DOCKER}', 'docker.io')


def get_ci_script(data):
    """Extract before_script and script from the CI test template

    Args:
        data (dict): Parsed YAML data

    Returns:
        tuple: (before_script, script) as lists of shell command strings,
            or (None, None) if not found
    """
    template = data.get(CI_TEMPLATE)
    if not template:
        tout.error(f'Cannot find {CI_TEMPLATE} in .gitlab-ci.yml')
        return None, None

    before = template.get('before_script', [])
    script = template.get('script', [])
    return before, script


def build_script(data, board, test_spec, adjust_cfg=None,
                 pytest_args=None, gdb=False):
    """Generate the shell script to run inside the Docker container

    Parses the before_script and script sections from .gitlab-ci.yml
    and applies variable substitutions for the given board and test spec.

    Args:
        data (dict): Parsed YAML data
        board (str): Board name (e.g. 'sandbox')
        test_spec (str or None): pytest -k filter spec
        adjust_cfg (list or None): Kconfig adjustments for buildman -a
        pytest_args (list or None): Extra pytest flags (e.g. ['-x', '-s'])
        gdb (bool): Install gdbserver in the container

    Returns:
        str: Shell script to pass to bash -c, or None on error
    """
    before, script = get_ci_script(data)
    if before is None:
        return None

    # Build substitution map; put -a flags into OVERRIDE so they
    # are appended to the buildman command line
    override = ''
    if adjust_cfg:
        override = ' '.join(f'-a {cfg}' for cfg in adjust_cfg)
    subs = dict(CI_SUBS)
    subs['OVERRIDE'] = override
    subs['TEST_PY_BD'] = board
    subs['TEST_PY_EXTRA'] = ' '.join(pytest_args) if pytest_args else ''

    # Apply substitutions to each command; set test spec as shell
    # variables so bash handles ${VAR:+...} expansions natively
    spec = test_spec or ''
    commands = ['set -e']
    if gdb:
        commands.append(
            'which gdbserver >/dev/null 2>&1 ||'
            ' (apt-get update -qq && apt-get install -y -qq gdbserver)')
    commands.extend(['mkdir -p test/hooks/bin test/hooks/py',
                     f'export TEST_PY_TEST_SPEC="{spec}"',
                     f'export TEST_SPEC="{spec}"'])
    # Insert gdbserver wrapper just before test.py (the last command
    # in script). This wraps the main u-boot binary, not SPL, so gdb
    # doesn't need to follow the SPL-to-u-boot exec.
    gdb_wrapper = []
    if gdb:
        gdb_wrapper = [
            'bd=$UBOOT_TRAVIS_BUILD_DIR',
            'mv $bd/u-boot $bd/u-boot.real',
            'printf \'#!/bin/bash\\n'
            'exec gdbserver :1234 '
            '"$(dirname "$0")/u-boot.real" "$@"\\n\''
            ' > $bd/u-boot',
            'chmod +x $bd/u-boot',
        ]

    all_cmds = before + script
    for i, cmd in enumerate(all_cmds):
        for var, val in subs.items():
            cmd = cmd.replace(f'${{{var}}}', val)
        if gdb_wrapper and i == len(all_cmds) - 1:
            commands.extend(gdb_wrapper)
        commands.append(cmd)

    return '\n'.join(commands)


def run(args):
    """Run the docker command

    Args:
        args (argparse.Namespace): Parsed arguments

    Returns:
        int: Exit code (0 for success, non-zero for failure)
    """
    uboot_dir = util.get_uboot_dir()
    if not uboot_dir:
        tout.error('Not in a U-Boot tree and $USRC not set')
        return 1

    # Load CI config
    data = load_ci_yaml(uboot_dir)
    if not data:
        return 1

    # Determine Docker image
    image = args.image
    if not image:
        image = get_ci_image(data)
        if not image:
            return 1

    board = args.board

    uid_gid = command.output_one_line('id', '-u') + ':' + \
        command.output_one_line('id', '-g')

    docker_cmd = ['docker', 'run', '--rm',
                  '-e', 'HOME=/tmp',
                  '-v', '/etc/passwd:/etc/passwd:ro',
                  '-v', f'{uboot_dir}:/source',
                  '-w', '/source']

    # Run as root when gdbserver is needed so we can install it;
    # otherwise run as current user for correct file ownership
    if args.gdb_phase:
        docker_cmd.extend(['--user', '0:0',
                           '--cap-add=SYS_PTRACE',
                           '-p', '1234:1234'])
        tout.notice('When tests stall, connect from another terminal:')
        tout.notice(f'   um py -G -B {board}')
        tout.notice('Type c <enter> to continue; tests then proceed')
    else:
        docker_cmd.extend(['--user', uid_gid])

    docker_cmd.append(image)

    if args.interactive:
        docker_cmd.append('bash')
        tout.notice(f'Starting interactive shell in {image}...')
    else:
        tout.notice(f'Building and testing {board}...')
        spec = ' '.join(args.test_spec) if args.test_spec else None
        extra = []
        if args.exitfirst:
            extra.append('-x')
        if args.show_output:
            extra.append('-s')

        # For SPL debugging, pass --gdbserver to test.py so it wraps
        # the initial binary (SPL); for u-boot debugging, use a wrapper
        # script so gdbserver starts after SPL exec's the main binary
        gdb = bool(args.gdb_phase)
        if args.gdb_phase and args.gdb_phase != 'u-boot':
            extra.extend(['--gdbserver', 'localhost:1234'])
            gdb = False
        script = build_script(data, board, spec, args.adjust_cfg,
                              extra or None, gdb=gdb)
        if not script:
            return 1
        docker_cmd.extend(['bash', '-c', script])

    result = util.exec_cmd(docker_cmd, args.dry_run, capture=False)
    if result and result.return_code:
        return result.return_code

    return 0
