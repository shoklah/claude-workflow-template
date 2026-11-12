"""Tests for the installer. Every run gets a temp HOME, so nothing here touches the real ~/.claude or ~/.agents.

Run with: /usr/bin/python3 -I tests/test_install.py
"""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, '..')
INSTALL = os.path.join(ROOT, 'install.py')
PYTHON = '/usr/bin/python3'
CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
TEMP_ROOT = os.path.realpath(tempfile.gettempdir())

AGENTS = ['final-reviewer.md', 'fix-checker.md', 'implementer-copy.md', 'implementer-escalated.md',
          'implementer.md', 'kit-reviewer.md', 'plan-reviewer.md', 'plan-simplifier.md', 'sweeper.md', 'task-reviewer.md']
SCRIPTS = ['failing-test-check.py', 'kit.py', 'wave.py', 'workflow-report.py']
SKILLS = ['workflow-dashboard']
HOOK_FILES = ['guard-agent-dispatch.py', 'guard-main-commit.py', 'remind-context-cap.py',
              'session-start-superpowers.sh']
HOOK_TESTS = ['settings-snippet.json', 'test_dispatch.py', 'test_hooks.py']
START = '<!-- agentic-workflow:start -->'
END = '<!-- agentic-workflow:end -->'

OTHER = {'type': 'command', 'command': 'other-hook run'}
GUARD = {'type': 'command', 'command': '/usr/bin/python3 -I "$HOME/.claude/hooks/guard-main-commit.py"', 'timeout': 10}
SESSION = {'type': 'command', 'command': 'sh "$HOME/.claude/hooks/session-start-superpowers.sh"', 'timeout': 5}
DISPATCH_GROUP = {'matcher': 'Agent', 'hooks': [
    {'type': 'command', 'command': '/usr/bin/python3 -I "$HOME/.claude/hooks/guard-agent-dispatch.py"', 'timeout': 10}]}
STOP_GROUP = {'hooks': [
    {'type': 'command', 'command': '/usr/bin/python3 -I "$HOME/.claude/hooks/remind-context-cap.py"', 'timeout': 10}]}
SETTINGS = {
    'hooks': {
        'SessionStart': [{'matcher': 'startup|clear|compact', 'hooks': [SESSION]}],
        'PreToolUse': [{'matcher': 'Bash', 'hooks': [OTHER, GUARD]}],
    },
    'enabledPlugins': {'superpowers@claude-plugins-official': True},
    'someUnknownKey': {'nested': [1, 2.5, None, 'é']},
    'theme': 'dark',
}
SETTINGS_AFTER = {
    'hooks': {
        'SessionStart': [{'matcher': 'startup|clear|compact', 'hooks': [SESSION]}],
        'PreToolUse': [{'matcher': 'Bash', 'hooks': [OTHER, GUARD]}, DISPATCH_GROUP],
        'Stop': [STOP_GROUP],
    },
    'enabledPlugins': {'superpowers@claude-plugins-official': True},
    'someUnknownKey': {'nested': [1, 2.5, None, 'é']},
    'theme': 'dark',
}
CLAUDE_MD = '# notes\n- my notes\n\n@OTHER.md\n\n# other\nkeep me\n'


def read(path, mode='r'):
    with open(path, mode, **({} if 'b' in mode else {'encoding': 'utf-8'})) as f:
        return f.read()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(text if isinstance(text, bytes) else text.encode('utf-8'))


def mode_of(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def tree(folder):
    """Every file under folder, as relative path -> bytes."""
    found = {}
    for dirpath, _, files in os.walk(folder):
        for name in files:
            full = os.path.join(dirpath, name)
            found[os.path.relpath(full, folder)] = read(full, 'rb')
    return found


class HomeFixture(unittest.TestCase):
    home_name = 'Application Support/home'

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='install-test-')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = os.path.join(self.root, self.home_name)
        self.claude = os.path.join(self.home, '.claude')
        os.makedirs(self.claude)
        self.section = read(os.path.join(ROOT, 'claude-md', 'agentic-workflow.md'))

    def run_install(self, *args):
        # Never let a run see the real HOME.
        self.assertTrue(os.path.realpath(self.home).startswith(TEMP_ROOT + os.sep))
        return subprocess.run([PYTHON, '-I', INSTALL, *args], capture_output=True, text=True, timeout=60,
                              env=dict(CLEAN_ENV, HOME=self.home))

    def ok(self, *args):
        p = self.run_install(*args)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stderr, '')
        return p

    def install_everything(self):
        for command in ('agents', 'section', 'scripts', 'skills', 'hooks'):
            self.ok(command)

    def path(self, *parts):
        return os.path.join(self.claude, *parts)

    def write_settings(self, data):
        write(self.path('settings.json'), json.dumps(data, indent=2, ensure_ascii=False) + '\n')

    def settings(self):
        return json.loads(read(self.path('settings.json')))


