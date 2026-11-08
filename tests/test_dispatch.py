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
GP_FINAL = {'subagent_type': 'general-purpose', 'model': 'fable', 'description': 'Final review of family B diff'}
SIMPLIFY_REASON = ('No simplify since the last final review in this session\'s transcript. Run the simplify skill, '
                   'then dispatch the final review again.')
PLAN_REVIEW = {'subagent_type': 'plan-reviewer'}
PLAN_SIMPLIFY = {'subagent_type': 'plan-simplifier'}
PLAN_SIMPLIFY_REASON = ("No plan-simplifier since the last plan review in this session's transcript. Dispatch "
                        'plan-simplifier, apply its findings, then dispatch the plan review again.')
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


def tool_result(call_id, text='Review done.', is_error=False):
    block = {'type': 'tool_result', 'tool_use_id': call_id, 'content': text}
    if is_error:
        block['is_error'] = True
    return {'type': 'user', 'message': {'role': 'user', 'content': [block]}}


def at(row, time_of_day):
    return dict(row, timestamp=f'2026-11-07T{time_of_day}:00.000Z')


def launched_review(call_id='toolu_prev', tool_input=FINAL):
    return [tool_call('Agent', tool_input, call_id=call_id), tool_result(call_id)]


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
        for name in ['implementer', 'implementer-copy', 'implementer-escalated', 'plan-reviewer', 'plan-simplifier',
                     'task-reviewer', 'fix-checker', 'final-reviewer']:
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
        # simplify, then a launched final review, so both passes run: the review's id has to be looked up.
        write_rows(path, [skill_call('simplify')] + launched_review())
        fillers = [json.dumps(row, separators=(',', ':')).encode() + b'\n'
                   for row in [assistant_text('x' * 1000), tool_result('toolu_filler', 'x' * 1000)]]
        mention = json.dumps(assistant_text('simplify ' + 'x' * 1000), separators=(',', ':')).encode() + b'\n'
        with open(path, 'ab') as f:
            count = 0
            while f.tell() < 50 * 1024 * 1024:
                f.write(mention if count % 100 == 0 else fillers[count % 2])
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


