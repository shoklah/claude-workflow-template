"""Tests for the two draft hooks. They run the real scripts against real git repos in a temp folder.

Run with: /usr/bin/python3 -I test_hooks.py
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
# Hooks sit in ../hooks in the repo and in .. once installed to ~/.claude/hooks/tests.
HOOKS_DIR = os.path.join(HERE, '..', 'hooks') if os.path.isdir(os.path.join(HERE, '..', 'hooks')) else os.path.join(HERE, '..')
GUARD = os.path.join(HOOKS_DIR, 'guard-main-commit.py')
SESSION_START = os.path.join(HOOKS_DIR, 'session-start-superpowers.sh')
SNIPPET = os.path.join(HERE, 'settings-snippet.json')
PYTHON = '/usr/bin/python3'
CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}


def git(cwd, *args):
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, env=CLEAN_ENV)


def make_repo(path, branch='main', commit=True):
    os.makedirs(path)
    git(path, 'init', '-q', '-b', branch)
    git(path, 'config', 'user.email', 't@example.test')
    git(path, 'config', 'user.name', 't')
    git(path, 'config', 'commit.gpgsign', 'false')
    if commit:
        git(path, 'commit', '-q', '--allow-empty', '-m', 'first')


class RepoFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='guard-test-')
        r = cls.root
        cls.dirs = {
            'root': r,
            'plain': f'{r}/plain',
            'main': f'{r}/main-repo',
            'sub': f'{r}/main-repo/sub',
            'spaced': f'{r}/repo with spaces',
            'master': f'{r}/master-repo',
            'feature': f'{r}/feature-repo',
            'worktree': f'{r}/feature-worktree',
            'detached': f'{r}/detached-repo',
            'unborn': f'{r}/unborn-repo',
        }
        os.makedirs(cls.dirs['plain'])
        make_repo(cls.dirs['main'])
        os.makedirs(cls.dirs['sub'])
        make_repo(cls.dirs['spaced'])
        make_repo(cls.dirs['master'], branch='master')
        make_repo(cls.dirs['feature'])
        git(cls.dirs['feature'], 'checkout', '-q', '-b', 'feature')
        git(cls.dirs['main'], 'worktree', 'add', '-q', '-b', 'in-worktree', cls.dirs['worktree'])
        make_repo(cls.dirs['detached'])
        git(cls.dirs['detached'], 'checkout', '-q', '--detach')
        make_repo(cls.dirs['unborn'], commit=False)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def fill(self, text):
        for key, path in self.dirs.items():
            text = text.replace(f'@{key}@', path)
        return text

    def run_guard(self, command, cwd='main', mode='default', tool='Bash', env=None):
        payload = {
            'session_id': 't', 'transcript_path': '/tmp/t.jsonl', 'cwd': self.dirs[cwd], 'permission_mode': mode,
            'hook_event_name': 'PreToolUse', 'tool_name': tool, 'tool_use_id': 'toolu_1',
            'tool_input': {'command': self.fill(command), 'description': 'x', 'timeout': 120000, 'run_in_background': False},
        }
        p = subprocess.run([PYTHON, '-I', GUARD], input=json.dumps(payload), capture_output=True, text=True,
                           timeout=20, env=dict(CLEAN_ENV, **(env or {})))
        self.assertEqual(p.returncode, 0, p.stderr)
        if not p.stdout.strip():
            return None
        out = json.loads(p.stdout)['hookSpecificOutput']
        self.assertEqual(out['hookEventName'], 'PreToolUse')
        # An updatedInput here would replace the command and undo another hook's rewrite.
        self.assertEqual(sorted(out), ['hookEventName', 'permissionDecision', 'permissionDecisionReason'])
        return out

    def decision(self, *args, **kwargs):
        out = self.run_guard(*args, **kwargs)
        return out and out['permissionDecision']


class StopsCommitsOnProtectedBranches(RepoFixture):
    def test_every_way_of_writing_a_commit_on_main(self):
        cases = [
            'git commit -m x',
            'git commit -am x',
            'git commit --amend --no-edit',
            'git add . && git commit -m x',
            'git add .; git commit -m x',
            'git add .\ngit commit -m x',
            'git status || git commit -m x',
            'git commit -m "$(cat <<\'EOF\'\nfix: don\'t break "this"\nEOF\n)"',
            'git -c user.name=x commit -m x',
            'git --no-pager commit -m x',
            '/usr/bin/git commit -m x',
            'FOO=1 git commit -m x',
            'env FOO=1 git commit -m x',
            'env -u FOO git commit -m x',
            'timeout 5 git commit -m x',
            'command git commit -m x',
            'nohup git commit -m x &',
            'bash -c "git commit -m x"',
            "sh -lc 'git add . && git commit -m x'",
            'eval "git commit -m x"',
            'if true; then git commit -m x; fi',
            '(git commit -m x)',
            '{ git commit -m x; }',
            'for i in 1; do git commit -m x; done',
            '! git commit -m x',
            'git add . && git commit -m x > /dev/null 2>&1',
            'x=$(git commit -m y)',
        ]
        for command in cases:
            with self.subTest(command=command):
                self.assertEqual(self.decision(command), 'ask')

    def test_commit_reached_from_another_folder(self):
        cases = [
            ('plain', 'cd @main@ && git commit -m x'),
            ('plain', 'git -C @main@ commit -m x'),
            ('plain', 'cd "@spaced@" && git commit -m x'),
            ('plain', 'git -C "@spaced@" commit -m x'),
            ('feature', 'cd @main@ && git commit -m x'),
            ('root', 'cd main-repo && git commit -m x'),
            ('main', 'cd sub && git commit -m x'),
            ('sub', 'git commit -m x'),
            ('plain', 'cd @main@; git status; git commit -m x'),
        ]
        for cwd, command in cases:
            with self.subTest(cwd=cwd, command=command):
                self.assertEqual(self.decision(command, cwd=cwd), 'ask')

    def test_commit_after_switching_onto_main(self):
        for command in ('git switch main && git commit -m x', 'git checkout main && git commit -m x'):
            with self.subTest(command=command):
                self.assertEqual(self.decision(command, cwd='feature'), 'ask')

    def test_reason_names_the_branch_it_found(self):
        self.assertIn('master', self.run_guard('git commit -m x', cwd='master')['permissionDecisionReason'])
        self.assertIn('main', self.run_guard('git commit -m x', cwd='main')['permissionDecisionReason'])

    def test_asks_when_the_branch_cannot_be_worked_out(self):
        cases = [
            ('plain', 'cd "$REPO" && git commit -m x'),
            ('plain', 'git -C "$REPO" commit -m x'),
            ('feature', 'cd $(git rev-parse --show-toplevel)/.. && git commit -m x'),
            ('feature', 'git --git-dir=/elsewhere/.git commit -m x'),
            ('feature', 'GIT_DIR=/elsewhere/.git git commit -m x'),
            ('main', 'git commit -m "unbalanced'),
        ]
        for cwd, command in cases:
            with self.subTest(cwd=cwd, command=command):
                self.assertEqual(self.decision(command, cwd=cwd), 'ask')

    def test_git_variables_in_the_hook_environment_do_not_hide_main(self):
        env = {'GIT_DIR': os.path.join(self.dirs['feature'], '.git')}
        self.assertEqual(self.decision('git commit -m x', cwd='main', env=env), 'ask')


class FolderVariablesSetInTheSameCommand(RepoFixture):
    """Real sessions write `B=/path; git -C "$B" commit`. Prompting on those was a false alarm on branches."""

    def test_stays_silent_when_the_variable_points_at_a_branch(self):
        cases = [
            ('main', 'B=@feature@; git -C "$B" commit -m x'),
            ('main', 'B=@feature@\ngit -C "$B" commit -m x'),
            ('main', 'W=@feature@; cd "$W" && git commit -m x'),
            ('main', 'W=@feature@; cd ${W} && git commit -m x'),
            ('main', 'SP=/tmp/unused B=@feature@; git -C "$B" commit -m x'),
            ('main', 'export W=@worktree@; git -C "$W" commit -m x'),
            ('main', 'R=@root@; W=$R/feature-repo; git -C "$W" commit -m x'),
            ('main', 'R=@root@; git -C "${R}/feature-repo" commit -m x'),
            ('main', "S=/tmp/s\nW=@feature@\ncat > \"$S/msg\" <<'EOF'\nfix: don't\nEOF\ngit -C \"$W\" commit -F \"$S/msg\""),
        ]
        for cwd, command in cases:
            with self.subTest(command=command):
                self.assertIsNone(self.decision(command, cwd=cwd))

    def test_still_asks_when_the_variable_points_at_main(self):
        cases = [
            ('plain', 'B=@main@; git -C "$B" commit -m x'),
            ('feature', 'W=@main@\ncd "$W" && git commit -m x'),
            ('feature', 'B="@spaced@"; git -C "$B" commit -m x'),
            ('feature', 'R=@root@; W=$R/main-repo; git -C "$W" commit -m x'),
            ('feature', 'export W=@main@; git -C "$W" commit -m x'),
            ('feature', 'W=@feature@; W=@main@; git -C "$W" commit -m x'),
            ('feature', 'W=@feature@; (W=@main@; git -C "$W" commit -m x)'),
        ]
        for cwd, command in cases:
            with self.subTest(command=command):
                out = self.run_guard(command, cwd=cwd)
                self.assertEqual(out and out['permissionDecision'], 'ask')
                self.assertIn('commits directly', out['permissionDecisionReason'])

    def test_a_subshell_assignment_does_not_leak_out(self):
        self.assertIsNone(self.decision('W=@feature@; (W=@main@; true); git -C "$W" commit -m x', cwd='main'))

    def test_still_asks_when_the_value_cannot_be_worked_out(self):
        cases = [
            'git -C "$NEVER_SET" commit -m x',
            'B=$(pwd)/x; git -C "$B" commit -m x',
            'B="$(pwd)/x"; git -C "$B" commit -m x',
            'B=$OTHER/x; git -C "$B" commit -m x',
            'for W in a b; do git -C "$W" commit -m x; done',
            # The shell expands $W before a prefix assignment applies, so the prefix tells us nothing.
            'W=@feature@ git -C "$W" commit -m x',
            # A child shell doesn't see unexported variables from this one.
            'W=@feature@; bash -c \'git -C "$W" commit -m x\'',
        ]
        for command in cases:
            with self.subTest(command=command):
                self.assertEqual(self.decision(command, cwd='plain'), 'ask')


class PermissionModes(RepoFixture):
    def test_prompts_in_modes_where_the_docs_promise_a_prompt(self):
        for mode in ('default', 'acceptEdits', 'auto'):
            with self.subTest(mode=mode):
                self.assertEqual(self.decision('git commit -m x', mode=mode), 'ask')

    def test_refuses_in_bypass_mode_where_nobody_is_prompted(self):
        self.assertEqual(self.decision('git commit -m x', mode='bypassPermissions'), 'deny')


class LeavesEverythingElseAlone(RepoFixture):
    def test_commits_that_do_not_land_on_a_protected_branch(self):
        cases = [
            ('feature', 'git commit -m x'),
            ('worktree', 'git commit -m x'),
            ('detached', 'git commit -m x'),
            ('unborn', 'git commit -m first'),
            ('unborn', 'git add . && git commit -m first'),
            ('plain', 'git commit -m x'),
            ('main', 'git checkout -b new-work && git commit -m x'),
            ('main', 'git checkout -B new-work && git commit -m x'),
            ('main', 'git switch -c new-work && git commit -m x'),
            ('main', 'git switch in-worktree && git commit -m x'),
            ('main', 'git checkout -b new-work && cd sub && git commit -m x'),
            ('main', 'cd @feature@ && git commit -m x'),
            ('main', 'git -C @feature@ commit -m x'),
            ('main', 'git worktree add ../not-created-yet -b later && cd ../not-created-yet && git commit -m x'),
            ('feature', '(cd @main@ && git status); git commit -m x'),
            ('feature', 'git checkout main -- file.txt && git commit -m x'),
        ]
        for cwd, command in cases:
            with self.subTest(cwd=cwd, command=command):
                self.assertIsNone(self.decision(command, cwd=cwd))

    def test_commands_that_only_mention_commit_on_main(self):
        cases = [
            'git status',
            'git log --grep=commit',
            'git log --format=commit',
            'git commit-tree HEAD^{tree} -m x',
            'git help commit',
            'git config alias.ci commit',
            'git notes add -m commit',
            'git merge feature',
            'git pull',
            'git push origin main',
            'echo git commit -m x',
            'echo "git commit -m x"',
            "printf 'git commit\\n'",
            'grep -rn "git commit" .',
            "rg 'git commit'",
            'git diff && echo commit',
            "cat > notes.txt <<'EOF'\ngit commit -m x\ndon't\nEOF",
            'git status # git commit -m x',
            "# don't commit yet\ngit status",
        ]
        for command in cases:
            with self.subTest(command=command):
                self.assertIsNone(self.decision(command))

    def test_ignores_other_tools(self):
        self.assertIsNone(self.decision('git commit -m x', tool='Edit'))

    def test_bad_input_never_blocks_the_call(self):
        for text in ('not json', '{}', '{"tool_name":"Bash"}', '{"tool_name":"Bash","tool_input":{"command":3},"cwd":null}'):
            with self.subTest(text=text):
                p = subprocess.run([PYTHON, '-I', GUARD], input=text, capture_output=True, text=True, timeout=20, env=CLEAN_ENV)
                self.assertEqual((p.returncode, p.stdout), (0, ''))


class SessionStart(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='session-start-test-')
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def install_linked_skill(self, body):
        # Skills are often symlinks: ~/.claude/skills/<name> links to a folder elsewhere.
        target = self.write_skill('shared-skills/using-superpowers', body)
        os.makedirs(f'{self.home}/.claude/skills')
        os.symlink(target, f'{self.home}/.claude/skills/using-superpowers')

    def install_guidelines(self, body):
        self.write_skill('.claude/plugins/marketplaces/karpathy-skills/skills/karpathy-guidelines', body)

    def write_skill(self, folder, body):
        """Writes <home>/<folder>/SKILL.md and returns the folder's full path."""
        target = f'{self.home}/{folder}'
        os.makedirs(target)
        with open(f'{target}/SKILL.md', 'w') as f:
            f.write(body)
        return target

    def run_hook(self, home):
        return subprocess.run(['sh', SESSION_START], input='{"hook_event_name":"SessionStart","source":"startup"}',
                              capture_output=True, text=True, timeout=10, env=dict(CLEAN_ENV, HOME=home))

    def test_prints_the_skill_through_the_symlink(self):
        self.install_linked_skill('FIXTURE RULE BODY\n')
        p = self.run_hook(self.home)
        self.assertEqual(p.returncode, 0)
        self.assertIn('FIXTURE RULE BODY', p.stdout)

    def test_a_missing_skill_adds_nothing_and_does_not_fail(self):
        p = self.run_hook(self.home)
        self.assertEqual((p.returncode, p.stdout), (0, ''))
        self.assertIn('using-superpowers', p.stderr)

    def test_prints_the_guidelines_after_the_skill(self):
        self.install_linked_skill('FIXTURE RULE BODY\n')
        self.install_guidelines('FIXTURE GUIDELINE BODY\n')
        p = self.run_hook(self.home)
        self.assertLess(p.stdout.index('FIXTURE RULE BODY'), p.stdout.index('FIXTURE GUIDELINE BODY'))

    def test_missing_guidelines_still_print_the_skill(self):
        self.install_linked_skill('FIXTURE RULE BODY\n')
        p = self.run_hook(self.home)
        self.assertEqual(p.returncode, 0)
        self.assertIn('FIXTURE RULE BODY', p.stdout)
        self.assertIn('karpathy-guidelines', p.stderr)

    def test_guidelines_alone_still_print_when_the_skill_is_missing(self):
        self.install_guidelines('FIXTURE GUIDELINE BODY\n')
        self.assertIn('FIXTURE GUIDELINE BODY', self.run_hook(self.home).stdout)

    # CI machines have no real ~/.claude; everywhere else a missing file must fail, not skip.
    @unittest.skipIf(os.environ.get('CI') == 'true', 'reads the real ~/.claude, which CI does not have')
    def test_the_real_skill_fits_under_the_10000_character_cap(self):
        # Past the cap Claude Code replaces the output with a file path, so the rules would not load.
        # The cap covers the combined output: the skill and the guidelines together.
        p = self.run_hook(os.path.expanduser('~'))
        self.assertIn('## The Rule', p.stdout)
        self.assertIn('## 2. Simplicity First', p.stdout)
        self.assertLess(len(p.stdout), 10000)


