#!/usr/bin/env python3
"""Run a wave of plan tasks at the same time, each in its own git worktree.

start: make one worktree per task at <toplevel>/.worktrees/task-N, on a new branch <run branch>-task-N cut from
       --base, with each --link path symlinked in from the checkout and kept out of git status.
merge: merge the task branches into the run branch, in the order given.
finish: remove the tasks' worktrees and branches, once every one is merged and clean.

Prints one JSON object. start prints {"run_branch", "base", "tasks": [{"task", "branch", "path"}]}; merge prints
{"run_branch", "merged": [N], "conflict": null | {"task", "files"}} and stops at the first conflict, leaving it in
progress, while any other failed merge is a refusal carrying git's message (the tasks before it stay merged); finish
prints {"removed": [{"task", "branch", "path"}]}, or {"error", "unmerged": [N]} with nothing removed; any other
refusal, a task worktree with uncommitted changes included, prints {"error"}. Exit codes: done 0, a merge conflict or
the git state isn't what the command expects 1 (refusals change nothing else), usage error 2 (on stderr, nothing on
stdout).

Usage: wave.py start --base SHA [--repo DIR] [--link PATH]... TASK...
       wave.py merge [--repo DIR] TASK...
       wave.py finish [--repo DIR] TASK...
"""
import argparse
import json
import os
import subprocess
import sys

# CLEAN_ENV through add_links are copied from scripts/failing-test-check.py, since scripts can't import each other:
# keep them the same.
CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
# Repo hooks (post-checkout and the like) must not run in the temp copies.
GIT = ['git', '-c', 'core.hooksPath=/dev/null']


class UsageError(Exception):
    pass


class CouldntRun(Exception):
    pass


def git(cwd, *args):
    return subprocess.run(GIT + list(args), cwd=cwd, env=CLEAN_ENV, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True)


def repo_relative(toplevel, path, option):
    """A path inside the repo, relative to its root, or a UsageError."""
    if os.path.isabs(path):
        parent = os.path.realpath(os.path.dirname(path))
        path = os.path.relpath(os.path.join(parent, os.path.basename(path)), toplevel)
    rel = os.path.normpath(path)
    if rel == '.' or rel == '..' or rel.startswith('../') or os.path.isabs(rel):
        raise UsageError('%s %s is not inside the repo' % (option, path))
    return rel


