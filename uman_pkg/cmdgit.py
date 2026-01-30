# SPDX-License-Identifier: GPL-2.0+
# Copyright 2025 Canonical Ltd
# Written by Simon Glass <simon.glass@canonical.com>

"""Git command for rebase helpers

This module handles the 'git' subcommand which provides interactive rebase
helpers similar to the rf/rn bash aliases.
"""

from collections import namedtuple
import os
import re

# pylint: disable=import-error
from u_boot_pylib import command
from u_boot_pylib import tout

from uman_pkg.util import git, git_output, git_output_quiet


def get_rebase_position():
    """Get current position in rebase (e.g., "3/12")

    Returns:
        str: Position string like "3/12", or empty string if not available
    """
    for subdir in ['rebase-merge', 'rebase-apply']:
        try:
            path = git_output('rev-parse', '--git-path', subdir)
            if os.path.isdir(path):
                msgnum_file = os.path.join(path, 'msgnum')
                end_file = os.path.join(path, 'end')
                if os.path.exists(msgnum_file) and os.path.exists(end_file):
                    with open(msgnum_file, encoding='utf-8') as inf:
                        msgnum = inf.read().strip()
                    with open(end_file, encoding='utf-8') as inf:
                        end = inf.read().strip()
                    return f'{msgnum}/{end}'
        except (command.CommandExc, OSError):
            pass
    return ''


def show_rebase_status(output, return_code=0):
    """Parse git rebase output and show a single-line status

    Args:
        output (str): Output from git rebase command (stderr or combined)
        return_code (int): Return code from git command
    """
    match = re.search(r'(Successfully rebased and updated [^.]+)', output)
    if match:
        tout.notice(match.group(1))
        return

    pos = get_rebase_position()
    pos_str = f' {pos}:' if pos else ':'

    match = re.search(r'Stopped at ([0-9a-f]+)\.\.\.\s+(.+)', output)
    if match:
        tout.notice(f'Rebasing{pos_str} stopped at {match.group(1)}... '
                    f'{match.group(2)}')
        return

    if return_code:
        match = re.search(r'Could not apply ([0-9a-f]+)\.\.\. (.+)', output)
        if match:
            tout.notice(f'Rebasing{pos_str} conflict in {match.group(1)}... '
                        f'{match.group(2)}')


def seq_edit_env(action, line=1):
    """Create environment with GIT_SEQUENCE_EDITOR set

    Args:
        action (str): 'break' to insert break, 'edit' to change pick to edit
        line (int): Line number to operate on (default 1)

    Returns:
        dict: Environment with GIT_SEQUENCE_EDITOR set
    """
    env = os.environ.copy()
    if action == 'break':
        env['GIT_SEQUENCE_EDITOR'] = f'sed -i "{line}i break"'
    else:  # edit
        env['GIT_SEQUENCE_EDITOR'] = f'sed -i "{line}s/^pick/edit/"'
    return env


def get_upstream():
    """Get the upstream branch name

    Returns:
        str: Upstream branch name, or None if not found
    """
    # Try @{upstream} first (use quiet version to suppress stderr)
    try:
        upstream = git_output_quiet('rev-parse', '--abbrev-ref', '@{upstream}')
        if upstream:
            return upstream
    except command.CommandExc:
        pass

    # Maybe we are in a rebase - try @{-1}
    try:
        upstream = git_output_quiet('rev-parse', '--abbrev-ref', '@{-1}')
        if upstream:
            tout.warning(f'Using upstream branch {upstream}')
            return upstream
    except command.CommandExc:
        pass

    return None


def get_rebase_dir():
    """Find the git rebase directory

    Returns:
        str: Path to rebase directory, or None if not in a rebase
    """
    try:
        path = git_output('rev-parse', '--git-path', 'rebase-merge')
        if os.path.isdir(path):
            return path
    except command.CommandExc:
        pass

    try:
        path = git_output('rev-parse', '--git-path', 'rebase-apply')
        if os.path.isdir(path):
            return path
    except command.CommandExc:
        pass

    return None


def do_gr(args):
    """Start interactive rebase to upstream, opening editor

    Args:
        args (argparse.Namespace): Arguments from cmdline
            args.arg: Number of commits back from HEAD, or None for upstream

    Returns:
        int: Exit code (0 for success, non-zero for failure)
    """
    if args.arg:
        target = f'HEAD~{args.arg}'
    else:
        target = get_upstream()
        if not target:
            tout.error('Cannot determine upstream branch')
            return 1

    if args.dry_run:
        tout.notice(f"git rebase -i {target}")
        return 0

    result = command.run_one('git', 'rebase', '-i', target, capture=False,
                             raise_on_error=False)
    return result.return_code


