# SPDX-License-Identifier: GPL-2.0+
# Copyright 2025 Canonical Ltd
# Written by Simon Glass <simon.glass@canonical.com>

"""Handles the main control logic of uman

This module provides various functions called by the main program to implement
the features of uman.
"""

import sys

# pylint: disable=import-error
from u_boot_pylib import command
from u_boot_pylib import terminal
from u_boot_pylib import tout

from uman_pkg import settings
from uman_pkg.util import exec_cmd

# Heavy imports are done lazily in the functions that need them:
# - gitlab: do_merge_request()
# - patman.patchstream: extract_mr_info()
# - u_boot_pylib.gitutil: extract_mr_info(), do_merge_request(), do_ci()
# - uman_pkg.gitlab_parser: validate_ci_args()
# - uman_pkg.build: run_command() for 'build'
# - uman_pkg.cmdconfig: run_command() for 'config'
# - uman_pkg.cmdgit: run_command() for 'git'
# - uman_pkg.cmdpy: run_command() for 'pytest'
# - uman_pkg.cmdtest: run_command() for 'test'
# - uman_pkg.setup: run_command() for 'setup'


def build_ci_vars(args):
    """Build CI variables based on command line arguments

    Args:
        args (argparse.Namespace): Arguments object with CI flags

    Returns:
        dict: Dictionary of CI variables and their values
    """
    ci_vars = {
        'SUITES': '0',
        'PYTEST': '0',
        'WORLD': '0',
        'SJG_LAB': ''
    }

    if not args.null:
        ci_flags_set = (args.suites or args.pytest or args.world or
                       args.sjg or args.test_spec)

        if args.all:
            ci_vars['SUITES'] = '1'
            ci_vars['PYTEST'] = '1'
            ci_vars['WORLD'] = '1'
            ci_vars['SJG_LAB'] = '1'
        elif not ci_flags_set:
            ci_vars['SUITES'] = '1'
            ci_vars['PYTEST'] = '1'
            ci_vars['WORLD'] = '1'
        else:
            if args.suites:
                ci_vars['SUITES'] = '1'
            # Use 'is not None' for args with nargs='?' to distinguish between
            # not provided (None) and provided with default value ('1')
            if args.pytest is not None:
                ci_vars['PYTEST'] = args.pytest
                # Pytest jobs depend on the tools suite
                ci_vars['SUITES'] = '1'
            if args.world:
                ci_vars['WORLD'] = '1'
            if args.sjg is not None:
                ci_vars['SJG_LAB'] = args.sjg
            if args.test_spec:
                ci_vars['TEST_SPEC'] = args.test_spec

    return ci_vars


def build_commit_tags(args, ci_vars):  # pylint: disable=unused-argument
    """Build commit message tags based on CI variables for MR pipelines

    Args:
        args (argparse.Namespace): Arguments object with CI flags
        ci_vars (dict): CI variables dictionary

    Returns:
        str: Space-separated commit message tags
    """
    tags = []

    # Add skip tags for variables set to '0' or empty
    if ci_vars.get('SUITES') == '0':
        tags.append('[skip-suites]')
    if ci_vars.get('PYTEST') == '0':
        tags.append('[skip-pytest]')
    if ci_vars.get('WORLD') == '0':
        tags.append('[skip-world]')
    if ci_vars.get('SJG_LAB') in ('0', ''):
        tags.append('[skip-sjg]')

    return ' '.join(tags)


def build_desc(desc, tags):
    """Append commit message tags to MR description

    Args:
        desc (str): Original description (empty string if no description)
        tags (str): Space-separated tags to append

    Returns:
        str: Description with tags appended
    """
    if not tags:
        return desc

    if desc:
        return f'{desc}\n\n{tags}'
    return tags


def git_push_branch(branch, args, ci_vars=None, upstream=False, dest=None):
    """Push a branch to the 'ci' remote with optional CI variables

    Args:
        branch (str): Branch name to push
        args (argparse.Namespace): Command line arguments (contains force,
            dry_run, dest flags)
        ci_vars (dict): Optional CI variables to include as push options
        upstream (bool): Whether to set upstream with -u flag
        dest (str): Destination branch name (defaults to args.dest or
            current branch name)

    Returns:
        CommandResult or None: Result of push command
    """
    push_cmd = ['git', 'push']

    if args.force:
        push_cmd.append('--force')

    if upstream:
        push_cmd.append('-u')

    if ci_vars:
        for key, value in ci_vars.items():
            push_cmd.extend(['-o', f'ci.variable={key}={value}'])

    # Determine destination branch - use provided dest, fall back to
    # args.dest, or current branch
    dest_branch = dest or args.dest or branch

    # Always push to 'ci' remote, but to the specified destination branch
    if dest_branch == branch:
        # Same branch name, simple push
        push_cmd.extend(['ci', branch])
    else:
        # Different branch name, use refspec
        push_cmd.extend(['ci', f'{branch}:{dest_branch}'])

    return exec_cmd(push_cmd, args.dry_run)


