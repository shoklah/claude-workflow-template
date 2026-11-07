#!/usr/bin/env python3
"""Prove that a task's new tests fail without the task's change.

Control: run the tests on the final code (HEAD plus the working tree's changes). They must pass,
otherwise the environment can't run them and the result is "couldnt_run".
Check: run the same test files on the code at --base. They must fail, otherwise the result is "failed".

Both runs happen in temporary git worktrees under the system temp folder, so the working tree is
never touched. Prints one JSON object. Exit codes: passed 0, failed and no_tests 1, usage error 2,
couldnt_run 3.

Usage: failing-test-check.py --base SHA [--repo DIR] [--test-file PATH]... [--link PATH]...
                             [--timeout SECONDS] -- TEST_COMMAND [ARGS...]
"""
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile

HANDLED_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
TEST_FILE_PATTERN = re.compile(r'(^|/)(tests?|__tests__|spec)/|(_test|\.test|\.spec|_spec)\.|(^|/)test_[^/]+$')
EXIT_CODES = {'passed': 0, 'failed': 1, 'no_tests': 1, 'couldnt_run': 3}
TAIL_LINES = 40
KILL_GRACE_SECONDS = 5
CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
# Repo hooks (post-checkout and the like) must not run in the temp copies.
GIT = ['git', '-c', 'core.hooksPath=/dev/null']


class UsageError(Exception):
    pass


class CouldntRun(Exception):
    pass


class Interrupted(Exception):
    pass


def git(cwd, *args):
    return subprocess.run(GIT + list(args), cwd=cwd, env=CLEAN_ENV, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True)


def git_out(cwd, *args):
    r = git(cwd, *args)
    if r.returncode != 0:
        raise CouldntRun('git %s failed: %s' % (args[0], r.stderr.strip()))
    return r.stdout


def git_paths(cwd, *args):
    return [p for p in git_out(cwd, *args).split('\0') if p]


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog='failing-test-check.py',
        usage='%(prog)s --base SHA [--repo DIR] [--test-file PATH]... [--link PATH]... [--timeout SECONDS]'
              ' -- TEST_COMMAND [ARGS...]',
        description='Prove that the new tests pass on the final code and fail on the code at --base.')
    parser.add_argument('--base', required=True, help='the commit the task started from')
    parser.add_argument('--repo', default='.', help='the checkout to test (default: the current folder)')
    parser.add_argument('--test-file', action='append', default=[],
                        help='a test file, relative to the repo root; replaces detection (repeatable)')
    parser.add_argument('--link', action='append', default=[],
                        help='a path, relative to the repo root, to symlink into each temp copy (repeatable)')
    parser.add_argument('--timeout', type=float, default=600, help='seconds allowed for each test run (default 600)')
    if '--' not in argv:
        parser.error('missing -- TEST_COMMAND')
    split = argv.index('--')
    opts = parser.parse_args(argv[:split])
    opts.command = argv[split + 1:]
    if not opts.command:
        parser.error('missing TEST_COMMAND after --')
    if opts.timeout <= 0:
        parser.error('--timeout must be positive')
    return opts


def repo_relative(toplevel, path, option):
    """A path inside the repo, relative to its root, or a UsageError."""
    if os.path.isabs(path):
        parent = os.path.realpath(os.path.dirname(path))
        path = os.path.relpath(os.path.join(parent, os.path.basename(path)), toplevel)
    rel = os.path.normpath(path)
    if rel == '.' or rel == '..' or rel.startswith('../') or os.path.isabs(rel):
        raise UsageError('%s %s is not inside the repo' % (option, path))
    return rel


def remove(path):
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)


def is_real_dir(path):
    return os.path.isdir(path) and not os.path.islink(path)


def place(src_root, dst_root, rel):
    """Copy src_root/rel to dst_root/rel, replacing whatever is in the way."""
    dst = os.path.join(dst_root, rel)
    current = dst_root
    for part in rel.split('/')[:-1]:
        current = os.path.join(current, part)
        if os.path.lexists(current) and not is_real_dir(current):
            os.unlink(current)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    remove(dst)
    src = os.path.join(src_root, rel)
    if is_real_dir(src):
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst, follow_symlinks=False)


def changed_paths(repo, commit):
    """Paths that differ between commit and the working tree, untracked files included.

    A path that is a real folder in the working tree is a submodule, so it is left out.
    """
    tracked = git_paths(repo, 'diff', '--name-only', '--no-renames', '-z', commit, '--')
    untracked = git_paths(repo, 'ls-files', '-z', '-o', '--exclude-standard')
    return sorted(p for p in set(tracked) | set(untracked) if not is_real_dir(os.path.join(repo, p)))


def detect_test_files(repo, base):
    """Test files changed since base that still exist in the final code."""
    return [p for p in changed_paths(repo, base)
            if TEST_FILE_PATTERN.search(p) and os.path.lexists(os.path.join(repo, p))]


def overlay_working_tree(repo, copy):
    """Make a HEAD copy match the working tree: changed and untracked files copied, deleted ones removed."""
    for rel in changed_paths(repo, 'HEAD'):
        if os.path.lexists(os.path.join(repo, rel)):
            place(repo, copy, rel)
        else:
            remove(os.path.join(copy, rel))


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


def add_worktree(repo, tmp, name, commit, worktrees):
    path = os.path.join(tmp, '%s-%s' % (name, os.path.basename(tmp).rsplit('-', 1)[-1]))
    worktrees.append(path)  # recorded first, so a half-made worktree is still cleaned up
    r = git(repo, 'worktree', 'add', '-q', '--detach', path, commit)
    if r.returncode != 0:
        raise CouldntRun('could not make the %s copy: %s' % (name, r.stderr.strip()))
    return path


