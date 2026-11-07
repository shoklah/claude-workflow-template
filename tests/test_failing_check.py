"""Tests for the failing-test check. They run the real script against real throwaway git repos in a temp folder.

Run with: /usr/bin/python3 -I tests/test_failing_check.py
"""
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
# The script sits in ../scripts in the repo and in .. once installed to ~/.claude/scripts/tests.
SCRIPTS_DIR = os.path.join(HERE, '..', 'scripts') if os.path.isdir(os.path.join(HERE, '..', 'scripts')) else os.path.join(HERE, '..')
SCRIPT = os.path.join(SCRIPTS_DIR, 'failing-test-check.py')
PYTHON = '/usr/bin/python3'
CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
CHECK_SH = 'test -f feature.txt\n'


def git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True, env=CLEAN_ENV).stdout


def make_repo(path):
    os.makedirs(path)
    git(path, 'init', '-q', '-b', 'main')
    git(path, 'config', 'user.email', 't@example.test')
    git(path, 'config', 'user.name', 't')
    git(path, 'config', 'commit.gpgsign', 'false')


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)


def commit(repo, message):
    git(repo, 'add', '-A')
    git(repo, 'commit', '-q', '--allow-empty', '-m', message)
    return git(repo, 'rev-parse', 'HEAD').strip()


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class FailingCheck(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix='failing-check-test-'))
        # The script's own temp folders go here, so the tests can see whether any are left behind.
        self.systmp = os.path.join(self.root, 'sys tmp')
        os.makedirs(self.systmp)
        self.repo = os.path.join(self.root, 'repo')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_script(self, args, cwd=None, env=None, timeout=60):
        full_env = dict(CLEAN_ENV, TMPDIR=self.systmp)
        full_env.update(env or {})
        p = subprocess.run([PYTHON, '-I', SCRIPT, *args], cwd=cwd or self.repo, capture_output=True, text=True,
                           env=full_env, timeout=timeout)
        self.last_rc = p.returncode
        self.last_stderr = p.stderr
        return json.loads(p.stdout) if p.stdout.strip() else None

    def build(self, base_has_feature, head_has_feature, repo=None):
        """Base commit, then a head commit that adds tests/check.sh and sets feature.txt as asked."""
        repo = repo or self.repo
        make_repo(repo)
        write(os.path.join(repo, 'README'), 'readme\n')
        if base_has_feature:
            write(os.path.join(repo, 'feature.txt'), 'feature\n')
        base = commit(repo, 'base')
        write(os.path.join(repo, 'tests', 'check.sh'), CHECK_SH)
        if head_has_feature:
            write(os.path.join(repo, 'feature.txt'), 'feature v2\n')
        elif os.path.exists(os.path.join(repo, 'feature.txt')):
            os.remove(os.path.join(repo, 'feature.txt'))
        commit(repo, 'head')
        return base

    def check(self, base_has_feature, head_has_feature, extra=(), command=('sh', 'tests/check.sh')):
        base = self.build(base_has_feature, head_has_feature)
        return self.run_script(['--base', base, *extra, '--', *command])

    def snapshot(self, repo=None):
        repo = repo or self.repo
        return (git(repo, 'status', '--porcelain', '--untracked-files=all'),
                git(repo, 'worktree', 'list', '--porcelain'))

    def assertNothingLeftBehind(self, repo=None):
        self.assertEqual(os.listdir(self.systmp), [])
        self.assertEqual(git(repo or self.repo, 'worktree', 'list', '--porcelain').count('worktree '), 1)

    def assertProcessGone(self, pid_file):
        with open(pid_file) as f:
            child = int(f.read())
        deadline = time.time() + 5
        while pid_alive(child) and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(pid_alive(child), 'the test command\'s child process outlived the run')

    # The brief's cases.

    def test_new_tests_that_fail_without_the_change_pass_the_check(self):
        out = self.check(base_has_feature=False, head_has_feature=True)
        self.assertEqual(out['result'], 'passed')
        self.assertEqual((out['control_rc'], out['test_files'], self.last_rc), (0, ['tests/check.sh'], 0))
        self.assertNotEqual(out['check_rc'], 0)
        self.assertNothingLeftBehind()

    def test_tests_that_also_pass_before_the_change_fail_the_check(self):
        out = self.check(base_has_feature=True, head_has_feature=True)
        self.assertEqual((out['result'], out['control_rc'], out['check_rc']), ('failed', 0, 0))
        self.assertEqual(self.last_rc, 1)

    def test_tests_failing_on_the_final_code_mean_couldnt_run(self):
        out = self.check(base_has_feature=False, head_has_feature=False)
        self.assertEqual(out['result'], 'couldnt_run')
        self.assertEqual((out['control_rc'], out['check_rc'], self.last_rc), (1, None, 3))
        self.assertIn('pass --link for ignored dependencies', out['reason'])

    def test_a_missing_command_means_couldnt_run_with_exit_3(self):
        out = self.check(base_has_feature=False, head_has_feature=True, command=('/nonexistent/test-runner',))
        self.assertEqual((out['result'], out['control_rc'], out['check_rc'], self.last_rc), ('couldnt_run', None, None, 3))
        self.assertNothingLeftBehind()

    def test_a_timeout_means_couldnt_run_and_leaves_nothing_behind(self):
        pid_file = os.path.join(self.root, 'child.pid')
        before = None
        base = self.build(base_has_feature=False, head_has_feature=True)
        before = self.snapshot()
        started = time.time()
        out = self.run_script(['--base', base, '--timeout', '1', '--',
                               'sh', '-c', 'sleep 30 & echo $! > "$1"; wait', 'sh', pid_file])
        self.assertLess(time.time() - started, 15)
        self.assertEqual((out['result'], out['control_rc'], self.last_rc), ('couldnt_run', None, 3))
        self.assertIn('timed out', out['reason'])
        self.assertEqual(self.snapshot(), before)
        self.assertNothingLeftBehind()
        self.assertProcessGone(pid_file)

    def test_no_test_files_changed_is_no_tests(self):
        make_repo(self.repo)
        write(os.path.join(self.repo, 'README'), 'readme\n')
        base = commit(self.repo, 'base')
        write(os.path.join(self.repo, 'feature.txt'), 'feature\n')
        commit(self.repo, 'head')
        out = self.run_script(['--base', base, '--', 'sh', 'tests/check.sh'])
        self.assertEqual((out['result'], out['test_files'], out['control_rc'], self.last_rc), ('no_tests', [], None, 1))
        self.assertNothingLeftBehind()

    def test_named_test_files_override_detection(self):
        make_repo(self.repo)
        write(os.path.join(self.repo, 'README'), 'readme\n')
        base = commit(self.repo, 'base')
        # checks/verify.sh does not match the test-file pattern; tests/other.sh does but is not named.
        write(os.path.join(self.repo, 'checks', 'verify.sh'), CHECK_SH)
        write(os.path.join(self.repo, 'tests', 'other.sh'), 'true\n')
        write(os.path.join(self.repo, 'feature.txt'), 'feature\n')
        commit(self.repo, 'head')
        out = self.run_script(['--base', base, '--', 'true'])
        self.assertEqual(out['test_files'], ['tests/other.sh'])
        # Named test files are the only ones copied into the check copy, so tests/other.sh is absent there.
        out = self.run_script(['--base', base, '--test-file', 'checks/verify.sh', '--', 'sh', '-c',
                               'sh checks/verify.sh || test -e tests/other.sh'])
        self.assertEqual((out['result'], out['test_files'], self.last_rc), ('passed', ['checks/verify.sh'], 0))

    def test_uncommitted_changes_count_as_the_final_code(self):
        make_repo(self.repo)
        write(os.path.join(self.repo, 'README'), 'readme\n')
        commit(self.repo, 'base')
        write(os.path.join(self.repo, 'feature.txt'), 'feature\n')
        git(self.repo, 'add', 'feature.txt')  # staged, never committed
        write(os.path.join(self.repo, 'tests', 'check.sh'), CHECK_SH)  # untracked
        out = self.run_script(['--base', 'HEAD', '--', 'sh', 'tests/check.sh'])
        self.assertEqual((out['result'], out['test_files']), ('passed', ['tests/check.sh']))

    def test_unstaged_edits_count_as_the_final_code(self):
        make_repo(self.repo)
        write(os.path.join(self.repo, 'feature.txt'), 'old\n')
        write(os.path.join(self.repo, 'tests', 'check.sh'), 'true\n')
        commit(self.repo, 'base')
        write(os.path.join(self.repo, 'feature.txt'), 'new\n')
        write(os.path.join(self.repo, 'tests', 'check.sh'), 'grep -q new feature.txt\n')
        out = self.run_script(['--base', 'HEAD', '--', 'sh', 'tests/check.sh'])
        self.assertEqual((out['result'], out['test_files']), ('passed', ['tests/check.sh']))

    def test_a_file_deleted_in_the_working_tree_is_gone_from_the_control_copy(self):
        make_repo(self.repo)
        write(os.path.join(self.repo, 'feature.txt'), 'old feature\n')
        write(os.path.join(self.repo, 'staged-gone.txt'), 'old\n')
        commit(self.repo, 'base')
        os.remove(os.path.join(self.repo, 'feature.txt'))
        git(self.repo, 'rm', '-q', 'staged-gone.txt')
        write(os.path.join(self.repo, 'tests', 'gone.sh'), 'test ! -e feature.txt && test ! -e staged-gone.txt\n')
        out = self.run_script(['--base', 'HEAD', '--', 'sh', 'tests/gone.sh'])
        self.assertEqual((out['result'], out['control_rc']), ('passed', 0))

    def test_a_test_file_deleted_in_the_final_code_is_skipped(self):
        make_repo(self.repo)
        write(os.path.join(self.repo, 'tests', 'old_test.sh'), 'true\n')
        base = commit(self.repo, 'base')
        os.remove(os.path.join(self.repo, 'tests', 'old_test.sh'))
        write(os.path.join(self.repo, 'tests', 'check.sh'), CHECK_SH)
        write(os.path.join(self.repo, 'feature.txt'), 'feature\n')
        commit(self.repo, 'head')
        out = self.run_script(['--base', base, '--', 'sh', 'tests/check.sh'])
        self.assertEqual((out['result'], out['test_files']), ('passed', ['tests/check.sh']))

    def test_ignored_dependencies_need_link(self):
        make_repo(self.repo)
        write(os.path.join(self.repo, '.gitignore'), 'deps/\n')
        write(os.path.join(self.repo, 'README'), 'readme\n')
        base = commit(self.repo, 'base')
        write(os.path.join(self.repo, 'deps', 'lib.txt'), 'lib\n')
        write(os.path.join(self.repo, 'tests', 'check.sh'), 'test -f deps/lib.txt && test -f feature.txt\n')
        write(os.path.join(self.repo, 'feature.txt'), 'feature\n')
        commit(self.repo, 'head')
        command = ['--', 'sh', 'tests/check.sh']
        out = self.run_script(['--base', base, *command])
        self.assertEqual((out['result'], self.last_rc), ('couldnt_run', 3))
        out = self.run_script(['--base', base, '--link', 'deps', *command])
        self.assertEqual((out['result'], self.last_rc), ('passed', 0))
        self.assertTrue(os.path.isfile(os.path.join(self.repo, 'deps', 'lib.txt')))
        self.assertNothingLeftBehind()

    def test_a_link_outside_the_repo_is_a_usage_error(self):
        base = self.build(base_has_feature=False, head_has_feature=True)
        self.assertIsNone(self.run_script(['--base', base, '--link', '../elsewhere', '--', 'sh', 'tests/check.sh']))
        self.assertEqual(self.last_rc, 2)
        self.assertNothingLeftBehind()

    def test_repo_path_with_spaces(self):
        self.repo = os.path.join(self.root, 'repo with spaces')
        make_repo(self.repo)
        write(os.path.join(self.repo, 'README'), 'readme\n')
        base = commit(self.repo, 'base')
        write(os.path.join(self.repo, 'tests', 'check two.sh'), CHECK_SH)
        write(os.path.join(self.repo, 'feature.txt'), 'feature\n')
        commit(self.repo, 'head')
        out = self.run_script(['--repo', self.repo, '--base', base, '--', 'sh', 'tests/check two.sh'], cwd=self.root)
        self.assertEqual((out['result'], out['test_files']), ('passed', ['tests/check two.sh']))
        self.assertNothingLeftBehind()

    def test_a_linked_worktree_as_the_repo(self):
        main = os.path.join(self.root, 'main repo')
        base = self.build(base_has_feature=False, head_has_feature=True, repo=main)
        self.repo = os.path.join(self.root, 'linked worktree')
        git(main, 'worktree', 'add', '-q', '-b', 'side', self.repo)
        write(os.path.join(self.repo, 'feature.txt'), 'side feature\n')  # uncommitted, only in the linked worktree
        before = self.snapshot(), self.snapshot(main)
        out = self.run_script(['--base', base, '--', 'sh', '-c', 'sh tests/check.sh && grep -q side feature.txt'])
        self.assertEqual((out['result'], out['test_files']), ('passed', ['tests/check.sh']))
        self.assertEqual((self.snapshot(), self.snapshot(main)), before)
        self.assertEqual(os.listdir(self.systmp), [])

    def test_working_tree_and_worktree_list_are_unchanged(self):
        base = self.build(base_has_feature=False, head_has_feature=True)
        write(os.path.join(self.repo, 'untracked.txt'), 'x\n')
        write(os.path.join(self.repo, 'README'), 'edited\n')
        before = self.snapshot()
        cases = [
            (['--', 'sh', 'tests/check.sh'], 'passed'),
            (['--', 'true'], 'failed'),
            (['--', 'false'], 'couldnt_run'),
            (['--', '/nonexistent/test-runner'], 'couldnt_run'),
            (['--timeout', '1', '--', 'sleep', '30'], 'couldnt_run'),
            (['--test-file', 'tests/missing.sh', '--', 'true'], 'couldnt_run'),
        ]
        for args, expected in cases:
            out = self.run_script(['--base', base, *args])
            self.assertEqual(out['result'], expected, args)
            self.assertEqual(self.snapshot(), before, args)
            self.assertNothingLeftBehind()
        out = self.run_script(['--base', 'HEAD', '--', 'true'])
        self.assertEqual(out['result'], 'no_tests')
        self.assertEqual(self.snapshot(), before)

    def test_git_variables_in_the_environment_do_not_redirect_it(self):
        other = os.path.join(self.root, 'other')
        make_repo(other)
        commit(other, 'other')
        base = self.build(base_has_feature=False, head_has_feature=True)
        other_before = self.snapshot(other)
        env = {'GIT_DIR': os.path.join(other, '.git'), 'GIT_WORK_TREE': other,
               'GIT_INDEX_FILE': os.path.join(other, '.git', 'index'), 'GIT_COMMON_DIR': os.path.join(other, '.git')}
        out = self.run_script(['--base', base, '--', 'sh', '-c', 'sh tests/check.sh && test -z "$GIT_DIR$GIT_WORK_TREE"'],
                              env=env)
        self.assertEqual(out['result'], 'passed')
        self.assertEqual(self.snapshot(other), other_before)
        self.assertNothingLeftBehind()

    # Usage and interruption.

    def test_usage_errors_exit_2(self):
        base = self.build(base_has_feature=False, head_has_feature=True)
        for args in (['--', 'true'], ['--base', base], ['--base', base, '--'], ['--base', 'not-a-commit', '--', 'true']):
            self.assertIsNone(self.run_script(args), args)
            self.assertEqual(self.last_rc, 2, args)
        self.assertNothingLeftBehind()

    def test_a_terminated_run_cleans_up_and_kills_the_test(self):
        base = self.build(base_has_feature=False, head_has_feature=True)
        before = self.snapshot()
        pid_file = os.path.join(self.root, 'child.pid')
        env = dict(CLEAN_ENV, TMPDIR=self.systmp)
        p = subprocess.Popen([PYTHON, '-I', SCRIPT, '--base', base, '--', 'sh', '-c', 'echo $$ > "$1"; sleep 30', 'sh',
                              pid_file], cwd=self.repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 15
        while not os.path.exists(pid_file) and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.2)
        p.send_signal(signal.SIGTERM)
        stdout, _ = p.communicate(timeout=15)
        self.assertEqual((json.loads(stdout)['result'], p.returncode), ('couldnt_run', 3))
        self.assertEqual(self.snapshot(), before)
        self.assertNothingLeftBehind()
        self.assertProcessGone(pid_file)


if __name__ == '__main__':
    unittest.main()