def do_rb(args):
    """Rebase from beginning - stop at upstream before first commit

    Args:
        args (argparse.Namespace): Arguments from cmdline

    Returns:
        CommandResult or int: Result with return_code, stdout, stderr; or 0
    """
    target = get_upstream()
    if not target:
        tout.error('Cannot determine upstream branch')
        return 1

    result = git('rebase', '-i', target, env=seq_edit_env('break'),
                 dry_run=args.dry_run)
    if result is None:
        return 0
    show_rebase_status(result.stdout + result.stderr, result.return_code)
    return result


def do_rf(args):
    """Start interactive rebase with first commit set to edit

    Args:
        args (argparse.Namespace): Arguments from cmdline
            args.arg: Number of commits back from HEAD, or None for upstream

    Returns:
        CommandResult or int: Result with return_code, stdout, stderr; or 0
    """
    if has_unstaged_changes():
        tout.error('Unstaged changes - commit or stash first')
        return 1

    if args.arg:
        target = f'HEAD~{args.arg}'
    else:
        target = get_upstream()
        if not target:
            tout.error('Cannot determine upstream branch')
            return 1

    result = git('rebase', '-i', target, env=seq_edit_env('edit'),
                 dry_run=args.dry_run)
    if result is None:
        return 0
    show_rebase_status(result.stdout + result.stderr, result.return_code)
    return result


def do_rp(args):
    """Rebase to upstream, stop at patch N for editing

    Args:
        args (argparse.Namespace): Arguments from cmdline
            args.arg: Patch number (0 = upstream, before first commit)

    Returns:
        CommandResult or int: Result with return_code, stdout, stderr; or 0
    """
    if args.arg is None:
        tout.error('Patch number required: um git rp N')
        return 1

    target = get_upstream()
    if not target:
        tout.error('Cannot determine upstream branch')
        return 1

    patch_num = int(args.arg)
    if patch_num == 0:
        env = seq_edit_env('break')
    else:
        env = seq_edit_env('edit', patch_num)

    result = git('rebase', '-i', target, env=env, dry_run=args.dry_run)
    if result is None:
        return 0
    show_rebase_status(result.stdout + result.stderr, result.return_code)
    return result


def has_conflicts():
    """Check if there are unresolved conflicts

    Returns:
        bool: True if there are conflicts (UU or AA in git status)
    """
    try:
        status = git_output('status', '--porcelain')
        for line in status.splitlines():
            if line.startswith('UU ') or line.startswith('AA '):
                return True
    except command.CommandExc:
        pass
    return False


def has_staged_changes():
    """Check if there are staged changes (index differs from HEAD)

    Returns:
        bool: True if there are staged changes
    """
    try:
        # --cached compares index to HEAD; exit code 1 means differences
        git_output('diff', '--cached', '--quiet')
        return False
    except command.CommandExc:
        return True


def has_unstaged_changes():
    """Check if there are unstaged changes (working tree differs from index)

    Returns:
        bool: True if there are unstaged changes
    """
    try:
        # Without --cached, compares working tree to index
        git_output('diff', '--quiet')
        return False
    except command.CommandExc:
        return True