class SettingsSnippet(unittest.TestCase):
    """Runs each command exactly as the snippet writes it, from an installed layout in a temp HOME."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='snippet-test-')
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        os.makedirs(f'{self.home}/.claude/hooks')
        shutil.copy(GUARD, f'{self.home}/.claude/hooks/')
        shutil.copy(SESSION_START, f'{self.home}/.claude/hooks/')
        with open(SNIPPET) as f:
            self.hooks = json.load(f)['hooks']

    def commands(self, event):
        return [h['command'] for group in self.hooks[event] for h in group['hooks']]

    def run_command(self, command, stdin):
        return subprocess.run(['bash', '-c', command], input=stdin, capture_output=True, text=True, timeout=20,
                              env=dict(CLEAN_ENV, HOME=self.home))

    def test_guard_command_stops_a_commit_on_main(self):
        repo = f'{self.home}/repo'
        make_repo(repo)
        command = next(c for c in self.commands('PreToolUse') if 'guard-main-commit' in c)
        payload = {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'cwd': repo, 'permission_mode': 'auto',
                   'tool_input': {'command': 'git commit -m x'}}
        p = self.run_command(command, json.dumps(payload))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)['hookSpecificOutput']['permissionDecision'], 'ask')

    def test_session_start_command_prints_the_skill(self):
        skill = f'{self.home}/.claude/skills/using-superpowers'
        os.makedirs(skill)
        with open(f'{skill}/SKILL.md', 'w') as f:
            f.write('FIXTURE RULE BODY\n')
        p = self.run_command(self.commands('SessionStart')[0], '{}')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('FIXTURE RULE BODY', p.stdout)


if __name__ == '__main__':
    unittest.main()
