# SPDX-License-Identifier: GPL-2.0+
# Copyright 2026 Canonical Ltd
# Written by Simon Glass <simon.glass@canonical.com>

"""Pytest plugin to stop after a named test completes"""

import pytest


def pytest_addoption(parser):
    parser.addoption('--stop-at', action='store', default=None,
                     help='Stop after the named test completes')


def pytest_collection_modifyitems(config, items):
    stop_at = config.getoption('--stop-at')
    if not stop_at:
        return
    for i, item in enumerate(items):
        if stop_at in item.nodeid:
            del items[i + 1:]
            return
    pytest.exit(f'--stop-at: no test matching {stop_at!r}', returncode=2)