def do_rn(args):
    """Continue rebase, setting next commit(s) to edit

    If there are unresolved conflicts, reports an error.
    If there are staged changes (just resolved a conflict), just continues.
    If stopped at an edit point, sets the next commit to edit and continues.

    Args:
        args (argparse.Namespace): Arguments from cmdline
            args.arg: Number of commits to skip (default 1)

    Returns:
        int: Exit code (0 for success, non-zero for failure)
    """
    rebase_dir = get_rebase_dir()
    if not rebase_dir:
        tout.error('Not in the middle of a git rebase')
        return 1

    # If there are conflicts, user must resolve them first
    if has_conflicts():
        tout.error('Resolve conflicts first, then use rn')
        return 1

    # If there are unstaged changes (but no staged), warn user
    if has_unstaged_changes() and not has_staged_changes():
        tout.error('Unstaged changes - use "git add" or "git checkout" first')
        return 1

    todo_file = os.path.join(rebase_dir, 'git-rebase-todo')
    if not os.path.exists(todo_file):
        tout.error('Rebase todo file not found')
        return 1

    # If there are staged changes, amend if at edit/break point, then continue
    if has_staged_changes():
        # Capture position before continuing (git will advance the counter)
        pos = get_rebase_position()

        with open(todo_file, 'r', encoding='utf-8') as inf:
            lines = inf.readlines()

        # Check if we're at an edit/break point vs conflict resolution
        # - amend file exists: at edit point
        # - last done line is 'break': at break point
        # - otherwise: conflict resolution (don't amend)
        amend_file = os.path.join(rebase_dir, 'amend')
        done_file = os.path.join(rebase_dir, 'done')

        at_edit_or_break = os.path.exists(amend_file)
        if not at_edit_or_break and os.path.exists(done_file):
            with open(done_file, 'r', encoding='utf-8') as inf:
                done_lines = [ln.strip() for ln in inf if ln.strip()]
            if done_lines and done_lines[-1].startswith('break'):
                at_edit_or_break = True

        if at_edit_or_break:
            # At edit/break point - amend the staged changes to HEAD first
            result = command.run_one('git', 'commit', '--amend', '--no-edit',
                                     capture=True, raise_on_error=False)
            if result.return_code != 0:
                tout.error('Failed to amend changes')
                return result

        # Insert break to stop for review
        lines.insert(0, 'break\n')
        with open(todo_file, 'w', encoding='utf-8') as outf:
            outf.writelines(lines)

        result = git('rebase', '--continue')
        if result.return_code == 0:
            # Use captured position (not git's inflated count from break)
            pos_str = f' {pos}:' if pos else ':'
            commit = git_output('rev-parse', '--short', 'HEAD')
            subject = git_output('log', '-1', '--format=%s')
            tout.notice(f'Rebasing{pos_str} review  {commit}... {subject}')
        else:
            show_rebase_status(result.stdout + result.stderr,
                               result.return_code)
        return result

    with open(todo_file, 'r', encoding='utf-8') as inf:
        lines = inf.readlines()

    # Find non-comment lines
    skip_count = int(args.arg) if args.arg else 1
    non_comment_indices = []
    for i, line in enumerate(lines):
        if line.strip() and not line.startswith('#'):
            non_comment_indices.append(i)
            if len(non_comment_indices) >= skip_count:
                break

    if non_comment_indices:
        # Change the last one to 'edit'
        target_idx = non_comment_indices[-1]
        lines[target_idx] = re.sub(r'^\S+', 'edit', lines[target_idx])

        with open(todo_file, 'w', encoding='utf-8') as outf:
            outf.writelines(lines)

    result = git('rebase', '--continue')
    show_rebase_status(result.stdout + result.stderr, result.return_code)
    return result

def do_rc(args):
    """Continue the current rebase

    Args:
        args (argparse.Namespace): Arguments from cmdline (unused)

    Returns:
        int or CommandResult: 1 if not rebasing, else CommandResult
    """
    del args  # unused
    if not get_rebase_dir():
        tout.error('Not in the middle of a rebase')
        return 1
    result = git('rebase', '--continue')
    show_rebase_status(result.stdout + result.stderr, result.return_code)
    return result

def do_rs(args):
    """Skip the current commit in rebase

    Args:
        args (argparse.Namespace): Arguments from cmdline (unused)

    Returns:
        int or CommandResult: 1 if not rebasing, else CommandResult
    """
    del args  # unused
    if not get_rebase_dir():
        tout.error('Not in the middle of a rebase')
        return 1
    result = git('rebase', '--skip')
    if result.return_code == 0:
        show_rebase_status(result.stdout + result.stderr)
    return result


def do_re(args):
    """Amend the current commit during rebase (rebase edit)

    Opens the editor to amend the commit message.

    Args:
        args (argparse.Namespace): Arguments from cmdline (unused)

    Returns:
        int: Exit code from git commit --amend
    """
    del args  # unused
    if not get_rebase_dir():
        tout.error('Not in the middle of a rebase')
        return 1

    result = command.run_one('git', 'commit', '--amend', capture=False,
                             raise_on_error=False)
    return result.return_code