class OneSimplifyPerFinalReview(HookFixture):
    def test_general_purpose_final_review_without_simplify_is_refused(self):
        self.assertEqual(self.reason(GP_FINAL, transcript=[]), SIMPLIFY_REASON)
        self.assertEqual(self.reason({'model': 'fable', 'description': 'final-review pass'}, transcript=[]), SIMPLIFY_REASON)
        self.assertEqual(self.reason(FINAL, transcript=[]), SIMPLIFY_REASON)
        self.assertIsNone(self.decision(GP_FINAL, transcript=[skill_call('simplify')]))

    def test_modelless_general_purpose_final_review_gets_the_unnamed_reason(self):
        tool_input = {'subagent_type': 'general-purpose', 'description': 'Final review of family B diff'}
        self.assertRegex(self.reason(tool_input, transcript=[]), 'named agent.*model')
        self.assertRegex(self.reason(tool_input, transcript=[skill_call('simplify')]), 'named agent.*model')

    def test_description_must_say_final_review(self):
        for tool_input in [{'subagent_type': 'general-purpose', 'model': 'fable', 'description': 'Review the plan wording'},
                           {'subagent_type': 'general-purpose', 'model': 'fable'},
                           {'subagent_type': 'general-purpose', 'model': 'fable', 'description': 5},
                           {'subagent_type': 'task-reviewer', 'description': 'Final review of family B diff'}]:
            self.assertIsNone(self.decision(tool_input, transcript=[]), tool_input)

    def test_fix_waves_and_rechecks_are_not_final_reviews(self):
        transcript = [skill_call('simplify')] + launched_review()
        for description in ['Final review fix wave', 'Fix-check final review findings']:
            tool_input = {'subagent_type': 'general-purpose', 'model': 'fable', 'description': description}
            self.assertIsNone(self.decision(tool_input, transcript=transcript), description)
        self.assertEqual(self.decision(GP_FINAL, transcript=transcript), 'deny')

    def test_second_final_review_needs_a_new_simplify(self):
        for previous in [FINAL, GP_FINAL]:
            transcript = [skill_call('simplify')] + launched_review(tool_input=previous)
            self.assertEqual(self.decision(FINAL, transcript=transcript), 'deny', previous)
            self.assertEqual(self.reason(GP_FINAL, transcript=transcript), SIMPLIFY_REASON, previous)

    def test_simplify_after_the_last_final_review_allows_the_next(self):
        transcript = [skill_call('simplify')] + launched_review() + [user_text('<command-name>/simplify</command-name>')]
        self.assertIsNone(self.decision(FINAL, transcript=transcript))
        self.assertIsNone(self.decision(GP_FINAL, transcript=transcript))

    def test_the_dispatch_being_judged_is_not_its_own_previous_review(self):
        transcript = [skill_call('simplify'), tool_call('Agent', FINAL, call_id='toolu_now')]
        self.assertIsNone(self.decision(FINAL, transcript=transcript))

    def test_a_failed_previous_final_review_does_not_count(self):
        for text in ["Agent type 'final-reviewer' not found", SIMPLIFY_REASON]:
            transcript = [skill_call('simplify'), tool_call('Agent', FINAL, call_id='toolu_prev'),
                          tool_result('toolu_prev', text, is_error=True)]
            self.assertIsNone(self.decision(FINAL, transcript=transcript), text)

    def test_report_text_saying_not_found_still_counts_as_launched(self):
        report = 'Final review: the old helper was not found in the diff, and the denied path is untested.'
        transcript = [skill_call('simplify'), tool_call('Agent', FINAL, call_id='toolu_prev'),
                      tool_result('toolu_prev', report)]
        self.assertEqual(self.decision(FINAL, transcript=transcript), 'deny')

    def test_order_across_parent_and_subagent_uses_timestamps(self):
        review = [at(row, '09:00') for row in launched_review()]
        self.assertIsNone(self.decision_in_subagent(FINAL, parent=[at(skill_call('simplify'), '10:00')], own=review))
        review = [at(row, '10:00') for row in launched_review()]
        self.assertEqual(self.decision_in_subagent(FINAL, parent=[at(skill_call('simplify'), '09:00')], own=review), 'deny')

    def test_a_row_without_a_timestamp_takes_the_previous_rows(self):
        own = [at(tool_call('Agent', {'subagent_type': 'Explore'}, call_id='toolu_x'), '10:00')] + launched_review()
        self.assertEqual(self.decision_in_subagent(FINAL, parent=[at(skill_call('simplify'), '09:00')], own=own), 'deny')