def admin_dir(path, common_dir):
    """The worktree's folder inside the repo's git dir."""
    try:
        with open(os.path.join(path, '.git')) as f:
            line = f.read().strip()
        if line.startswith('gitdir: '):
            return line[len('gitdir: '):]
    except OSError:
        pass
    return os.path.join(common_dir, 'worktrees', os.path.basename(path))


def remove_worktrees(repo, common_dir, worktrees):
    for path in worktrees:
        admin = admin_dir(path, common_dir)
        git(repo, 'worktree', 'remove', '--force', '--force', path)
        shutil.rmtree(path, ignore_errors=True)
        # If git could not remove it, drop its registration by hand, but only if it is ours.
        try:
            with open(os.path.join(admin, 'gitdir')) as f:
                registered = f.read().strip()
        except OSError:
            continue
        if os.path.realpath(os.path.dirname(registered)) == os.path.realpath(path):
            shutil.rmtree(admin, ignore_errors=True)


def kill_group(pgid):
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def tail(output):
    return '\n'.join(output.decode('utf-8', 'replace').splitlines()[-TAIL_LINES:])


def run_tests(command, cwd, timeout):
    """Run the command in its own process group. Returns (returncode or None on timeout, output tail)."""
    try:
        proc = subprocess.Popen(command, cwd=cwd, env=dict(CLEAN_ENV, PWD=cwd), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as e:
        raise CouldntRun('could not start the test command: %s' % e)
    output = b''
    timed_out = False
    try:
        try:
            output, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_group(proc.pid)
            try:
                output, _ = proc.communicate(timeout=KILL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                pass  # something outside the group still holds the output open
    finally:
        # Also on normal exit and on interruption: nothing the tests started may outlive them.
        kill_group(proc.pid)
        proc.stdout.close()
        proc.wait()
    return (None if timed_out else proc.returncode), tail(output)


def check(opts, repo, base, named, result):
    """Fill in result. Temp worktrees are removed before this returns or raises."""
    test_files = named or detect_test_files(repo, base)
    result['test_files'] = test_files
    if not test_files:
        result.update(result='no_tests', reason='no test files changed since %s' % opts.base)
        return
    common_dir = os.path.join(repo, git_out(repo, 'rev-parse', '--git-common-dir').strip())
    tmp = None
    worktrees = []

    def run_copy(name, copy, where):
        """Run the tests in one temp copy, record <name>_rc and <name>_tail, and treat a timeout as couldn't run."""
        add_links(repo, copy, opts.link)
        rc, result[name + '_tail'] = run_tests(opts.command, copy, opts.timeout)
        result[name + '_rc'] = rc
        if rc is None:
            raise CouldntRun('the tests timed out after %gs on %s' % (opts.timeout, where))
        return rc

    try:
        tmp = os.path.realpath(tempfile.mkdtemp(prefix='failing-test-check-'))
        control = add_worktree(repo, tmp, 'control', 'HEAD', worktrees)
        overlay_working_tree(repo, control)
        missing = [f for f in test_files if not os.path.lexists(os.path.join(control, f))]
        if missing:
            raise CouldntRun('test files not in the final code: %s' % ', '.join(missing))
        rc = run_copy('control', control, 'the final code')
        if rc != 0:
            raise CouldntRun('the tests fail on the final code (exit %d), so this environment cannot run them '
                             '(pass --link for ignored dependencies)' % rc)

        check_copy = add_worktree(repo, tmp, 'check', base, worktrees)
        for f in test_files:
            place(control, check_copy, f)
        rc = run_copy('check', check_copy, 'the code before the change')
        if rc == 0:
            result.update(result='failed', reason='the tests also pass on the code before the change, so they do not test it')
        else:
            result.update(result='passed', reason='the tests pass on the final code and fail before the change (exit %d)' % rc)
    finally:
        for sig in HANDLED_SIGNALS:
            signal.signal(sig, signal.SIG_IGN)  # finish cleaning up whatever arrives now
        remove_worktrees(repo, common_dir, worktrees)
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def interrupt(signum, frame):
    raise Interrupted('interrupted by signal %d' % signum)


def main(argv):
    opts = parse_args(argv)
    try:
        top = git(opts.repo, 'rev-parse', '--show-toplevel')
        if top.returncode != 0:
            raise UsageError('--repo %s is not a git checkout' % opts.repo)
        repo = os.path.realpath(top.stdout.strip())
        base = git(repo, 'rev-parse', '--verify', '--quiet', opts.base + '^{commit}')
        if base.returncode != 0:
            raise UsageError('--base %s is not a commit' % opts.base)
        named = sorted(set(repo_relative(repo, p, '--test-file') for p in opts.test_file))
        opts.link = [repo_relative(repo, p, '--link') for p in opts.link]
    except (UsageError, OSError) as e:
        sys.stderr.write('failing-test-check.py: error: %s\n' % e)
        return 2

    result = {'result': 'couldnt_run', 'reason': '', 'test_files': [], 'control_rc': None, 'check_rc': None,
              'control_tail': '', 'check_tail': ''}
    for sig in HANDLED_SIGNALS:
        signal.signal(sig, interrupt)
    try:
        check(opts, repo, base.stdout.strip(), named, result)
    except (CouldntRun, Interrupted) as e:
        result.update(result='couldnt_run', reason=str(e))
    except Exception as e:  # a crash still reports, after the worktrees are gone
        result.update(result='couldnt_run', reason='internal error: %s: %s' % (type(e).__name__, e))
    print(json.dumps(result))
    return EXIT_CODES[result['result']]


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