class Hooks(HomeFixture):

    def test_hooks_adds_the_dispatch_hook_and_keeps_everything_else(self):
        self.write_settings(SETTINGS)
        self.ok('hooks')
        after = self.settings()
        self.assertEqual(after, SETTINGS_AFTER)
        self.assertEqual(list(after), ['hooks', 'enabledPlugins', 'someUnknownKey', 'theme'])
        self.assertEqual(list(after['hooks']), ['SessionStart', 'PreToolUse', 'Stop'])
        self.assertIn('"someUnknownKey": {\n    "nested": [\n      1,\n      2.5,\n      null,\n      "é"\n',
                      read(self.path('settings.json')))
        self.assertTrue(read(self.path('settings.json')).endswith('}\n'))

    def test_hooks_copies_hook_files_and_their_tests_keeping_modes(self):
        self.write_settings(SETTINGS)
        self.ok('hooks')
        self.assertEqual(sorted(f for f in os.listdir(self.path('hooks')) if f != 'tests'), HOOK_FILES)
        self.assertEqual(sorted(os.listdir(self.path('hooks', 'tests'))), HOOK_TESTS)
        for name in HOOK_FILES:
            with self.subTest(name=name):
                source = os.path.join(ROOT, 'hooks', name)
                self.assertEqual(read(self.path('hooks', name), 'rb'), read(source, 'rb'))
                self.assertEqual(mode_of(self.path('hooks', name)), mode_of(source))
        for name in HOOK_TESTS:
            with self.subTest(name=name):
                self.assertEqual(read(self.path('hooks', 'tests', name), 'rb'), read(os.path.join(HERE, name), 'rb'))
        self.assertEqual(sorted(os.listdir(self.claude)), ['hooks', 'settings.json'])

    def test_hooks_creates_settings_when_there_is_none(self):
        self.ok('hooks')
        self.assertEqual(self.settings(), {'hooks': {'PreToolUse': [DISPATCH_GROUP], 'Stop': [STOP_GROUP]}})

    def test_installed_hook_tests_still_run_from_their_new_folder(self):
        self.write_settings(SETTINGS)
        self.ok('hooks')
        # test_hooks.py is left out: one of its tests reads the real ~/.claude skill, which a temp HOME doesn't have.
        p = subprocess.run([PYTHON, '-I', self.path('hooks', 'tests', 'test_dispatch.py')], capture_output=True,
                           text=True, timeout=300, cwd=self.root, env=dict(CLEAN_ENV, HOME=self.home))
        self.assertEqual(p.returncode, 0, p.stderr[-3000:])
        self.assertIn('OK (skipped=', p.stderr)  # The replay tool's tests skip once installed.

    def test_hooks_is_idempotent(self):
        self.write_settings(SETTINGS)
        self.ok('hooks')
        first = read(self.path('settings.json'), 'rb')
        self.ok('hooks')
        self.assertEqual(read(self.path('settings.json'), 'rb'), first)
        self.assertEqual(self.settings(), SETTINGS_AFTER)

    def test_an_edited_agent_hook_entry_is_recognised_and_not_duplicated(self):
        edited = {'matcher': 'Agent', 'hooks': [
            {'type': 'command', 'command': "/usr/bin/python3 -I '/Users/me/.claude/hooks/guard-agent-dispatch.py'",
             'timeout': 30}]}
        before = dict(SETTINGS, hooks=dict(SETTINGS['hooks'], PreToolUse=SETTINGS['hooks']['PreToolUse'] + [edited],
                                           Stop=[STOP_GROUP]))
        self.write_settings(before)
        first = read(self.path('settings.json'), 'rb')
        p = self.ok('hooks')
        self.assertIn('Agent and Stop hooks already present', p.stdout)
        self.assertEqual(read(self.path('settings.json'), 'rb'), first)

    def test_an_edited_stop_hook_entry_is_recognised_and_not_duplicated(self):
        edited = {'hooks': [
            {'type': 'command', 'command': "/usr/bin/python3 -I '/Users/me/.claude/hooks/remind-context-cap.py'",
             'timeout': 30}]}
        before = dict(SETTINGS, hooks=dict(SETTINGS['hooks'],
                                           PreToolUse=SETTINGS['hooks']['PreToolUse'] + [DISPATCH_GROUP],
                                           Stop=[edited]))
        self.write_settings(before)
        first = read(self.path('settings.json'), 'rb')
        p = self.ok('hooks')
        self.assertIn('Agent and Stop hooks already present', p.stdout)
        self.assertEqual(read(self.path('settings.json'), 'rb'), first)

    def test_hooks_keeps_another_stop_group(self):
        other = {'hooks': [{'type': 'command', 'command': 'other-tool hook'}]}
        self.write_settings(dict(SETTINGS, hooks=dict(SETTINGS['hooks'], Stop=[other])))
        self.ok('hooks')
        self.assertEqual(self.settings()['hooks']['Stop'], [other, STOP_GROUP])

    def test_the_dispatch_command_under_another_matcher_is_not_the_agent_hook(self):
        elsewhere = {'matcher': 'Bash', 'hooks': [DISPATCH_GROUP['hooks'][0]]}
        before = dict(SETTINGS, hooks=dict(SETTINGS['hooks'], PreToolUse=[elsewhere]))
        self.write_settings(before)
        self.ok('hooks')
        self.assertEqual(self.settings()['hooks']['PreToolUse'], [elsewhere, DISPATCH_GROUP])

    def test_invalid_settings_json_stops_without_touching_the_file(self):
        for bad in (b'{"theme": "dark",\n', b'["a list"]\n', b'', b'{"hooks": {"PreToolUse": {"not": "a list"}}}\n',
                    b'{"hooks": ["not", "an", "object"]}\n', b'{"hooks": {"Stop": {}}}\n'):
            with self.subTest(bad=bad):
                write(self.path('settings.json'), bad)
                p = self.run_install('hooks')
                self.assertEqual(p.returncode, 2)
                self.assertIn('settings.json', p.stderr)
                self.assertEqual(p.stdout, '')
                self.assertEqual(read(self.path('settings.json'), 'rb'), bad)
                self.assertEqual(os.listdir(self.claude), ['settings.json'])


