# SPDX-License-Identifier: GPL-2.0+
# Copyright 2025 Canonical Ltd
# Written by Simon Glass <simon.glass@canonical.com>

"""Settings handling for uman

This module provides access to the uman configuration file (~/.uman).
"""

import configparser
import os

# pylint: disable=import-error
from u_boot_pylib import tout

# Default config file content
DEFAULT_CONFIG = '''# uman config file

[DEFAULT]
# Build directory for U-Boot out-of-tree builds
build_dir = /tmp/b

# Git remote for CI pushes (default: ci)
# ci_remote = ci

# Map upstream remotes to push remotes (comma-separated from:to pairs)
# e.g. if upstream is 'us' but you push to 'dm': ci_remote_map = us:dm
# ci_remote_map = us:dm

# Directory for firmware blobs (OpenSBI, etc.)
blobs_dir = ~/dev/blobs

# Emscripten SDK, used to build sandbox as WebAssembly (default: ~/dev/emsdk)
# emsdk_dir = ~/dev/emsdk

# OPENSBI firmware paths for RISC-V testing (built by 'uman setup')
opensbi = ~/dev/blobs/opensbi/fw_dynamic.bin
opensbi_rv32 = ~/dev/blobs/opensbi/fw_dynamic_rv32.bin

# QEMU build directory (built by 'uman setup qemu-build')
qemu_build_dir = ~/dev/qemu/build

# TF-A firmware directory for ARM SBSA testing (built by 'uman setup')
tfa_dir = ~/dev/blobs/tfa

# U-Boot test hooks directory
test_hooks = /vid/software/devel/ubtest/u-boot-test-hooks
'''

# Global settings storage
SETTINGS = {'config': None}


def get_all():
    """Get or create the global settings instance

    Returns:
        configparser.ConfigParser: Settings object
    """
    if SETTINGS['config'] is None:
        SETTINGS['config'] = configparser.ConfigParser()
        fname = os.path.expanduser('~/.uman')
        if not os.path.exists(fname):
            tout.warning(f'Creating config file: {fname}')
            with open(fname, 'w', encoding='utf-8') as fil:
                fil.write(DEFAULT_CONFIG)
        SETTINGS['config'].read(fname)
    return SETTINGS['config']


def get(name, fallback=None):
    """Get a setting by name

    Args:
        name (str): Name of setting to retrieve
        fallback (str or None): Value to return if the setting is missing

    Returns:
        str or None: Setting value with ~ and env vars expanded, or fallback
    """
    cfg = get_all()
    raw = cfg.get('DEFAULT', name, fallback=fallback)
    if raw is None:
        return None
    return os.path.expandvars(os.path.expanduser(raw))
