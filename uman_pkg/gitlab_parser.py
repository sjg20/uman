# SPDX-License-Identifier: GPL-2.0+
# Copyright 2025 Canonical Ltd
# Written by Simon Glass <simon.glass@canonical.com>

"""GitLab CI file parser for uman

This module parses the GitLab CI YAML file to extract valid values
for SJG_LAB roles and TEST_PY_BD boards.
"""

import os
import re
from pathlib import Path

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False


def find_gitlab_ci_file():
    """Find the GitLab CI file in the U-Boot source tree

    Returns:
        str: Path to .gitlab-ci.yml file, or None if not found
    """
    # Try common locations relative to current directory
    candidates = [
        '.gitlab-ci.yml',
        '../.gitlab-ci.yml',
        '../../.gitlab-ci.yml',
        Path.home() / 'u/.gitlab-ci.yml'
    ]

    # Also check USRC environment variable
    usrc = os.environ.get('USRC')
    if usrc:
        candidates.insert(0, os.path.join(usrc, '.gitlab-ci.yml'))

    for candidate in candidates:
        path = Path(candidate).resolve()
        if path.exists():
            return str(path)

    return None


class GitLabCIParser:
    """Parser for GitLab CI configuration files

    Parses the .gitlab-ci.yml file once and provides access to extracted data
    through properties. This is more efficient than parsing the file multiple
    times.
    """

    def __init__(self, filepath=None):
        """Initialise parser and parse the GitLab CI file

        Args:
            filepath (str): Path to .gitlab-ci.yml file. If None, auto-detect.
        """
        self._filepath = filepath or find_gitlab_ci_file()
        self._roles = []
        self._boards = []
        self._job_names = []
        self._parse_file()

    def _parse_file(self):
        """Parse the GitLab CI file to extract roles, boards, and job names"""
        if not self._filepath or not os.path.exists(self._filepath):
            return

        roles = set()
        boards = set()
        job_names = set()

        # Read file content once
        with open(self._filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        # Try YAML parsing first if available
        if YAML_AVAILABLE:
            data = yaml.safe_load(content)

            # Walk through all jobs to find ROLE, TEST_PY_BD values and
            # pytest job names
            for job_name, job_config in data.items():
                if not isinstance(job_config, dict):
                    continue

                # Collect pytest job names (jobs ending with 'test.py')
                if isinstance(job_name, str) and job_name.endswith('test.py'):
                    job_names.add(job_name)

                # Look for variables section
                variables = job_config.get('variables')
                if not isinstance(variables, dict):
                    continue

                if 'ROLE' in variables:
                    roles.add(str(variables['ROLE']))
                if 'TEST_PY_BD' in variables:
                    boards.add(str(variables['TEST_PY_BD']).strip('"'))

        # If YAML is not available, use regex fallback
        else:
            # Extract ROLE values - look for "ROLE: value" patterns
            role_matches = re.findall(r'^[ \t]*ROLE:\s*([a-zA-Z0-9_-]+)',
                                    content, re.MULTILINE)
            roles.update(role_matches)

            # Extract TEST_PY_BD values - look for "TEST_PY_BD: value" patterns
            board_matches = re.findall(r'^[ \t]*TEST_PY_BD:\s*"([^"]+)"',
                                     content, re.MULTILINE)
            boards.update(board_matches)

            # Extract pytest job names - look for lines ending with "test.py:"
            job_matches = re.findall(r'^([^#\s][^:]*test\.py):', content,
                                     re.MULTILINE)
            job_names.update(job_matches)

        # Store sorted lists
        self._roles = sorted(list(roles))
        self._boards = sorted(list(boards))
        self._job_names = sorted(list(job_names))

    @property
    def roles(self):
        """Get list of valid SJG_LAB role values"""
        return self._roles

    @property
    def boards(self):
        """Get list of valid TEST_PY_BD board values"""
        return self._boards

    @property
    def job_names(self):
        """Get list of valid pytest job names"""
        return self._job_names