def do_ra(args):
    """Abort the current rebase

    Args:
        args (argparse.Namespace): Arguments from cmdline (unused)

    Returns:
        int or CommandResult: 1 if not rebasing, else CommandResult
    """
    del args  # unused
    if not get_rebase_dir():
        tout.error('Not in the middle of a rebase')
        return 1

    # Get HEAD first for stash message and recovery info
    head = None
    try:
        head = git_output('rev-parse', '--short', 'HEAD')
    except command.CommandExc:
        pass

    # Stash uncommitted changes before aborting
    try:
        status = git_output('status', '--porcelain')
        if status:
            msg = f'uman-abort-{head}' if head else 'uman-abort'
            stash_result = git('stash', 'push', '-m', msg)
            if stash_result.return_code == 0:
                tout.notice(f'Stashed as "{msg}" (use "git stash pop" '
                            'to recover)')
            else:
                tout.warning('Could not stash changes - they may be lost')
        else:
            tout.notice('No uncommitted changes')
    except command.CommandExc:
        pass

    # Print current HEAD so user can recover if needed
    if head:
        tout.notice(f'Current HEAD: {head} (use "git reset --hard {head}" '
                    'to recover)')
    result = git('rebase', '--abort')
    if result.return_code == 0:
        tout.notice('Rebase aborted')
    return result


def do_et(args):
    """Edit the rebase todo list

    Args:
        args (argparse.Namespace): Arguments from cmdline (unused)

    Returns:
        int: Exit code from git rebase --edit-todo
    """
    del args  # unused
    if not get_rebase_dir():
        tout.error('Not in the middle of a rebase')
        return 1

    result = command.run_one('git', 'rebase', '--edit-todo', capture=False,
                             raise_on_error=False)
    return result.return_code


def do_us(args):
    """Set upstream branch for current branch

    Args:
        args (argparse.Namespace): Arguments from cmdline
            args.arg: Upstream branch name (default: m/master)

    Returns:
        int: Exit code (0 for success)
    """
    try:
        branch = git_output('rev-parse', '--abbrev-ref', 'HEAD')
    except command.CommandExc:
        tout.error('Cannot determine current branch')
        return 1

    upstream = args.arg or 'm/master'

    result = git('branch', '--set-upstream-to', upstream, branch)
    if result.return_code == 0:
        tout.notice(f'Set upstream of {branch} to {upstream}')
    else:
        tout.error(result.stderr.strip() if result.stderr else
                   'Failed to set upstream')
    return result.return_code


def do_pm(args):
    """Apply patch from rebase-apply directory

    Args:
        args (argparse.Namespace): Arguments from cmdline (unused)

    Returns:
        int: Exit code from patch command
    """
    del args  # unused
    rebase_dir = get_rebase_dir()
    if not rebase_dir:
        tout.error('Not in the middle of a rebase')
        return 1

    patch_file = os.path.join(rebase_dir, 'patch')
    if not os.path.exists(patch_file):
        tout.error('No patch file found in rebase directory')
        return 1

    with open(patch_file, 'r', encoding='utf-8') as patch_f:
        result = command.run_one('patch', '-p1', '--merge', capture=False,
                                 raise_on_error=False, stdin=patch_f)
    return result.return_code


def do_rd(args):
    """Show diff against the nth next commit in the rebase

    Args:
        args (argparse.Namespace): Arguments from cmdline
            args.arg: Which commit to diff against (default 1 = next commit),
                or a file path if not a digit
            args.extra: Additional arguments (e.g., file paths)

    Returns:
        int: Exit code from git diff
    """
    rebase_dir = get_rebase_dir()
    if not rebase_dir:
        tout.error('Not in the middle of a rebase')
        return 1

    todo_file = os.path.join(rebase_dir, 'git-rebase-todo')
    if not os.path.exists(todo_file):
        tout.error('Rebase todo file not found')
        return 1

    with open(todo_file, 'r', encoding='utf-8') as inf:
        lines = inf.readlines()

    # Parse args: if first arg is a digit, it's the commit number
    extra = list(args.extra) if args.extra else []
    if args.arg and args.arg.isdigit():
        target = int(args.arg)
    else:
        target = 1
        if args.arg:
            extra.insert(0, args.arg)

    # Find the nth non-comment, non-empty line
    count = 0
    commit_hash = None
    for line in lines:
        line = line.strip()
        if line and not line.startswith('#'):
            count += 1
            if count == target:
                # Line format: "pick abc1234 commit message"
                parts = line.split()
                if len(parts) >= 2:
                    commit_hash = parts[1]
                break

    if not commit_hash:
        tout.error(f'No commit found at position {target}')
        return 1

    # Show diff against that commit using difftool
    cmd = ['git', 'difftool', commit_hash]
    if extra:
        cmd.append('--')
        cmd.extend(extra)
    result = command.run_one(*cmd, capture=False, raise_on_error=False)
    return result.return_code