def show_pytest_choices(parser):
    """Show all available pytest choices (boards + job names)

    Args:
        parser (GitLabCIParser): GitLabCIParser instance

    Returns:
        int: Exit code (always 0)
    """
    tout.notice('Available pytest targets:')
    tout.notice('')
    tout.notice('Special values:')
    tout.notice('  1                    - Run all pytest jobs')
    tout.notice('')
    tout.notice('Board names (targets all jobs for that board):')
    for board in parser.boards:
        tout.notice(f'  {board}')

    tout.notice('')
    tout.notice('Job names (targets specific job variant):')
    for job in parser.job_names:
        tout.notice(f'  {job}')
    return 0


def show_sjg_choices(parser):
    """Show all available SJG_LAB choices

    Args:
        parser (GitLabCIParser): GitLabCIParser instance

    Returns:
        int: Exit code (always 0)
    """
    tout.notice('Available SJG_LAB targets:')
    tout.notice('')
    tout.notice('Special values:')
    tout.notice('  1                    - Run all lab jobs')
    tout.notice('  (empty)              - Manual lab jobs only')
    tout.notice('')
    tout.notice('Lab names:')
    for role in parser.roles:
        tout.notice(f'  {role}')

    return 0


def validate_pytest_value(value, parser):
    """Validate a pytest value against available choices

    Args:
        value (str): Value to validate
        parser (GitLabCIParser): GitLabCIParser instance

    Returns:
        bool: True if valid, False otherwise
    """
    if value in ('1', 'help'):
        return True
    return value in parser.boards or value in parser.job_names


def validate_sjg_value(value, parser):
    """Validate an SJG_LAB value against available choices

    Args:
        value (str): Value to validate
        parser (GitLabCIParser): GitLabCIParser instance

    Returns:
        bool: True if valid, False otherwise
    """
    if value in ('1', '', 'help'):
        return True
    return value in parser.roles


def validate_ci_args(args):
    """Validate CI arguments and handle help requests

    Args:
        args (argparse.Namespace): Arguments from cmdline

    Returns:
        int: Exit code (0 for success, non-zero for failure, None to continue)
    """
    # pylint: disable=import-outside-toplevel
    from uman_pkg.gitlab_parser import GitLabCIParser

    # Parse GitLab CI file once for validation and help requests
    parser = GitLabCIParser()

    # Handle help requests
    if args.pytest == 'help':
        return show_pytest_choices(parser)
    if args.sjg == 'help':
        return show_sjg_choices(parser)

    # Validate pytest argument
    if args.pytest is not None:
        if not validate_pytest_value(args.pytest, parser):
            tout.error(f'Invalid pytest value: {args.pytest}')
            tout.notice(f'To see available choices: {sys.argv[0]} ci -p help')
            return 1

    # Validate sjg argument
    if args.sjg is not None:
        if not validate_sjg_value(args.sjg, parser):
            tout.error(f'Invalid SJG_LAB value: {args.sjg}')
            tout.notice(f'To see available choices: {sys.argv[0]} ci -l help')
            return 1

    # All validation passed
    return None


def run_command(args):  # pylint: disable=R0911
    """Run the appropriate command based on parsed arguments

    Args:
        args (argparse.Namespace): Arguments from cmdline

    Returns:
        int: Exit code (0 for success, non-zero for failure)
    """
    # pylint: disable=import-outside-toplevel

    # Set verbosity level
    tout.init(tout.INFO if args.verbose else tout.NOTICE)

    # Set up terminal color support
    args.col = terminal.Color()

    # Ensure settings file exists
    settings.get_all()

    tout.info(f'Running command: {args.cmd}')

    if args.cmd == 'build':
        from uman_pkg import build
        return build.run(args)

    if args.cmd == 'ci':
        # Validate CI arguments and handle help requests
        result = validate_ci_args(args)
        if result is not None:
            return result

        if args.merge:
            return do_merge_request(args)
        return do_ci(args)

    if args.cmd == 'config':
        from uman_pkg import cmdconfig
        return cmdconfig.run(args)

    if args.cmd == 'git':
        from uman_pkg import cmdgit
        return cmdgit.run(args)

    if args.cmd == 'pytest':
        from uman_pkg.cmdpy import do_pytest
        return do_pytest(args)

    if args.cmd == 'setup':
        from uman_pkg.setup import do_setup
        return do_setup(args)

    if args.cmd == 'test':
        from uman_pkg.cmdtest import do_test
        return do_test(args)

    tout.error(f'Unknown command: {args.cmd}')
    return 1