class Section(HomeFixture):

    def expected_block(self):
        return START + '\n' + self.section + END + '\n'

    def test_section_replaces_between_markers_and_keeps_the_rest(self):
        before = '# notes\n- my notes\n\n' + START + '\nold text\nmore old text\n' + END + '\n\n@OTHER.md\n\n# other\nkeep me\n'
        write(self.path('CLAUDE.md'), before)
        self.ok('section')
        expected = '# notes\n- my notes\n\n' + self.expected_block() + '\n@OTHER.md\n\n# other\nkeep me\n'
        self.assertEqual(read(self.path('CLAUDE.md')), expected)
        self.ok('section')
        self.assertEqual(read(self.path('CLAUDE.md')), expected)
        self.assertEqual(read(self.path('CLAUDE.md')).count(START), 1)

    def test_section_appends_when_the_markers_are_absent(self):
        write(self.path('CLAUDE.md'), CLAUDE_MD)
        self.ok('section')
        self.assertEqual(read(self.path('CLAUDE.md')), CLAUDE_MD + '\n' + self.expected_block())
        self.ok('section')
        self.assertEqual(read(self.path('CLAUDE.md')), CLAUDE_MD + '\n' + self.expected_block())

    def test_section_appends_after_a_last_line_with_no_newline(self):
        write(self.path('CLAUDE.md'), '@OTHER.md')
        self.ok('section')
        self.assertEqual(read(self.path('CLAUDE.md')), '@OTHER.md\n\n' + self.expected_block())

    def test_section_creates_claude_md_when_missing(self):
        self.ok('section')
        self.assertEqual(read(self.path('CLAUDE.md')), self.expected_block())

    def test_malformed_markers_stop_without_touching_the_file(self):
        for bad in (CLAUDE_MD + START + '\nhalf\n', CLAUDE_MD + END + '\n' + START + '\n',
                    CLAUDE_MD + END + '\n', START + '\n' + END + '\n' + START + '\n' + END + '\n'):
            with self.subTest(bad=bad):
                write(self.path('CLAUDE.md'), bad)
                p = self.run_install('section')
                self.assertEqual(p.returncode, 2)
                self.assertIn('marker', p.stderr)
                self.assertEqual(read(self.path('CLAUDE.md')), bad)
                self.assertEqual(os.listdir(self.claude), ['CLAUDE.md'])


