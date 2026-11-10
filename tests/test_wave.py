"""Tests for the wave script. They run the real script against real throwaway git repos in a temp folder.

Run with: /usr/bin/python3 -I tests/test_wave.py
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
# The script sits in ../scripts in the repo and in .. once installed to ~/.claude/scripts/tests.
SCRIPTS_DIR = os.path.join(HERE, '..', 'scripts') if os.path.isdir(os.path.join(HERE, '..', 'scripts')) else os.path.join(HERE, '..')
SCRIPT = os.path.join(SCRIPTS_DIR, 'wave.py')
PYTHON = '/usr/bin/python3'

# Copied from tests/test_failing_check.py, since tests can't import each other: keep them the same.
CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}


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


class Wave(unittest.TestCase):
    def setUp(self):
        # realpath'd because git rev-parse --show-toplevel resolves symlinks (/tmp is /private/tmp on macOS).
        self.root = os.path.realpath(tempfile.mkdtemp(prefix='wave-test-'))
        self.repo = os.path.join(self.root, 'some folder', 'repo')
        make_repo(self.repo)
        write(os.path.join(self.repo, '.gitignore'), 'deps/\n')
        write(os.path.join(self.repo, 'README'), 'readme\n')
        commit(self.repo, 'base')
        write(os.path.join(self.repo, 'deps', 'lib.txt'), 'lib\n')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_script(self, args, cwd=None):
        p = subprocess.run([PYTHON, '-I', SCRIPT, *args], cwd=cwd or self.repo, capture_output=True, text=True,
                           env=CLEAN_ENV, timeout=60)
        self.last_rc = p.returncode
        self.last_stderr = p.stderr
        return json.loads(p.stdout) if p.stdout.strip() else None

    def head(self):
        return git(self.repo, 'rev-parse', 'HEAD').strip()

    def task_path(self, n):
        return os.path.join(self.repo, '.worktrees', f'task-{n}')

    def worktrees(self):
        """How many worktrees the repo has registered, the checkout included."""
        return git(self.repo, 'worktree', 'list', '--porcelain').count('worktree ')

    def wave(self, *tasks):
        base = self.head()
        self.run_script(['start', '--base', base, *map(str, tasks)])
        return base

    def edit(self, n, rel, text):
        write(os.path.join(self.task_path(n), rel), text)
        commit(self.task_path(n), f'task {n}')

    def run_in_linked_worktree(self):
        """Move the run branch (main) into a linked worktree, the checkout going to another branch; returns its path."""
        path = os.path.join(self.root, 'run')
        git(self.repo, 'checkout', '-q', '-b', 'other')
        git(self.repo, 'worktree', 'add', '-q', path, 'main')
        return path

    def test_start_makes_a_worktree_per_task_from_the_base(self):
        base = self.head()
        out = self.run_script(['start', '--base', base, '--link', 'deps', '3', '4'])
        self.assertEqual(self.last_rc, 0)
        self.assertEqual(out['run_branch'], 'main')
        self.assertEqual(out['base'], base)
        self.assertEqual(out['tasks'],
                         [{'task': n, 'branch': f'main-task-{n}', 'path': self.task_path(n)} for n in (3, 4)])
        for n in (3, 4):
            path = self.task_path(n)
            self.assertEqual(git(path, 'rev-parse', 'HEAD').strip(), base)
            self.assertEqual(git(path, 'symbolic-ref', '--short', 'HEAD').strip(), f'main-task-{n}')
            self.assertTrue(os.path.islink(os.path.join(path, 'deps')))
            self.assertTrue(os.path.isfile(os.path.join(path, 'deps', 'lib.txt')))
            self.assertEqual(git(path, 'status', '--porcelain', '--untracked-files=all'), '')  # the link is excluded

    def test_start_writes_the_exclude_once_and_a_self_ignoring_gitignore_when_needed(self):
        # .worktrees/ is not ignored by this fixture, so start must keep it out of git status itself.
        base = self.head()
        self.run_script(['start', '--base', base, '--link', 'deps', '3'])
        self.run_script(['start', '--base', base, '--link', 'deps', '4'])
        exclude = os.path.join(self.repo, '.git', 'info', 'exclude')
        with open(exclude) as f:
            self.assertEqual(f.read().count('/deps\n'), 1)
        with open(os.path.join(self.repo, '.worktrees', '.gitignore')) as f:
            self.assertEqual(f.read(), '*\n')
        self.assertEqual(git(self.repo, 'status', '--porcelain', '--untracked-files=all'), '')

    def test_start_refuses_an_existing_task_branch_and_creates_nothing_else(self):
        base = self.wave(3)
        out = self.run_script(['start', '--base', base, '3', '4'])
        self.assertEqual(self.last_rc, 1)
        self.assertIn('main-task-3', out['error'])
        self.assertFalse(os.path.exists(self.task_path(4)))
        self.assertEqual(git(self.repo, 'branch', '--list', 'main-task-4'), '')

    def test_start_usage_errors_exit_2(self):
        base = self.head()
        for args in (['start', '--base', base], ['start', '--base', 'nope', '3'], ['start', '--base', base, 'x'],
                     ['start', '--base', base, '--link', '../elsewhere', '3'], ['start', '--base', base, '--link', 'nope', '3']):
            with self.subTest(args=args):
                self.assertIsNone(self.run_script(args))
                self.assertEqual(self.last_rc, 2)
        self.assertIsNone(self.run_script(['start', '--base', base, '3'], cwd=self.root))  # not a checkout
        self.assertEqual(self.last_rc, 2)
        git(self.repo, 'checkout', '-q', '--detach')
        self.assertIsNone(self.run_script(['start', '--base', base, '3']))
        self.assertEqual(self.last_rc, 2)
        self.assertIn('detached', self.last_stderr)
        self.assertEqual(self.worktrees(), 1)

    def test_start_refuses_a_tracked_link_before_creating_anything(self):
        base = self.head()
        self.assertIsNone(self.run_script(['start', '--base', base, '--link', 'README', '3', '4']))
        self.assertEqual(self.last_rc, 2)
        self.assertEqual(self.worktrees(), 1)
        self.assertEqual(git(self.repo, 'branch', '--list', 'main-task-*'), '')

    def test_start_with_a_repeated_link_makes_one_link(self):
        out = self.run_script(['start', '--base', self.head(), '--link', 'deps', '--link', 'deps', '3'])
        self.assertEqual(self.last_rc, 0)
        self.assertTrue(os.path.islink(os.path.join(out['tasks'][0]['path'], 'deps')))

    def test_start_from_another_folder_with_repo(self):
        base = self.head()
        out = self.run_script(['start', '--repo', self.repo, '--base', base, '3'], cwd=self.root)
        self.assertEqual(self.last_rc, 0)
        self.assertEqual(out['tasks'][0]['path'], self.task_path(3))

    def test_merge_merges_in_order_with_one_merge_commit_each(self):
        base = self.wave(3, 4)
        self.edit(3, 'three.txt', 'three\n')
        self.edit(4, 'four.txt', 'four\n')
        out = self.run_script(['merge', '3', '4'])
        self.assertEqual((self.last_rc, out['merged'], out['conflict']), (0, [3, 4], None))
        subjects = git(self.repo, 'log', '--first-parent', '--format=%s', f'{base}..HEAD').splitlines()
        self.assertEqual(subjects, ['merge: task 4', 'merge: task 3'])
        self.assertEqual(git(self.repo, 'rev-list', '--merges', '--count', f'{base}..HEAD').strip(), '2')
        for name in ('three.txt', 'four.txt'):
            self.assertTrue(os.path.isfile(os.path.join(self.repo, name)))

    def test_merge_stops_at_the_first_conflict_and_leaves_it_in_progress(self):
        self.wave(3, 4)
        self.edit(3, 'shared.txt', 'three\n')
        self.edit(4, 'shared.txt', 'four\n')
        out = self.run_script(['merge', '3', '4'])
        self.assertEqual(self.last_rc, 1)
        self.assertEqual((out['merged'], out['conflict']), ([3], {'task': 4, 'files': ['shared.txt']}))
        self.assertTrue(git(self.repo, 'rev-parse', '-q', '--verify', 'MERGE_HEAD'))
        self.assertEqual(git(self.repo, 'log', '--format=%s', '-1').strip(), 'merge: task 3')

    def test_merge_reports_a_failed_merge_that_is_not_a_conflict_as_an_error(self):
        # An untracked file at a path the task adds passes the dirty check, but git refuses to start the merge.
        self.wave(3)
        self.edit(3, 'three.txt', 'three\n')
        write(os.path.join(self.repo, 'three.txt'), 'untracked\n')
        out = self.run_script(['merge', '3'])
        self.assertEqual(self.last_rc, 1)
        self.assertIn('untracked working tree files would be overwritten', out['error'])
        self.assertNotIn('merged', out)
        with self.assertRaises(subprocess.CalledProcessError):
            git(self.repo, 'rev-parse', '-q', '--verify', 'MERGE_HEAD')

    def test_merge_refuses_a_dirty_tree_or_a_merge_in_progress(self):
        self.wave(3, 4)
        self.edit(3, 'shared.txt', 'three\n')
        self.edit(4, 'shared.txt', 'four\n')
        write(os.path.join(self.repo, 'README'), 'edited\n')
        out = self.run_script(['merge', '3'])
        self.assertEqual(self.last_rc, 1)
        self.assertIn('uncommitted', out['error'])
        self.assertEqual(git(self.repo, 'rev-list', '--merges', '--count', 'HEAD').strip(), '0')
        git(self.repo, 'checkout', '-q', 'README')
        self.run_script(['merge', '3', '4'])  # stops on task 4's conflict
        out = self.run_script(['merge', '4'])
        self.assertEqual(self.last_rc, 1)
        self.assertIn('in progress', out['error'])

    def test_finish_removes_merged_worktrees_and_branches(self):
        self.wave(3, 4)
        self.edit(3, 'three.txt', 'three\n')
        self.edit(4, 'four.txt', 'four\n')
        self.run_script(['merge', '3', '4'])
        out = self.run_script(['finish', '3', '4'])
        self.assertEqual(self.last_rc, 0)
        self.assertEqual([r['task'] for r in out['removed']], [3, 4])
        self.assertEqual(self.worktrees(), 1)
        self.assertEqual(git(self.repo, 'branch', '--list', 'main-task-*'), '')
        self.assertFalse(os.path.exists(self.task_path(3)))

    def test_finish_from_a_linked_worktree_after_starting_in_the_main_checkout(self):
        self.wave(3, 4)
        self.edit(3, 'three.txt', 'three\n')
        self.edit(4, 'four.txt', 'four\n')
        run = self.run_in_linked_worktree()
        self.run_script(['merge', '--repo', run, '3', '4'], cwd=self.root)
        out = self.run_script(['finish', '--repo', run, '3', '4'], cwd=self.root)
        self.assertEqual(self.last_rc, 0)
        self.assertEqual([r['path'] for r in out['removed']], [self.task_path(3), self.task_path(4)])
        self.assertEqual(self.worktrees(), 2)  # the checkout and the run's worktree

    def test_finish_with_a_linked_folder_keeps_the_checkouts_folder(self):
        self.run_script(['start', '--base', self.head(), '--link', 'deps', '3'])
        self.edit(3, 'three.txt', 'three\n')
        self.run_script(['merge', '3'])
        self.run_script(['finish', '3'])
        self.assertEqual(self.last_rc, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.repo, 'deps', 'lib.txt')))
        self.assertEqual(self.worktrees(), 1)
        self.assertFalse(os.path.lexists(self.task_path(3)))

    def test_finish_refuses_an_unmerged_branch_and_removes_nothing(self):
        self.wave(3, 4)
        self.edit(3, 'three.txt', 'three\n')
        self.edit(4, 'four.txt', 'four\n')
        self.run_script(['merge', '3'])
        out = self.run_script(['finish', '3', '4'])
        self.assertEqual((self.last_rc, out['unmerged']), (1, [4]))
        self.assertTrue(os.path.isdir(self.task_path(3)))
        self.assertEqual(self.worktrees(), 3)

    def test_finish_refuses_a_worktree_with_uncommitted_work_and_removes_nothing(self):
        self.wave(3, 4)
        self.edit(3, 'three.txt', 'three\n')
        self.edit(4, 'four.txt', 'four\n')
        self.run_script(['merge', '3', '4'])
        write(os.path.join(self.task_path(4), 'notes.txt'), 'not committed\n')
        out = self.run_script(['finish', '3', '4'])
        self.assertEqual(self.last_rc, 1)
        self.assertIn('uncommitted', out['error'])
        self.assertEqual(self.worktrees(), 3)
        self.assertTrue(os.path.isdir(self.task_path(3)))

    def test_finish_refuses_a_task_branch_no_worktree_has_and_removes_nothing(self):
        self.wave(3, 4)
        self.edit(3, 'three.txt', 'three\n')
        self.edit(4, 'four.txt', 'four\n')
        self.run_script(['merge', '3', '4'])
        git(self.repo, 'worktree', 'remove', self.task_path(4))
        out = self.run_script(['finish', '3', '4'])
        self.assertEqual(self.last_rc, 1)
        self.assertIn('main-task-4', out['error'])
        self.assertEqual(self.worktrees(), 2)
        self.assertEqual(git(self.repo, 'branch', '--list', '--format=%(refname:short)', 'main-task-*').split(),
                         ['main-task-3', 'main-task-4'])

    def test_finish_drops_a_worktree_whose_folder_is_gone(self):
        self.wave(3)
        self.edit(3, 'three.txt', 'three\n')
        self.run_script(['merge', '3'])
        shutil.rmtree(self.task_path(3))
        out = self.run_script(['finish', '3'])
        self.assertEqual(self.last_rc, 0)
        self.assertEqual([r['task'] for r in out['removed']], [3])
        self.assertEqual(self.worktrees(), 1)
        self.assertEqual(git(self.repo, 'branch', '--list', 'main-task-3'), '')

    def test_merge_and_finish_refuse_a_task_with_no_branch(self):
        self.wave(3)
        self.edit(3, 'three.txt', 'three\n')
        for command in ('merge', 'finish'):
            with self.subTest(command=command):
                out = self.run_script([command, '3', '9'])
                self.assertEqual(self.last_rc, 1)
                self.assertIn('main-task-9', out['error'])
        self.assertEqual(git(self.repo, 'rev-list', '--merges', '--count', 'HEAD').strip(), '0')
        self.assertEqual(self.worktrees(), 2)


if __name__ == '__main__':
    unittest.main()