def do_ol(args):
    """Show oneline log of commits in current branch

    Shows commits from upstream to HEAD in oneline format with decoration.

    Args:
        args (argparse.Namespace): Arguments from cmdline
            args.arg: Number of commits, or file path, or None for all

    Returns:
        int: Exit code from git log
    """
    if args.arg and args.arg.isdigit():
        # Show last N commits
        cmd = ['git', 'log', '--oneline', '--decorate', f'-{args.arg}']
    else:
        # Show commits from upstream to HEAD
        upstream = get_upstream()
        if not upstream:
            tout.error('Cannot determine upstream branch')
            return 1
        cmd = ['git', 'log', '--oneline', '--decorate', f'{upstream}..']
        if args.arg:
            cmd.extend(['--', args.arg])

    result = command.run_one(*cmd, capture=False, raise_on_error=False)
    return result.return_code


def do_pe(_args):
    """Show last 10 commits in oneline format

    Returns:
        int: Exit code from git log
    """
    result = command.run_one('git', 'log', '--oneline', '-n10', '--decorate',
                             capture=False, raise_on_error=False)
    return result.return_code


def grep_branch(branch, count, upstream):
    """Check if commits from a branch are present in upstream

    Args:
        branch (str): Branch to check commits from
        count (int): Number of commits to check
        upstream (str): Upstream branch to search in

    Returns:
        int: 0 for success
    """
    # Get commit subjects from the branch
    try:
        subjects = git_output('log', f'-n{count}', '--format=%s', branch)
    except command.CommandExc as exc:
        tout.error(f'Cannot get commits from {branch}: {exc}')
        return 1

    # Get upstream log to search in
    try:
        upstream_log = git_output('log', '--oneline', '-n25000', upstream)
    except command.CommandExc as exc:
        tout.error(f'Cannot get log from {upstream}: {exc}')
        return 1

    tout.notice(f'Checking {branch} against {upstream}')
    for subject in subjects.splitlines():
        if not subject:
            continue
        if subject in upstream_log:
            print(f'\033[92mFound: {subject}\033[0m')
        else:
            print(f'\033[91mNot found: {subject}\033[0m')
    return 0


def do_fm(args):
    """Check if commits are in us/master

    Args:
        args (argparse.Namespace): Arguments from cmdline
            args.arg: Number of commits to check (default 5)

    Returns:
        int: Exit code
    """
    count = int(args.arg) if args.arg else 5
    try:
        branch = git_output('rev-parse', '--abbrev-ref', 'HEAD')
    except command.CommandExc:
        tout.error('Cannot determine current branch')
        return 1
    return grep_branch(branch, count, 'us/master')


def do_fn(args):
    """Check if commits are in us/next

    Args:
        args (argparse.Namespace): Arguments from cmdline
            args.arg: Number of commits to check (default 20)

    Returns:
        int: Exit code
    """
    count = int(args.arg) if args.arg else 20
    try:
        branch = git_output('rev-parse', '--abbrev-ref', 'HEAD')
    except command.CommandExc:
        tout.error('Cannot determine current branch')
        return 1
    return grep_branch(branch, count, 'us/next')


def do_fci(args):
    """Check if commits are in ci/master

    Args:
        args (argparse.Namespace): Arguments from cmdline
            args.arg: Number of commits to check (default 20)

    Returns:
        int: Exit code
    """
    count = int(args.arg) if args.arg else 20
    try:
        branch = git_output('rev-parse', '--abbrev-ref', 'HEAD')
    except command.CommandExc:
        tout.error('Cannot determine current branch')
        return 1
    return grep_branch(branch, count, 'ci/master')


def search_log(pattern, upstream):
    """Search upstream log for a pattern

    Args:
        pattern (str): Pattern to search for
        upstream (str): Branch to search (e.g. 'us/master')

    Returns:
        int: Exit code
    """
    if not pattern:
        tout.error('Pattern required: um git gm <pattern>')
        return 1

    try:
        log = git_output('log', '--oneline', upstream)
    except command.CommandExc as exc:
        tout.error(f'Cannot get log for {upstream}: {exc}')
        return 1

    for line in log.splitlines():
        if pattern.lower() in line.lower():
            print(line)
    return 0