class AgentsAndScripts(HomeFixture):

    def test_agents_and_scripts_copy_into_place(self):
        write(self.path('agents', 'another-agent.md'), 'theirs\n')
        self.ok('agents')
        self.ok('scripts')
        self.assertEqual(sorted(os.listdir(self.path('agents'))), sorted(AGENTS + ['another-agent.md']))
        self.assertEqual(read(self.path('agents', 'another-agent.md')), 'theirs\n')
        for name in AGENTS:
            with self.subTest(name=name):
                self.assertEqual(read(self.path('agents', name), 'rb'), read(os.path.join(ROOT, 'agents', name), 'rb'))
        self.assertEqual(sorted(os.listdir(self.path('scripts'))), SCRIPTS)
        for name in SCRIPTS:
            with self.subTest(name=name):
                source = os.path.join(ROOT, 'scripts', name)
                self.assertEqual(read(self.path('scripts', name), 'rb'), read(source, 'rb'))
                self.assertEqual(mode_of(self.path('scripts', name)), mode_of(source))

    def test_skills_copy_into_place(self):
        write(self.path('skills', 'another-skill', 'SKILL.md'), 'theirs\n')
        self.ok('skills')
        self.assertEqual(sorted(os.listdir(self.path('skills'))), sorted(SKILLS + ['another-skill']))
        self.assertEqual(read(self.path('skills', 'another-skill', 'SKILL.md')), 'theirs\n')
        for name in SKILLS:
            with self.subTest(name=name):
                self.assertEqual(os.listdir(self.path('skills', name)), ['SKILL.md'])
                self.assertEqual(read(self.path('skills', name, 'SKILL.md'), 'rb'),
                                 read(os.path.join(ROOT, 'skills', name, 'SKILL.md'), 'rb'))

    def test_docs_copy_into_place(self):
        write(self.path('docs', 'another-doc.md'), 'theirs\n')
        self.ok('docs')
        self.assertEqual(sorted(os.listdir(self.path('docs'))), ['another-doc.md', 'kits.md'])
        self.assertEqual(read(self.path('docs', 'another-doc.md')), 'theirs\n')
        self.assertEqual(read(self.path('docs', 'kits.md'), 'rb'), read(os.path.join(ROOT, 'claude-md', 'kits.md'), 'rb'))

    def test_usage_errors_exit_2(self):
        for args in ((), ('nonsense',), ('rollback',), ('agents', 'extra'), ('docs', 'extra'), ('rollback', 'a', 'b')):
            with self.subTest(args=args):
                p = self.run_install(*args)
                self.assertEqual(p.returncode, 2)
                self.assertIn('usage', p.stderr.lower())
        self.assertEqual(os.listdir(self.claude), [])


