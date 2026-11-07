"""Tests for the dispatch hook and its replay tool. They run the real scripts against real temp folders.

Run with: /usr/bin/python3 -I tests/test_dispatch.py
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
# Hooks sit in ../hooks in the repo and in .. once installed to ~/.claude/hooks/tests.
HOOKS_DIR = os.path.join(HERE, '..', 'hooks') if os.path.isdir(os.path.join(HERE, '..', 'hooks')) else os.path.join(HERE, '..')
HOOK = os.path.join(HOOKS_DIR, 'guard-agent-dispatch.py')
# The replay tool stays in the repo, so its tests are skipped once this file is installed.
REPLAY = os.path.join(HERE, '..', 'tools', 'replay_dispatches.py')
PYTHON = '/usr/bin/python3'
CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}

DENY = [{'description': 'x'}, {'subagent_type': 'general-purpose', 'description': 'x'}]
SILENT = [
    {'subagent_type': 'general-purpose', 'model': 'opus'}, {'model': 'sonnet'},
    {'subagent_type': 'implementer'}, {'subagent_type': 'Explore'}, {'subagent_type': 'fork'},
]
FINAL = {'subagent_type': 'final-reviewer'}
OUTPUT_KEYS = ['hookEventName', 'permissionDecision', 'permissionDecisionReason']


def assistant(*blocks):
    return {'type': 'assistant', 'message': {'role': 'assistant', 'content': list(blocks)}}


def assistant_text(text):
    return assistant({'type': 'text', 'text': text})


def tool_call(name, tool_input, call_id='toolu_1'):
    return assistant({'type': 'tool_use', 'id': call_id, 'name': name, 'input': tool_input})


def skill_call(skill, **extra):
    return tool_call('Skill', dict(skill=skill, **extra))


def user_text(text):
    return {'type': 'user', 'message': {'role': 'user', 'content': text}}


def write_rows(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        for row in rows:
            if isinstance(row, bytes):
                f.write(row + b'\n')
            else:
                f.write(json.dumps(row, separators=(',', ':')).encode() + b'\n')


class TempFolder(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='dispatch-test-')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        # The owner's folders include Application Support, so every path here has a space in it.
        self.projects = f'{self.root}/Application Support/projects'
        os.makedirs(self.projects)


class HookFixture(TempFolder):
    def run_stdin(self, text):
        return subprocess.run([PYTHON, '-I', HOOK], input=text, capture_output=True, text=True, timeout=30, env=CLEAN_ENV)

    def run_hook(self, tool_input, transcript=None, path=None, tool='Agent'):
        """transcript is a list of rows to write and point at; path sends that transcript_path as it is."""
        payload = {'hook_event_name': 'PreToolUse', 'tool_name': tool, 'tool_input': tool_input}
        if transcript is not None:
            path = f'{self.projects}/session.jsonl'
            write_rows(path, transcript)
        if path is not None:
            payload['transcript_path'] = path
        p = self.run_stdin(json.dumps(payload))
        self.assertEqual(p.returncode, 0, p.stderr)
        if not p.stdout.strip():
            return None
        out = json.loads(p.stdout)
        self.assertEqual(sorted(out), ['hookSpecificOutput'])
        out = out['hookSpecificOutput']
        self.assertEqual(sorted(out), OUTPUT_KEYS)
        self.assertEqual(out['hookEventName'], 'PreToolUse')
        return out

    def decision(self, *args, **kwargs):
        out = self.run_hook(*args, **kwargs)
        return out and out['permissionDecision']

    def reason(self, *args, **kwargs):
        return self.run_hook(*args, **kwargs)['permissionDecisionReason']

    def decision_in_subagent(self, tool_input, parent, own=()):
        session = f'{self.projects}/session'
        own_path = f'{session}/subagents/agent-x.jsonl'
        write_rows(own_path, own)
        write_rows(f'{session}.jsonl', parent)
        return self.decision(tool_input, path=own_path)


class UnnamedDispatches(HookFixture):
    def test_unnamed_dispatch_without_a_model_is_refused(self):
        for tool_input in DENY:
            self.assertEqual(self.decision(tool_input), 'deny')
        self.assertRegex(self.reason(DENY[0]), 'named agent.*model')

    def test_the_refusal_names_the_agents_to_use(self):
        reason = self.reason(DENY[0])
        for name in ['implementer', 'implementer-copy', 'implementer-escalated', 'plan-reviewer', 'task-reviewer',
                     'fix-checker', 'final-reviewer']:
            self.assertIn(name, reason)

    def test_everything_else_stays_silent(self):
        for tool_input in SILENT:
            self.assertIsNone(self.decision(tool_input))

    def test_an_empty_type_or_model_counts_as_missing(self):
        self.assertEqual(self.decision({'subagent_type': ''}), 'deny')
        self.assertEqual(self.decision({'subagent_type': 'general-purpose', 'model': ''}), 'deny')
        self.assertEqual(self.decision({'subagent_type': None, 'model': None}), 'deny')

    def test_other_tools_are_ignored(self):
        for tool in ['Bash', 'Task', 'Write', 'agent']:
            self.assertIsNone(self.decision(DENY[0], tool=tool))
            self.assertIsNone(self.decision(FINAL, transcript=[], tool=tool))
        self.assertEqual(self.run_stdin(json.dumps({'tool_input': DENY[0]})).stdout, '')

    def test_output_never_says_allow_or_carries_updated_input(self):
        payloads = [{'tool_name': 'Agent', 'tool_input': tool_input} for tool_input in DENY + SILENT]
        write_rows(f'{self.projects}/empty.jsonl', [])
        payloads.append({'tool_name': 'Agent', 'tool_input': FINAL, 'transcript_path': f'{self.projects}/empty.jsonl'})
        denied = 0
        for payload in payloads:
            stdout = self.run_stdin(json.dumps(payload)).stdout
            denied += bool(stdout)
            self.assertNotIn('allow', stdout.lower())
            self.assertNotIn('updatedInput', stdout)
        self.assertEqual(denied, 3)


class FinalReviewNeedsSimplify(HookFixture):
    def test_final_review_requires_simplify_in_the_transcript(self):
        self.assertEqual(self.decision(FINAL, transcript=[assistant_text('run simplify next')]), 'deny')
        self.assertRegex(self.reason(FINAL, transcript=[]), 'simplify')
        self.assertIsNone(self.decision(FINAL, transcript=[skill_call('simplify')]))
        self.assertIsNone(self.decision(FINAL, transcript=[user_text('<command-name>/simplify</command-name>')]))

    def test_a_model_does_not_excuse_final_review_from_simplify(self):
        self.assertEqual(self.decision({'subagent_type': 'final-reviewer', 'model': 'opus'}, transcript=[]), 'deny')

    def test_a_slash_command_in_text_blocks_counts(self):
        row = {'type': 'user', 'message': {'role': 'user', 'content': [
            {'type': 'text', 'text': '<command-message>simplify</command-message>\n<command-name>/simplify</command-name>'},
        ]}}
        self.assertIsNone(self.decision(FINAL, transcript=[row]))

    def test_typed_command_forms_in_a_string_count(self):
        for text in ['<command-name>/simplify</command-name>',
                     '<command-message>simplify</command-message>\n<command-name>/simplify</command-name>\n<command-args></command-args>',
                     '\n <command-name>/simplify</command-name>']:
            self.assertIsNone(self.decision(FINAL, transcript=[user_text(text)]), text)

    def test_a_user_message_quoting_the_tag_mid_text_does_not_count(self):
        tag = '<command-name>/simplify</command-name>'
        quoted = 'The hook looks for ' + tag + ' in the transcript.'
        block = {'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'text', 'text': quoted}]}}
        for row in [user_text(quoted), user_text('<command-message>x</command-message> then ' + tag), block]:
            self.assertEqual(self.decision(FINAL, transcript=[row]), 'deny', row)

    def test_things_that_only_mention_simplify_do_not_count(self):
        tag = '<command-name>/simplify</command-name>'
        mentions = [
            user_text('please simplify this'),
            user_text('/simplify'),
            assistant_text(tag),
            skill_call('code-review', args='simplify'),
            skill_call('simplify-more'),
            tool_call('Bash', {'skill': 'simplify', 'command': 'echo simplify'}),
            # A file read back by a tool, such as a plan that quotes the tag, is not running the command.
            {'type': 'user', 'message': {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': tag},
            ]}},
            {'type': 'progress', 'data': {'message': skill_call('simplify')}},
            {'type': 'queue-operation', 'content': tag},
        ]
        for row in mentions:
            self.assertEqual(self.decision(FINAL, transcript=[row]), 'deny', row)
        self.assertEqual(self.decision(FINAL, transcript=mentions), 'deny')

    def test_a_broken_line_does_not_hide_a_later_call(self):
        broken = [b'{"type":"assistant","message":"simplify', b'\xff\xfe simplify', b'"simplify"', b'[1, "simplify"]',
                  b'{"type":"user","message":{"content":5},"x":"simplify"}',
                  b'{"type":"assistant","message":{"content":["simplify"]}}']
        self.assertEqual(self.decision(FINAL, transcript=broken), 'deny')
        self.assertIsNone(self.decision(FINAL, transcript=broken + [skill_call('simplify')]))

    def test_final_review_with_a_subagent_transcript_path_also_checks_the_parent(self):
        # transcript_path is <session>/subagents/agent-x.jsonl; simplify is only in <session>.jsonl.
        # The real path a nested hook receives is not yet confirmed live.
        self.assertIsNone(self.decision_in_subagent(FINAL, parent=[skill_call('simplify')]))

    def test_a_subagent_transcript_counts_on_its_own(self):
        self.assertIsNone(self.decision_in_subagent(FINAL, parent=[], own=[skill_call('simplify')]))

    def test_a_subagent_is_refused_when_neither_transcript_shows_simplify(self):
        self.assertEqual(self.decision_in_subagent(FINAL, parent=[assistant_text('simplify')], own=[]), 'deny')

    def test_a_workflow_subagent_path_finds_the_session_transcript(self):
        session = f'{self.projects}/session'
        own = f'{session}/subagents/workflows/wf_1/agent-x.jsonl'
        write_rows(own, [])
        write_rows(f'{session}.jsonl', [skill_call('simplify')])
        self.assertIsNone(self.decision(FINAL, path=own))

    def test_a_50_megabyte_transcript_is_scanned_in_under_2_seconds(self):
        path = f'{self.projects}/huge.jsonl'
        filler = json.dumps(assistant_text('x' * 1000), separators=(',', ':')).encode() + b'\n'
        mention = json.dumps(assistant_text('simplify ' + 'x' * 1000), separators=(',', ':')).encode() + b'\n'
        with open(path, 'wb') as f:
            count = 0
            while f.tell() < 50 * 1024 * 1024:
                f.write(mention if count % 100 == 0 else filler)
                count += 1
        self.assertGreaterEqual(os.path.getsize(path), 50 * 1024 * 1024)

        start = time.monotonic()
        self.assertEqual(self.decision(FINAL, path=path), 'deny')
        self.assertLess(time.monotonic() - start, 2)

        with open(path, 'ab') as f:
            f.write(json.dumps(skill_call('simplify')).encode() + b'\n')
        start = time.monotonic()
        self.assertIsNone(self.decision(FINAL, path=path))
        self.assertLess(time.monotonic() - start, 2)


class NeverBlocksOnBadInput(HookFixture):
    def assert_silent(self, text):
        p = self.run_stdin(text)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, '', text)

    def test_missing_transcript_and_bad_input_never_block(self):
        for text in ['not json', '', '{}', '[]', 'null', '5', '"Agent"']:
            self.assert_silent(text)
        for tool_input in ['x', 5, None, ['subagent_type'], True]:
            self.assert_silent(json.dumps({'tool_name': 'Agent', 'tool_input': tool_input}))
        self.assert_silent(json.dumps({'tool_name': 'Agent'}))

    def test_stdin_that_is_not_text_never_blocks(self):
        p = subprocess.run([PYTHON, '-I', HOOK], input=b'\xff\xfe\x00', capture_output=True, timeout=30, env=CLEAN_ENV)
        self.assertEqual((p.returncode, p.stdout), (0, b''))

    def test_final_review_without_a_transcript_path_stays_silent(self):
        self.assertIsNone(self.decision(FINAL))
        self.assertIsNone(self.decision(FINAL, path=''))
        for path in [0, 5, ['x'], {'a': 1}, True]:
            self.assertIsNone(self.decision(FINAL, path=path))
        self.assertIsNone(self.decision(FINAL, path=f'{self.projects}/null\x00byte.jsonl'))

    def test_final_review_with_an_unreadable_transcript_stays_silent(self):
        self.assertIsNone(self.decision(FINAL, path=f'{self.projects}/no such file.jsonl'))
        self.assertIsNone(self.decision(FINAL, path=self.projects))
        locked = f'{self.projects}/locked.jsonl'
        write_rows(locked, [])
        os.chmod(locked, 0)
        self.assertIsNone(self.decision(FINAL, path=locked))

    def test_a_subagent_whose_parent_is_missing_stays_silent(self):
        own = f'{self.projects}/session/subagents/agent-x.jsonl'
        write_rows(own, [assistant_text('no call here')])
        self.assertIsNone(self.decision(FINAL, path=own))

    def test_a_missing_subagent_transcript_stays_silent_whatever_the_parent_says(self):
        own = f'{self.projects}/session/subagents/agent-x.jsonl'
        write_rows(f'{self.projects}/session.jsonl', [assistant_text('no call here')])
        self.assertIsNone(self.decision(FINAL, path=own))
        write_rows(f'{self.projects}/session.jsonl', [skill_call('simplify')])
        self.assertIsNone(self.decision(FINAL, path=own))

    def test_a_reader_that_went_away_still_exits_0(self):
        payload = json.dumps({'tool_name': 'Agent', 'tool_input': DENY[0]})
        reader, writer = os.pipe()
        os.close(reader)
        self.addCleanup(os.close, writer)
        p = subprocess.run([PYTHON, '-I', HOOK], input=payload.encode(), stdout=writer, stderr=subprocess.PIPE,
                           timeout=30, env=CLEAN_ENV)
        self.assertEqual(p.returncode, 0, p.stderr)


@unittest.skipUnless(os.path.exists(REPLAY), 'the replay tool is not installed with the hooks')
class ReplayTool(TempFolder):
    def replay(self, tool=REPLAY, projects=None):
        return subprocess.run([PYTHON, '-I', tool, '--projects', projects or self.projects], capture_output=True,
                              text=True, timeout=60, env=CLEAN_ENV)

    def dispatch(self, call_id, **tool_input):
        return tool_call('Agent', tool_input, call_id=call_id)

    def test_counts_every_dispatch_by_type_through_the_real_hook(self):
        session = f'{self.projects}/-Users-me-Application-Support/session-a'
        duplicate = self.dispatch('toolu_a1', description='no type at all')
        write_rows(f'{session}.jsonl', [
            duplicate,
            duplicate,  # The same call is logged again by a fork or a resumed session.
            self.dispatch('toolu_a2', subagent_type='general-purpose', description='general no model'),
            self.dispatch('toolu_a3', subagent_type='general-purpose', model='opus', description='general with model'),
            self.dispatch('toolu_a4', subagent_type='Explore', description='explore'),
            self.dispatch('toolu_a5', subagent_type='final-reviewer', description='review without simplify'),
            tool_call('Bash', {'command': 'echo "Agent"'}),
            tool_call('Task', {'description': 'old tool name'}),
            user_text('the "Agent" tool'),
            {'type': 'user', 'message': {'role': 'user', 'content': [
                {'type': 'tool_use', 'id': 'toolu_user', 'name': 'Agent', 'input': {'description': 'not assistant'}},
            ]}},
            {'type': 'progress', 'data': {'message': self.dispatch('toolu_nested', description='nested progress')}},
            b'{"torn line with "Agent"',
        ])
        write_rows(f'{session}/subagents/agent-x.jsonl', [
            self.dispatch('toolu_b1', subagent_type='general-purpose', description='general in a subagent'),
            skill_call('simplify'),
            self.dispatch('toolu_b2', subagent_type='final-reviewer', description='review after simplify'),
        ])
        with open(f'{session}/subagents/agent-x.meta.json', 'w') as f:
            f.write('{"name":"Agent"}')

        p = self.replay()

        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertRegex(p.stdout, r'(?m)^Dispatches: 7, denied 4, silent 3$')
        self.assertRegex(p.stdout, r'(?m)^\(missing\) +1 +0$')
        self.assertRegex(p.stdout, r'(?m)^general-purpose +2 +1$')
        self.assertRegex(p.stdout, r'(?m)^Explore +0 +1$')
        self.assertRegex(p.stdout, r'(?m)^final-reviewer +1 +1$')
        self.assertRegex(p.stdout, r'(?m)^Denied with a model: 0$')
        self.assertRegex(p.stdout, r'(?m)^Hook failures: 0$')
        self.assertRegex(p.stdout, r'(?m)^Bad lines or unreadable files skipped: 1$')
        for description in ['no type at all', 'general no model', 'general in a subagent', 'review without simplify']:
            self.assertIn(description, p.stdout)
        for description in ['general with model', 'explore', 'review after simplify', 'old tool name', 'not assistant',
                            'nested progress']:
            self.assertNotIn(description, p.stdout)

    def test_lists_at_most_20_denied_descriptions(self):
        write_rows(f'{self.projects}/p/s.jsonl', [
            self.dispatch(f'toolu_{n}', description=f'denied number {n}.') for n in range(25)
        ] + [tool_call('Agent', 'not a dict', call_id='toolu_bad')])

        p = self.replay()

        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertRegex(p.stdout, r'(?m)^Dispatches: 26, denied 25, silent 1$')
        self.assertRegex(p.stdout, r'(?m)^\(missing\) +25 +1$')
        self.assertEqual(p.stdout.count('denied number'), 20)

    def test_dispatches_with_no_id_are_each_counted(self):
        block = {'type': 'tool_use', 'name': 'Agent', 'input': {'subagent_type': 'Explore'}}
        write_rows(f'{self.projects}/p/s.jsonl', [assistant(block), assistant(block)])
        self.assertRegex(self.replay().stdout, r'(?m)^Dispatches: 2, denied 0, silent 2$')

    def test_a_denial_with_a_model_is_counted(self):
        write_rows(f'{self.projects}/p/s.jsonl', [self.dispatch('toolu_1', subagent_type='final-reviewer', model='opus')])
        self.assertRegex(self.replay().stdout, r'(?m)^Denied with a model: 1$')

    def test_a_hook_that_breaks_its_contract_is_reported_not_hidden(self):
        os.makedirs(f'{self.root}/tools')
        os.makedirs(f'{self.root}/hooks')
        copy = shutil.copy(REPLAY, f'{self.root}/tools/')
        with open(f'{self.root}/hooks/guard-agent-dispatch.py', 'w') as f:
            f.write('import sys\nsys.exit(3)\n')
        write_rows(f'{self.projects}/p/s.jsonl', [self.dispatch('toolu_1', description='x')])

        p = self.replay(tool=copy)

        self.assertEqual(p.returncode, 1)
        self.assertRegex(p.stdout, r'(?m)^Hook failures: 1$')
        self.assertRegex(p.stdout, r'(?m)^Dispatches: 0, denied 0, silent 0$')

    def test_a_projects_folder_that_does_not_exist_is_an_error(self):
        p = self.replay(projects=f'{self.projects}/nope')
        self.assertEqual(p.returncode, 2)
        self.assertIn('nope', p.stderr)

    def test_reads_the_transcripts_without_changing_them(self):
        path = f'{self.projects}/p/s.jsonl'
        write_rows(path, [self.dispatch('toolu_1', description='x')])
        before = sorted(os.listdir(os.path.dirname(path))), os.path.getmtime(path), os.path.getsize(path)
        self.assertEqual(self.replay().returncode, 0)
        self.assertEqual((sorted(os.listdir(os.path.dirname(path))), os.path.getmtime(path), os.path.getsize(path)), before)


if __name__ == '__main__':
    unittest.main()