def extract_mr_info(branch, args):
    """Extract title and description for merge request from patch series

    Args:
        branch (str): Current git branch name
        args (argparse.Namespace): Arguments from cmdline

    Returns:
        tuple: (title_str, description_str, commit_tags) or
            (None, None, None) if error
    """
    # pylint: disable=import-outside-toplevel
    from patman import patchstream
    from u_boot_pylib import gitutil

    start = 0
    end = 0

    # Work out how many patches to send if we can
    count = gitutil.count_commits_to_branch(branch) - start
    series = patchstream.get_metadata(branch, start, count - end)

    # For single commit, use commit subject/body; for multiple commits,
    # require cover letter
    if count - end == 1:
        # Single commit - use the commit subject as title and body as
        # description
        commit = series.commits[0]
        title = commit.subject
        desc = commit.msg.splitlines() if commit.msg else []
        tout.info('Using single commit subject and body for merge request')
    else:
        # Multiple commits - require cover letter
        cover = series.cover
        if not cover:
            tout.error('No cover letter found in patch series')
            tout.notice('Use \'git format-patch --cover-letter\' or add a '
                        'cover letter to your series')
            return None, None, None
        title = cover[0]  # pylint: disable=unsubscriptable-object
        desc = cover[1:]  # pylint: disable=unsubscriptable-object
        tout.info('Using cover letter for merge request')

    tout.info(f'Found {count - end} patches for branch {branch}')

    if not title:
        tout.error('Could not extract title')
        return None, None, None

    # Build CI variables for pipeline creation
    ci_vars = build_ci_vars(args)
    # When creating MR, append commit message tags for pipeline control
    commit_tags = ''
    if hasattr(args, 'merge') and args.merge:
        commit_tags = build_commit_tags(args, ci_vars)
    desc_with_tags = build_desc('\n'.join(desc), commit_tags)

    return title, desc_with_tags, commit_tags


def do_merge_request(args):  # pylint: disable=too-many-locals
    """Create a merge request using cover letter from patch series

    Args:
        args (argparse.Namespace): Arguments from cmdline

    Returns:
        int: Exit code
    """
    # pylint: disable=import-outside-toplevel
    import gitlab
    from pickman import gitlab_api
    from u_boot_pylib import gitutil

    tout.info('Creating merge request from patch series...')

    # Get branch and extract title/description
    branch = gitutil.get_branch()
    mr_info = extract_mr_info(branch, args)
    title, desc, commit_tags = mr_info
    if title is None:
        return 1

    # Get remote URL and parse it using pickman's functions
    remote_url = gitlab_api.get_remote_url('ci')
    host, proj = gitlab_api.parse_url(remote_url)
    if not host or not proj:
        tout.error(f'Cannot parse remote URL: {remote_url}')
        return 1

    # Check if MR already exists for this branch
    existing_mr = None
    try:
        token = gitlab_api.get_token()
        if token:
            glab = gitlab.Gitlab(f'https://{host}', private_token=token)
            project = glab.projects.get(proj)
            mrs = project.mergerequests.list(source_branch=branch,
                                             state='opened')
            if mrs:
                existing_mr = mrs[0]
    except gitlab.GitlabError as exc:
        tout.error(f'Could not check for existing MR: {exc}')
        return 1

    if args.dry_run:
        tout.notice(f'dry-run: Create MR \'{title}\'')
        return 0

    # Push branch with CI variables - respects --null flag
    tout.info('Pushing branch...')
    ci_vars = build_ci_vars(args)
    git_push_branch(branch, args, ci_vars=ci_vars)

    if existing_mr:
        # Update existing MR
        tout.info('Updating existing merge request...')
        existing_mr.title = title
        existing_mr.description = desc
        existing_mr.save()
        mr_url = existing_mr.web_url
        tout.notice(f'Merge request updated: {mr_url}')
    else:
        # Create new MR
        tout.info('Creating merge request...')
        mr_url = gitlab_api.create_mr(host, proj, branch, 'master',
                                      title, desc)
        if not mr_url:
            tout.error('Failed to create merge request')
            return 1
        tout.notice(f'Merge request: {mr_url}')
    if commit_tags:
        tout.info(f'MR pipeline will use commit message tags: {commit_tags}')

    return 0


def do_ci(args):
    """Handle CI command - push current branch to trigger CI

    Args:
        args (argparse.Namespace): Arguments from cmdline

    Returns:
        int: Exit code
    """
    branch = command.output_one_line('git', 'branch', '--show-current')

    if not branch:
        tout.error('Could not determine current branch')
        return 1

    tout.info(f'Current branch: {branch}')

    ci_vars = build_ci_vars(args)

    git_push_branch(branch, args, ci_vars=ci_vars)

    return 0