class SnapshotAndRollback(HomeFixture):

    def snapshot(self):
        p = self.ok('snapshot')
        folder = p.stdout.strip()
        self.assertEqual(os.path.dirname(folder), os.path.join(self.home, '.agents', 'backups'))
        self.assertTrue(os.path.isdir(folder))
        return folder

    def seed_owner_files(self):
        self.write_settings(SETTINGS)
        write(self.path('CLAUDE.md'), CLAUDE_MD)
        write(self.path('hooks', 'guard-main-commit.py'), 'old guard\n')
        write(self.path('hooks', 'tests', 'test_hooks.py'), 'old tests\n')
        write(self.path('agents', 'another-agent.md'), 'theirs\n')

    def test_snapshot_copies_settings_claude_md_and_hooks(self):
        self.seed_owner_files()
        folder = self.snapshot()
        stamp = time.strftime('%Y-%m-%d-')
        self.assertTrue(os.path.basename(folder).startswith(stamp), folder)
        self.assertIn('-workflow', os.path.basename(folder))
        files = tree(folder)
        files.pop('snapshot.json')
        self.assertEqual(files, {
            'settings.json': read(self.path('settings.json'), 'rb'),
            'CLAUDE.md': CLAUDE_MD.encode(),
            os.path.join('hooks', 'guard-main-commit.py'): b'old guard\n',
            os.path.join('hooks', 'tests', 'test_hooks.py'): b'old tests\n',
        })

    def test_snapshot_never_reuses_an_existing_folder(self):
        backups = os.path.join(self.home, '.agents', 'backups')
        now = time.time()
        taken = [time.strftime('%Y-%m-%d-%H%M', time.localtime(t)) + '-workflow' for t in (now, now + 60)]
        for name in taken:
            write(os.path.join(backups, name, 'keep.txt'), 'earlier\n')
        folder = self.snapshot()
        self.assertNotIn(os.path.basename(folder), taken)
        for name in taken:
            self.assertEqual(os.listdir(os.path.join(backups, name)), ['keep.txt'])
        self.assertNotEqual(self.snapshot(), folder)

    def test_snapshot_tolerates_missing_files(self):
        folder = self.snapshot()
        self.assertEqual(list(tree(folder)), ['snapshot.json'])

    def test_rollback_restores_files_and_removes_only_our_agents(self):
        self.seed_owner_files()
        before = tree(self.claude)
        folder = self.snapshot()
        self.install_everything()
        self.assertNotEqual(tree(self.claude), before)
        p = self.ok('rollback', folder)
        self.assertEqual(tree(self.claude), before)
        self.assertEqual(os.listdir(self.path('agents')), ['another-agent.md'])
        self.assertEqual(os.listdir(self.path('scripts')), [])
        self.assertFalse(os.path.exists(self.path('skills', 'workflow-dashboard', 'SKILL.md')))
        self.assertIn('settings.json', p.stdout)

    def test_rollback_removes_settings_and_claude_md_install_created(self):
        folder = self.snapshot()
        self.install_everything()
        self.assertTrue(os.path.exists(self.path('settings.json')))
        self.assertTrue(os.path.exists(self.path('CLAUDE.md')))
        self.ok('rollback', folder)
        self.assertEqual(tree(self.claude), {})

    def test_rollback_refuses_a_folder_that_is_not_a_snapshot(self):
        self.seed_owner_files()
        before = tree(self.claude)
        incomplete = self.snapshot()
        os.unlink(os.path.join(incomplete, 'settings.json'))
        self.install_everything()
        installed = tree(self.claude)
        empty = os.path.join(self.root, 'not a snapshot')
        os.makedirs(empty)
        for folder in (empty, os.path.join(self.root, 'missing'), incomplete):
            with self.subTest(folder=folder):
                p = self.run_install('rollback', folder)
                self.assertEqual(p.returncode, 2)
                self.assertIn('snapshot', p.stderr)
                self.assertEqual(tree(self.claude), installed)
        self.assertNotEqual(installed, before)


class SpacedHome(HomeFixture):
    home_name = 'Application Support/home with spaces'

    def test_home_with_spaces(self):
        self.write_settings(SETTINGS)
        write(self.path('CLAUDE.md'), CLAUDE_MD)
        before = tree(self.claude)
        folder = self.ok('snapshot').stdout.strip()
        self.assertTrue(folder.startswith(os.path.join(self.home, '.agents', 'backups') + os.sep))
        self.install_everything()
        self.assertEqual(sorted(os.listdir(self.path('agents'))), AGENTS)
        self.assertEqual(sorted(os.listdir(self.path('scripts'))), SCRIPTS)
        self.assertEqual(self.settings(), SETTINGS_AFTER)
        self.assertIn(START, read(self.path('CLAUDE.md')))
        self.ok('rollback', folder)
        self.assertEqual(tree(self.claude), before)


if __name__ == '__main__':
    unittest.main()