def do_gm(args):
    """Search us/master log for a pattern

    Args:
        args (argparse.Namespace): Arguments from cmdline
            args.arg: Pattern to search for

    Returns:
        int: Exit code
    """
    return search_log(args.arg, 'us/master')


def do_gn(args):
    """Search us/next log for a pattern

    Args:
        args (argparse.Namespace): Arguments from cmdline
            args.arg: Pattern to search for

    Returns:
        int: Exit code
    """
    return search_log(args.arg, 'us/next')


def do_gci(args):
    """Search ci/master log for a pattern

    Args:
        args (argparse.Namespace): Arguments from cmdline
            args.arg: Pattern to search for

    Returns:
        int: Exit code
    """
    return search_log(args.arg, 'ci/master')


def do_eg(args):
    """Search errno.h for error codes

    Args:
        args (argparse.Namespace): Arguments from cmdline
            args.arg: Pattern to search for

    Returns:
        int: Exit code
    """
    if not args.arg:
        tout.error('Pattern required: um git eg <pattern>')
        return 1

    result = command.run_one('grep', args.arg, 'include/linux/errno.h',
                             capture=False, raise_on_error=False)
    return result.return_code


def do_sd(args):
    """Show a commit using difftool

    Args:
        args (argparse.Namespace): Arguments from cmdline
            args.arg: Commit reference (default HEAD)

    Returns:
        int: Exit code
    """
    commit = args.arg or 'HEAD'
    result = command.run_one('git', 'difftool', f'{commit}~..{commit}',
                             capture=False, raise_on_error=False)
    return result.return_code


def do_db(args):
    """Diff current commit against a branch

    Shows the changes in this commit, then runs difftool against a branch
    for only the files changed in this commit.

    Args:
        args.arg: Optional branch name to diff against (default: upstream)

    Returns:
        int: Exit code
    """
    # Get files changed in current commit
    try:
        numstat = git_output('log', '--numstat', '--pretty=format:', '-n1')
    except command.CommandExc as exc:
        tout.error(f'Cannot get commit changes: {exc}')
        return 1

    files = []
    for line in numstat.splitlines():
        if line.strip():
            parts = line.split()
            if len(parts) >= 3:
                files.append(parts[2])

    if not files:
        tout.error('No files changed in current commit')
        return 1

    # Show current commit summary
    print('Changes in this commit:')
    print()
    result = command.run_one('git', 'log', '--stat', '--oneline', '-n1',
                             capture=False, raise_on_error=False)
    if result.return_code:
        return result.return_code

    print()
    print('Performing diff against branch for changed files only')
    print(' '.join(files))

    # Get target branch
    target = args.arg if args.arg else get_upstream()
    if not target:
        tout.error('Cannot determine upstream branch')
        return 1

    # Run difftool for those files
    result = command.run_one('git', 'difftool', target, '--', *files,
                             capture=False, raise_on_error=False)
    return result.return_code


def do_am(_args):
    """Amend the current commit

    Returns:
        int: Exit code from git commit --amend
    """
    result = command.run_one('git', 'commit', '--amend', capture=False,
                             raise_on_error=False)
    return result.return_code


def do_ams(_args):
    """Amend the current commit with signoff

    Returns:
        int: Exit code from git commit --amend --signoff
    """
    result = command.run_one('git', 'commit', '--amend', '--signoff',
                             capture=False, raise_on_error=False)
    return result.return_code


def do_au(_args):
    """Add all changed files to staging

    Returns:
        int: Exit code from git add -u
    """
    result = command.run_one('git', 'add', '-u', capture=False,
                             raise_on_error=False)
    return result.return_code


def do_cm(_args):
    """Commit staged changes

    Returns:
        int: Exit code from git commit
    """
    result = command.run_one('git', 'commit', capture=False,
                             raise_on_error=False)
    return result.return_code


def do_cms(_args):
    """Commit staged changes with signoff

    Returns:
        int: Exit code from git commit --signoff
    """
    result = command.run_one('git', 'commit', '--signoff', capture=False,
                             raise_on_error=False)
    return result.return_code


def do_gb(_args):
    """List branches

    Returns:
        int: Exit code from git branch
    """
    result = command.run_one('git', 'branch', capture=False,
                             raise_on_error=False)
    return result.return_code


def do_gba(_args):
    """List all branches including remotes

    Returns:
        int: Exit code from git branch -a
    """
    result = command.run_one('git', 'branch', '-a', capture=False,
                             raise_on_error=False)
    return result.return_code