class PlanReviewNeedsPlanSimplify(HookFixture):
    def gp(self, description, model='fable'):
        return {'subagent_type': 'general-purpose', 'model': model, 'description': description}

    def test_plan_review_without_plan_simplify_is_refused(self):
        self.assertEqual(self.reason(PLAN_REVIEW, transcript=[]), PLAN_SIMPLIFY_REASON)

    def test_launched_plan_simplifier_allows_the_plan_review(self):
        transcript = launched_review('toolu_simplify', PLAN_SIMPLIFY)
        self.assertIsNone(self.decision(PLAN_REVIEW, transcript=transcript))

    def test_general_purpose_plan_simplify_with_a_model_counts(self):
        transcript = launched_review('toolu_simplify', self.gp('Plan simplify of the build plan', model='opus'))
        self.assertIsNone(self.decision(PLAN_REVIEW, transcript=transcript))

    def test_a_check_that_mentions_plan_simplifier_is_not_a_plan_simplify(self):
        transcript = launched_review('toolu_check', self.gp('Builder check on plan-simplifier spec', model='opus'))
        self.assertEqual(self.reason(PLAN_REVIEW, transcript=transcript), PLAN_SIMPLIFY_REASON)

    def test_general_purpose_plan_review_with_a_model_is_gated(self):
        self.assertEqual(self.reason(self.gp('Plan review of the build plan'), transcript=[]), PLAN_SIMPLIFY_REASON)

    def test_second_plan_review_needs_a_new_plan_simplify(self):
        transcript = launched_review('toolu_simplify', PLAN_SIMPLIFY) + launched_review('toolu_prev', PLAN_REVIEW)
        self.assertEqual(self.reason(PLAN_REVIEW, transcript=transcript), PLAN_SIMPLIFY_REASON)

    def test_a_failed_previous_plan_review_does_not_count(self):
        for text in ["Agent type 'plan-reviewer' not found", PLAN_SIMPLIFY_REASON]:
            transcript = launched_review('toolu_simplify', PLAN_SIMPLIFY) + [
                tool_call('Agent', PLAN_REVIEW, call_id='toolu_prev'), tool_result('toolu_prev', text, is_error=True)]
            self.assertIsNone(self.decision(PLAN_REVIEW, transcript=transcript), text)

    def test_a_plan_simplifier_that_was_not_found_does_not_unlock(self):
        transcript = [tool_call('Agent', PLAN_SIMPLIFY, call_id='toolu_simplify'),
                      tool_result('toolu_simplify', "Agent type 'plan-simplifier' not found", is_error=True)]
        self.assertEqual(self.decision(PLAN_REVIEW, transcript=transcript), 'deny')

    def test_simplify_skill_agents_are_not_plan_simplifies(self):
        transcript = launched_review('toolu_angle', self.gp('Simplify: reuse angle', model='opus'))
        self.assertEqual(self.decision(PLAN_REVIEW, transcript=transcript), 'deny')

    def test_plan_review_fix_wording_is_not_a_plan_review(self):
        for description in ['Plan review fix', 'Fix-check plan review findings']:
            self.assertIsNone(self.decision(self.gp(description), transcript=[]), description)

    def test_plan_simplify_in_the_parent_counts_for_a_subagent(self):
        review = [at(row, '09:00') for row in launched_review('toolu_prev', PLAN_REVIEW)]
        simplify = [at(row, '10:00') for row in launched_review('toolu_simplify', PLAN_SIMPLIFY)]
        self.assertIsNone(self.decision_in_subagent(PLAN_REVIEW, parent=simplify, own=review))
        review = [at(row, '10:00') for row in launched_review('toolu_prev', PLAN_REVIEW)]
        simplify = [at(row, '09:00') for row in launched_review('toolu_simplify', PLAN_SIMPLIFY)]
        self.assertEqual(self.decision_in_subagent(PLAN_REVIEW, parent=simplify, own=review), 'deny')

    def test_the_gates_do_not_unlock_each_other(self):
        self.assertEqual(self.reason(PLAN_REVIEW, transcript=[skill_call('simplify')]), PLAN_SIMPLIFY_REASON)
        transcript = launched_review('toolu_simplify', PLAN_SIMPLIFY)
        self.assertEqual(self.reason(FINAL, transcript=transcript), SIMPLIFY_REASON)

    def test_a_general_purpose_plan_review_re_review_is_gated(self):
        for description in ['Plan re-review of the build plan', 'plan-rereview']:
            self.assertEqual(self.reason(self.gp(description), transcript=[]), PLAN_SIMPLIFY_REASON, description)


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

    def test_each_dispatch_is_judged_by_the_transcript_up_to_it(self):
        write_rows(f'{self.projects}/p/s.jsonl', [
            skill_call('simplify'),
            self.dispatch('toolu_a', subagent_type='final-reviewer', description='final review A'),
            tool_result('toolu_a'),
            self.dispatch('toolu_b', subagent_type='general-purpose', model='fable', description='final review B'),
        ])

        p = self.replay()

        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertRegex(p.stdout, r'(?m)^Dispatches: 2, denied 1, silent 1$')
        self.assertRegex(p.stdout, r'(?m)^final-reviewer +0 +1$')
        self.assertRegex(p.stdout, r'(?m)^general-purpose +1 +0$')

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

    def test_a_relative_projects_folder_still_finds_a_subagents_session(self):
        # The hook stays silent when it cannot read the session, so a dangling link would hide this denial.
        session = f'{self.projects}/p/s'
        write_rows(f'{session}.jsonl', [user_text('start')])
        write_rows(f'{session}/subagents/agent-x.jsonl', [
            self.dispatch('toolu_1', subagent_type='final-reviewer', description='review without simplify'),
        ])

        p = subprocess.run([PYTHON, '-I', REPLAY, '--projects', 'Application Support/projects'], capture_output=True,
                           text=True, timeout=60, env=CLEAN_ENV, cwd=self.root)

        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertRegex(p.stdout, r'(?m)^Dispatches: 1, denied 1, silent 0$')

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
