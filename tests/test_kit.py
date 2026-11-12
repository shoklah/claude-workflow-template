"""Tests for the kit lookup. They run the real script against a copy of the stand-in kit in a temp folder.

Run with: /usr/bin/python3 -I tests/test_kit.py
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
SCRIPT = os.path.join(SCRIPTS_DIR, 'kit.py')
STANDIN = os.path.join(HERE, 'kits', 'standin')
PYTHON = '/usr/bin/python3'

# Copied from tests/test_failing_check.py, since tests can't import each other: keep them the same.
CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}


class Kit(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix='kit-test-'))
        self.kits = os.path.join(self.root, 'kits')
        shutil.copytree(STANDIN, os.path.join(self.kits, 'standin'))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_script(self, *args):
        p = subprocess.run([PYTHON, '-I', SCRIPT, *args], cwd=self.root, capture_output=True, text=True,
                           env=CLEAN_ENV, timeout=60)
        return p.returncode, p.stdout, p.stderr

    def show(self, name, *args):
        rc, stdout, stderr = self.run_script('show', name, '--kits', self.kits, *args)
        self.assertEqual(rc, 0, stdout + stderr)
        return json.loads(stdout)

    def write_manifest(self, manifest):
        with open(os.path.join(self.kits, 'standin', 'kit.json'), 'w') as f:
            f.write(manifest if isinstance(manifest, str) else json.dumps(manifest))

    def manifest(self, **changes):
        with open(os.path.join(STANDIN, 'kit.json')) as f:
            data = json.load(f)
        for key, value in changes.items():
            if value is None:
                data.pop(key)
            else:
                data[key] = value
        return data

    def test_show_prints_the_manifest_with_full_paths(self):
        out = self.show('standin')
        folder = os.path.join(self.kits, 'standin')
        self.assertEqual({k: out[k] for k in ('status', 'format', 'name', 'version', 'folder')},
                         {'status': 'ok', 'format': 1, 'name': 'standin', 'version': '0.1.0', 'folder': folder})
        self.assertEqual(out['applies_when'], "The task changes the stand-in's sample file.")
        self.assertEqual([out['design'], out['guidance'], out['checklist']],
                         [os.path.join(folder, f) for f in ('design.md', 'guidance.md', 'checklist.md')])
        self.assertEqual(out['checks'], [{'name': 'sample', 'command': f'/usr/bin/python3 -I {folder}/checks/sample.py'}])
        self.assertEqual(out['bindings'], [{'read': os.path.join(folder, 'extra/always.md'), 'tag': None},
                                           {'read': os.path.join(folder, 'extra/motion.md'), 'tag': 'motion'}])
        self.assertEqual(out['stacks'], ['alpha'])

    def test_a_listed_stack_adds_its_entries(self):
        out = self.show('standin', '--stack', 'alpha')
        self.assertIn({'read': os.path.join(self.kits, 'standin', 'stacks/alpha/notes.md'), 'tag': None},
                      out['bindings'])

    def test_an_unlisted_stack_still_gets_the_core(self):
        out = self.show('standin', '--stack', 'beta')
        self.assertEqual((out['status'], len(out['bindings'])), ('ok', 2))

    def test_optional_keys_left_out_read_as_null_or_empty(self):
        self.write_manifest({'format': 1, 'name': 'standin', 'version': '0.1.0', 'applies_when': 'Always.'})
        out = self.show('standin')
        self.assertEqual([out[k] for k in ('design', 'guidance', 'checklist', 'checks', 'bindings', 'stacks')],
                         [None, None, None, [], [], []])

    def test_refusals_exit_1_with_a_reason(self):
        cases = [
            ('no folder for the name', 'missing', None, 'no kit'),
            ('no kit.json', 'standin', 'remove', 'kit.json'),
            ('kit.json not JSON', 'standin', '{not json', 'kit.json'),
            ('format 2', 'standin', self.manifest(format=2), 'format'),
            ('name differs from the folder', 'standin', self.manifest(name='other'), 'name'),
            ('applies_when missing', 'standin', self.manifest(applies_when=None), 'applies_when'),
            ('version missing', 'standin', self.manifest(version=None), 'version'),
            ('a named file is missing', 'standin', self.manifest(guidance='nowhere.md'), 'nowhere.md'),
            ('bindings not a list', 'standin', self.manifest(bindings='x'), 'bindings'),
            ('a check not an object', 'standin', self.manifest(checks=[3]), 'checks'),
            ('a binding with no read', 'standin', self.manifest(bindings=[{'tag': 'motion'}]), 'bindings'),
            ('a check with no command', 'standin', self.manifest(checks=[{'name': 'sample'}]), 'checks'),
        ]
        for label, name, manifest, word in cases:
            with self.subTest(label):
                self.tearDown()
                self.setUp()
                if manifest == 'remove':
                    os.remove(os.path.join(self.kits, 'standin', 'kit.json'))
                elif manifest is not None:
                    self.write_manifest(manifest)
                rc, stdout, stderr = self.run_script('show', name, '--kits', self.kits)
                self.assertEqual((rc, stderr), (1, ''))
                out = json.loads(stdout)
                self.assertEqual(out['status'], 'refused')
                self.assertIn(word, out['reason'])

    def test_usage_errors_exit_2_with_nothing_on_stdout(self):
        for args in [(), ('nonsense',), ('show',), ('show', 'standin', 'extra'), ('show', 'standin', '--stack')]:
            with self.subTest(args):
                rc, stdout, stderr = self.run_script(*args)
                self.assertEqual((rc, stdout), (2, ''))
                self.assertTrue(stderr)


if __name__ == '__main__':
    unittest.main()