def do_gd(_args):
    """Show changes using difftool

    Returns:
        int: Exit code from git difftool
    """
    result = command.run_one('git', 'difftool', capture=False,
                             raise_on_error=False)
    return result.return_code


def do_gdc(_args):
    """Show staged changes using difftool

    Returns:
        int: Exit code from git difftool --cached
    """
    result = command.run_one('git', 'difftool', '--cached', capture=False,
                             raise_on_error=False)
    return result.return_code


def do_cs(_args):
    """Show the current commit

    Returns:
        int: Exit code from git show
    """
    result = command.run_one('git', 'show', capture=False,
                             raise_on_error=False)
    return result.return_code


def do_sc(_args):
    """Show the current commit with stats

    Returns:
        int: Exit code from git show --stat
    """
    result = command.run_one('git', 'show', '--stat', capture=False,
                             raise_on_error=False)
    return result.return_code


def do_g(_args):
    """Show short status

    Returns:
        int: Exit code from git status -sb
    """
    result = command.run_one('git', 'status', '-sb', capture=False,
                             raise_on_error=False)
    return result.return_code


def do_dh(args):
    """Show diff of the top commit using difftool

    Args:
        args (argparse.Namespace): Arguments from cmdline
            args.arg: Number of commits back (default 1), or file path
            args.extra: Additional arguments (e.g., file paths)

    Returns:
        int: Exit code from git difftool
    """
    extra = list(args.extra) if args.extra else []
    if args.arg and args.arg.isdigit():
        target = f'HEAD~{args.arg}'
    else:
        target = 'HEAD~'
        if args.arg:
            extra.insert(0, args.arg)

    cmd = ['git', 'difftool', target]
    if extra:
        cmd.append('--')
        cmd.extend(extra)
    result = command.run_one(*cmd, capture=False, raise_on_error=False)
    return result.return_code


def do_sl(args):
    """Show log with stats from upstream

    Args:
        args (argparse.Namespace): Arguments from cmdline
            args.arg: Number of commits, or file path, or None for all

    Returns:
        int: Exit code from git log --stat
    """
    if args.arg and args.arg.isdigit():
        cmd = ['git', 'log', '--stat', f'-{args.arg}']
    else:
        upstream = get_upstream()
        if not upstream:
            tout.error('Cannot determine upstream branch')
            return 1
        cmd = ['git', 'log', '--stat', f'{upstream}..']
        if args.arg:
            cmd.extend(['--', args.arg])

    result = command.run_one(*cmd, capture=False, raise_on_error=False)
    return result.return_code


def do_co(_args):
    """Checkout (switch branches or restore files)

    Returns:
        int: Exit code from git checkout
    """
    result = command.run_one('git', 'checkout', capture=False,
                             raise_on_error=False)
    return result.return_code


def do_st(_args):
    """Stash changes

    Returns:
        int: Exit code from git stash
    """
    result = command.run_one('git', 'stash', capture=False,
                             raise_on_error=False)
    return result.return_code


def do_ust(_args):
    """Pop stashed changes

    Returns:
        int: Exit code from git stash pop
    """
    result = command.run_one('git', 'stash', 'pop', capture=False,
                             raise_on_error=False)
    return result.return_code


# Git action definition: short name, long name, description, function
GitAction = namedtuple('GitAction', ['short', 'long', 'name', 'func'])