def add_links(repo, copy, links):
    for rel in links:
        src = os.path.join(repo, rel)
        dst = os.path.join(copy, rel)
        if not os.path.lexists(src):
            raise CouldntRun('--link %s does not exist in the checkout' % rel)
        if os.path.lexists(dst):
            raise CouldntRun('--link %s is already in the temp copy, so it is not an ignored path' % rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.symlink(src, dst)


def parse_args(argv):
    parser = argparse.ArgumentParser(prog='wave.py', description='Run a wave of plan tasks, each in its own worktree.')
    subs = parser.add_subparsers(dest='command', required=True)
    for name, text in (('start', 'make a worktree and branch per task from --base'),
                       ('merge', 'merge the task branches into the run branch, in order'),
                       ('finish', "remove the merged tasks' worktrees and branches")):
        sub = subs.add_parser(name, help=text, description=text)
        sub.add_argument('--repo', default='.', help='the checkout (default: the current folder)')
        if name == 'start':
            sub.add_argument('--base', required=True, help='the commit the wave starts from')
            sub.add_argument('--link', action='append', default=[],
                             help='a path, relative to the repo root, to symlink into each worktree (repeatable)')
        sub.add_argument('tasks', metavar='TASK', type=int, nargs='+', help='a task number from the plan')
    return parser.parse_args(argv)


def repo_state(opts):
    """The checkout's root and the branch it is on, or a UsageError."""
    top = git(opts.repo, 'rev-parse', '--show-toplevel')
    if top.returncode != 0:
        raise UsageError('--repo %s is not a git checkout' % opts.repo)
    toplevel = os.path.realpath(top.stdout.strip())
    branch = git(toplevel, 'symbolic-ref', '--short', 'HEAD')
    if branch.returncode != 0:
        raise UsageError('HEAD is detached in %s: check out the run branch first' % toplevel)
    return toplevel, branch.stdout.strip()


def task_branch(run_branch, n):
    return f'{run_branch}-task-{n}'


def task_path(toplevel, n):
    return os.path.join(toplevel, '.worktrees', f'task-{n}')


def branch_exists(toplevel, branch):
    return git(toplevel, 'rev-parse', '--verify', '--quiet', 'refs/heads/' + branch).returncode == 0


def check_branches(toplevel, run_branch, tasks):
    """A CouldntRun naming the first task branch that doesn't exist."""
    for n in tasks:
        if not branch_exists(toplevel, task_branch(run_branch, n)):
            raise CouldntRun('branch %s does not exist' % task_branch(run_branch, n))


def exclude_links(toplevel, links):
    """Add /<link> to the repo's info/exclude for each link it doesn't list yet, so git status ignores the symlinks."""
    exclude = os.path.join(toplevel, git(toplevel, 'rev-parse', '--git-path', 'info/exclude').stdout.strip())
    text = ''
    if os.path.exists(exclude):
        with open(exclude) as f:
            text = f.read()
    listed = text.splitlines()
    new = ['/' + rel for rel in links if '/' + rel not in listed]
    if not new:
        return
    os.makedirs(os.path.dirname(exclude), exist_ok=True)
    with open(exclude, 'a') as f:
        f.write(('\n' if text and not text.endswith('\n') else '') + ''.join(line + '\n' for line in new))


def start(opts, toplevel, run_branch):
    base = git(toplevel, 'rev-parse', '--verify', '--quiet', opts.base + '^{commit}')
    if base.returncode != 0:
        raise UsageError('--base %s is not a commit' % opts.base)
    base = base.stdout.strip()
    links = list(dict.fromkeys(repo_relative(toplevel, p, '--link') for p in opts.link))
    for rel in links:
        if not os.path.lexists(os.path.join(toplevel, rel)):
            raise UsageError('--link %s does not exist in the checkout' % rel)
        if git(toplevel, 'ls-tree', '--name-only', base, '--', rel).stdout:
            raise UsageError('--link %s is tracked at the base, so it is not an ignored path' % rel)

    problems = []
    for n in opts.tasks:
        branch, path = task_branch(run_branch, n), task_path(toplevel, n)
        if branch_exists(toplevel, branch):
            problems.append('branch %s already exists' % branch)
        if os.path.lexists(path):
            problems.append('%s already exists' % path)
    if problems:
        return {'error': '; '.join(problems)}, 1

    ignore = os.path.join(toplevel, '.worktrees', '.gitignore')
    os.makedirs(os.path.dirname(ignore), exist_ok=True)
    if not os.path.lexists(ignore):
        with open(ignore, 'w') as f:
            f.write('*\n')
    tasks = []
    for n in opts.tasks:
        branch, path = task_branch(run_branch, n), task_path(toplevel, n)
        r = git(toplevel, 'worktree', 'add', '-q', '-b', branch, path, base)
        if r.returncode != 0:
            raise CouldntRun('could not make the worktree for task-%d: %s' % (n, r.stderr.strip()))
        add_links(toplevel, path, links)
        tasks.append({'task': n, 'branch': branch, 'path': path})
    exclude_links(toplevel, links)
    return {'run_branch': run_branch, 'base': base, 'tasks': tasks}, 0


def merge(opts, toplevel, run_branch):
    check_branches(toplevel, run_branch, opts.tasks)
    if git(toplevel, 'rev-parse', '-q', '--verify', 'MERGE_HEAD').returncode == 0:
        return {'error': 'a merge is in progress in %s: finish or abort it first' % toplevel}, 1
    if git(toplevel, 'status', '--porcelain', '--untracked-files=no').stdout:
        return {'error': '%s has uncommitted changes: commit or discard them first' % toplevel}, 1
    merged = []
    for n in opts.tasks:
        r = git(toplevel, 'merge', '--no-ff', '--no-edit', '-m', 'merge: task %d' % n, task_branch(run_branch, n))
        if r.returncode != 0:
            if git(toplevel, 'rev-parse', '-q', '--verify', 'MERGE_HEAD').returncode != 0:
                raise CouldntRun(r.stderr.strip())
            files = git(toplevel, 'diff', '--name-only', '--diff-filter=U').stdout.splitlines()
            return {'run_branch': run_branch, 'merged': merged, 'conflict': {'task': n, 'files': files}}, 1
        merged.append(n)
    return {'run_branch': run_branch, 'merged': merged, 'conflict': None}, 0


def finish(opts, toplevel, run_branch):
    check_branches(toplevel, run_branch, opts.tasks)
    unmerged = [n for n in opts.tasks
                if git(toplevel, 'merge-base', '--is-ancestor', task_branch(run_branch, n), 'HEAD').returncode != 0]
    if unmerged:
        names = ', '.join(task_branch(run_branch, n) for n in unmerged)
        return {'error': 'not merged into %s yet: %s' % (run_branch, names), 'unmerged': unmerged}, 1
    paths = [task_path(toplevel, n) for n in opts.tasks]
    dirty = [p for p in paths if os.path.isdir(p) and git(p, 'status', '--porcelain').stdout]
    if dirty:
        return {'error': 'uncommitted changes in %s: commit or discard them first' % ', '.join(dirty)}, 1
    removed = []
    for n, path in zip(opts.tasks, paths):
        branch = task_branch(run_branch, n)
        # A folder that is already gone is fine: git just drops its registration.
        for args in (('worktree', 'remove', path), ('branch', '-d', branch)):
            r = git(toplevel, *args)
            if r.returncode != 0:
                raise CouldntRun('could not remove task-%d: %s' % (n, r.stderr.strip()))
        removed.append({'task': n, 'branch': branch, 'path': path})
    return {'removed': removed}, 0


def main(argv):
    opts = parse_args(argv)
    try:
        toplevel, run_branch = repo_state(opts)
        result, code = {'start': start, 'merge': merge, 'finish': finish}[opts.command](opts, toplevel, run_branch)
    except (UsageError, OSError) as e:
        sys.stderr.write('wave.py: error: %s\n' % e)
        return 2
    except CouldntRun as e:
        print(json.dumps({'error': str(e)}))
        return 1
    print(json.dumps(result))
    return code


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