GIT_ACTIONS = [
    GitAction('am', 'amend', 'Amend the current commit', do_am),
    GitAction('ams', 'amend-signoff', 'Amend with signoff', do_ams),
    GitAction('au', 'add-update', 'Add changed files to staging', do_au),
    GitAction('cm', 'commit', 'Commit staged changes', do_cm),
    GitAction('cms', 'commit-signoff', 'Commit with signoff', do_cms),
    GitAction('co', 'checkout', 'Checkout (switch branches/restore)', do_co),
    GitAction('db', 'diff-branch', 'Diff commit files against upstream', do_db),
    GitAction('dh', 'diff-head', 'Show diff of top commit', do_dh),
    GitAction('eg', 'errno-grep', 'Search errno.h for error codes', do_eg),
    GitAction('et', 'edit-todo', 'Edit rebase todo list', do_et),
    GitAction('g', 'status', 'Show short status', do_g),
    GitAction('fci', 'find-ci', 'Check commits against ci/master', do_fci),
    GitAction('fm', 'find-master', 'Check commits against us/master', do_fm),
    GitAction('fn', 'find-next', 'Check commits against us/next', do_fn),
    GitAction('gb', 'branch', 'List branches', do_gb),
    GitAction('gba', 'branch-all', 'List all branches including remotes', do_gba),
    GitAction('gci', 'grep-ci', 'Search ci/master log for pattern', do_gci),
    GitAction('gd', 'difftool', 'Show changes using difftool', do_gd),
    GitAction('gdc', 'difftool-cached', 'Show staged changes', do_gdc),
    GitAction('gm', 'grep-master', 'Search us/master log for pattern', do_gm),
    GitAction('gn', 'grep-next', 'Search us/next log for pattern', do_gn),
    GitAction('gr', 'git-rebase', 'Start interactive rebase', do_gr),
    GitAction('cs', 'commit-show', 'Show the current commit', do_cs),
    GitAction('ol', 'oneline-log', 'Show oneline log of commits', do_ol),
    GitAction('pe', 'peek', 'Show last 10 commits', do_pe),
    GitAction('pm', 'patch-merge', 'Apply patch from rebase-apply', do_pm),
    GitAction('ra', 'rebase-abort', 'Abort the current rebase', do_ra),
    GitAction('rb', 'rebase-beginning', 'Rebase from beginning', do_rb),
    GitAction('rc', 'rebase-continue', 'Continue the current rebase', do_rc),
    GitAction('rd', 'rebase-diff', 'Show diff against next commit', do_rd),
    GitAction('re', 'rebase-edit', 'Amend current commit in rebase', do_re),
    GitAction('rf', 'rebase-first', 'Start rebase, edit first commit', do_rf),
    GitAction('rn', 'rebase-next', 'Continue rebase, edit next commit', do_rn),
    GitAction('rp', 'rebase-patch', 'Stop at patch N for editing', do_rp),
    GitAction('rs', 'rebase-skip', 'Skip current commit in rebase', do_rs),
    GitAction('sc', 'show-commit', 'Show commit with stats', do_sc),
    GitAction('sd', 'show-diff', 'Show a commit using difftool', do_sd),
    GitAction('sl', 'stat-log', 'Show log with stats from upstream', do_sl),
    GitAction('st', 'stash', 'Stash changes', do_st),
    GitAction('us', 'set-upstream', 'Set upstream branch', do_us),
    GitAction('ust', 'unstash', 'Pop stashed changes', do_ust),
]

# Build lookup dicts from the action list
ACTIONS = {a.short: a.func for a in GIT_ACTIONS}
ACTION_ALIASES = {a.long: a.short for a in GIT_ACTIONS}

# Simple commands that can be shell aliases (short_name -> git command)
SIMPLE_ALIASES = {
    'am': 'git commit --amend',
    'ams': 'git commit --amend -s',
    'au': 'git add -u',
    'cm': 'git commit',
    'cms': 'git commit -s',
    'co': 'git checkout',
    'cs': 'git show',
    'dh': 'git difftool HEAD~',
    'g': 'git status -sb',
    'gb': 'git branch',
    'gba': 'git branch -a',
    'gd': 'git difftool',
    'gdc': 'git difftool --cached',
    'pe': 'git log --oneline -n10 --decorate',
    'rc': 'git rebase --continue',
    'rs': 'git rebase --skip',
    'sc': 'git show --stat',
    'st': 'git stash',
    'ust': 'git stash pop',
}


def print_aliases():
    """Print shell alias definitions for simple git commands

    Returns:
        int: Exit code (always 0)
    """
    print('# Git aliases - add to ~/.bashrc: eval "$(um git -a)"')
    for name, cmd in sorted(SIMPLE_ALIASES.items()):
        print(f"alias {name}='{cmd}'")
    # Shell function for cg to pass $b environment variable
    print("cg() { b=\"$b\" command cg \"$@\"; }")
    return 0


def run(args):
    """Handle git subcommand

    Args:
        args (argparse.Namespace): Arguments from cmdline

    Returns:
        int: Exit code (0 for success, non-zero for failure)
    """
    if args.aliases:
        return print_aliases()

    if not args.action:
        tout.error('Action required (or use -a for aliases)')
        return 1

    # Resolve alias to short name
    action = ACTION_ALIASES.get(args.action, args.action)

    func = ACTIONS.get(action)
    if func:
        result = func(args)
        # Functions may return int or CommandResult
        if hasattr(result, 'return_code'):
            return result.return_code
        return result

    tout.error(f'Unknown action: {args.action}')
    return 1
