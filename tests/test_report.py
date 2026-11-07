"""Tests for the workflow report. They run the real script against a fixture transcript tree and run log in a temp folder.

Run with: /usr/bin/python3 -I tests/test_report.py
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
# The script sits in ../scripts in the repo and in .. once installed to ~/.claude/scripts/tests.
SCRIPTS_DIR = os.path.join(HERE, '..', 'scripts') if os.path.isdir(os.path.join(HERE, '..', 'scripts')) else os.path.join(HERE, '..')
SCRIPT = os.path.join(SCRIPTS_DIR, 'workflow-report.py')
PYTHON = '/usr/bin/python3'
CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
NOW = '2026-11-07T12:00:00Z'  # a Saturday; its week started Sunday 2026-11-01 07:00 UTC

ADVISOR = {'type': 'advisor_message', 'model': 'claude-fable-5-1', 'input_tokens': 70000, 'output_tokens': 1600,
           'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}


def usage(iterations=None, input_tokens=10, output_tokens=10):
    u = {'input_tokens': input_tokens, 'output_tokens': output_tokens, 'cache_read_input_tokens': 0,
         'cache_creation_input_tokens': 0}
    if iterations is not None:
        u['iterations'] = iterations
    return u


def executor(input_tokens=10, output_tokens=10):
    return dict(usage(None, input_tokens, output_tokens), type='message')


def asst(mid, content=None, model='claude-opus-5-5', iterations=None, ts='2026-11-05T10:00:00.000Z', u=None):
    return {'type': 'assistant', 'timestamp': ts,
            'message': {'id': mid, 'model': model, 'role': 'assistant',
                        'content': content if content is not None else [{'type': 'text', 'text': 'ok'}],
                        'usage': u or usage(iterations)}}


def user(content, ts='2026-11-05T10:00:00.000Z', **extra):
    row = {'type': 'user', 'timestamp': ts, 'message': {'role': 'user', 'content': content}}
    row.update(extra)
    return row


def tool_use(tid, name, inp=None):
    return {'type': 'tool_use', 'id': tid, 'name': name, 'input': inp or {}}


def hook_row(command, stdout, ts='2026-11-05T10:00:00.000Z'):
    return {'type': 'attachment', 'timestamp': ts,
            'attachment': {'type': 'hook_success', 'hookName': 'PreToolUse:Bash', 'hookEvent': 'PreToolUse',
                           'stdout': stdout, 'command': command, 'exitCode': 0}}


def decision(dec, reason):
    return json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': dec,
                                              'permissionDecisionReason': reason}}) + '\n'


def run_record(**over):
    rec = {'date': '2026-11-05T12:00:00Z', 'project': '/p', 'branch': 'b', 'base': 'x', 'head': 'y',
           'path': 'full', 'risk_area': False, 'stage': '1', 'advisor': True, 'fable_start': 10, 'fable_end': 20,
           'all_start': 10, 'all_end': 20, 'tasks': 1, 'fix_rounds': 0, 'stops': 0,
           'final_findings': {'critical': 0, 'important': 0, 'minor': 0, 'unneeded': 0},
           'simplify': {'applied': 0, 'skipped': 0}, 'failing_test': []}
    rec.update(over)
    return rec


def section(out, title):
    """Lines of the report section that starts with `title`, up to the next unindented line."""
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(title):
            body = [line]
            for nxt in lines[i + 1:]:
                if nxt and not nxt.startswith(' '):
                    break
                body.append(nxt)
            return body
    raise AssertionError(f'no section {title!r} in:\n{out}')


class Report(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix='workflow report test '))
        self.projects = os.path.join(self.root, 'projects dir')
        self.log = os.path.join(self.root, 'log dir', 'workflow-log.jsonl')
        os.makedirs(self.projects)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write_lines(self, path, rows):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'a') as f:
            for row in rows:
                f.write((row if isinstance(row, str) else json.dumps(row)) + '\n')

    def session(self, rows, project='-Users-me-Work-proj', sid='sess-1'):
        self.write_lines(os.path.join(self.projects, project, sid + '.jsonl'), rows)

    def subagent(self, agent_type, rows, project='-Users-me-Work-proj', sid='sess-1', aid='a1'):
        d = os.path.join(self.projects, project, sid, 'subagents')
        self.write_lines(os.path.join(d, f'agent-{aid}.jsonl'), rows)
        with open(os.path.join(d, f'agent-{aid}.meta.json'), 'w') as f:
            json.dump({'agentType': agent_type, 'description': 'd', 'toolUseId': 't', 'spawnDepth': 1}, f)

    def records(self, recs):
        self.write_lines(self.log, recs)

    def run_report(self, *args):
        p = subprocess.run([PYTHON, '-I', SCRIPT, '--projects', self.projects, '--log', self.log, '--now', NOW, *args],
                           capture_output=True, text=True, env=CLEAN_ENV, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stderr, '')
        return p.stdout

    def test_advisor_calls_counted_once_per_message_with_fable_cost(self):
        # the same message id appears on two rows
        row = asst('msg_1', iterations=[executor(), ADVISOR])
        self.session([row, row])
        out = self.run_report()
        line = section(out, 'Advisor calls:')[0]
        self.assertIn('calls=1', line)
        self.assertIn('input=70000', line)
        self.assertIn('output=1600', line)
        self.assertIn('$0.78', line)

    def test_calls_by_role_flag_roles_that_should_not_consult(self):
        # subagent meta agentType task-reviewer is flagged; implementer is not; a main session on Fable is flagged
        self.subagent('task-reviewer', [asst('m_r', iterations=[executor(), ADVISOR])], aid='r1')
        self.subagent('implementer', [asst('m_i', iterations=[executor(), ADVISOR])], aid='i1')
        self.session([asst('m_f', model='claude-fable-5-1', iterations=[executor(), ADVISOR])], sid='sess-fable')
        self.session([asst('m_o', iterations=[executor(), ADVISOR])], sid='sess-opus')
        roles = {line.strip().split(':')[0]: line for line in section(self.run_report(), 'Advisor calls by role:')[1:]}
        self.assertEqual(sorted(roles), ['implementer', 'main', 'main on Fable', 'task-reviewer'])
        self.assertIn('calls=1', roles['task-reviewer'])
        self.assertIn('FLAG', roles['task-reviewer'])
        self.assertIn('FLAG', roles['main on Fable'])
        self.assertNotIn('FLAG', roles['implementer'])
        self.assertNotIn('FLAG', roles['main'])

    def test_workflow_subagent_transcripts_are_read_with_their_role(self):
        # <project>/<session>/subagents/workflows/<id>/agent-x.jsonl, with its .meta.json beside it
        d = os.path.join(self.projects, '-Users-me-Work-proj', 'sess-1', 'subagents', 'workflows', 'wf 1')
        self.write_lines(os.path.join(d, 'agent-w1.jsonl'), [asst('m_w', iterations=[executor(), ADVISOR])])
        with open(os.path.join(d, 'agent-w1.meta.json'), 'w') as f:
            json.dump({'agentType': 'workflow-subagent', 'description': 'd', 'toolUseId': 't', 'spawnDepth': 1}, f)
        out = self.run_report()
        roles = {line.strip().split(':')[0]: line for line in section(out, 'Advisor calls by role:')[1:]}
        self.assertEqual(sorted(roles), ['workflow-subagent'])
        self.assertIn('calls=1', roles['workflow-subagent'])
        self.assertIn('calls=1', section(out, 'Advisor calls:')[0])

    def test_failed_calls_by_error_code(self):
        # an advisor_tool_result whose content has error_code overloaded: 'overloaded=1'
        failed = {'type': 'advisor_tool_result', 'tool_use_id': 'srvtoolu_1',
                  'content': {'type': 'advisor_tool_result_error', 'error_code': 'overloaded'}}
        ok = {'type': 'advisor_tool_result', 'tool_use_id': 'srvtoolu_2',
              'content': {'type': 'advisor_redacted_result', 'encrypted_content': 'xx', 'stop_reason': 'end_turn'}}
        self.session([asst('m1', content=[failed]), asst('m1', content=[failed]), asst('m2', content=[ok])])
        line = section(self.run_report(), 'Advisor failed calls:')[0]
        self.assertIn('overloaded=1', line)
        self.assertNotIn('redacted', line)

    def test_notes_counted_and_missing_notes_reported(self):
        # 2 calls, 1 note with changed: yes: 'notes=1 changed=1 missing=1'
        note = 'Done.\nAdvisor: check the cache path | changed: yes | moved the lookup\nMore.'
        self.session([asst('m1', iterations=[executor(), ADVISOR]),
                      asst('m2', iterations=[executor(), ADVISOR]),
                      asst('m3', content=[{'type': 'text', 'text': note}]),
                      asst('m4', content=[{'type': 'text', 'text': 'Advisor: no pipe format here'}])])
        out = self.run_report()
        self.assertIn('notes=1 changed=1 missing=1', section(out, 'Advisor notes:')[0])
        self.assertIn('Consultations with no note: 1', self.run_report('--brief'))

    def test_same_note_text_in_two_messages_counts_twice(self):
        note = [{'type': 'text', 'text': 'Advisor: rerun it | changed: no | nothing'}]
        row = asst('m3', content=note)
        self.session([asst('m1', iterations=[executor(), ADVISOR]), asst('m2', iterations=[executor(), ADVISOR]),
                      row, row, asst('m4', content=note)])
        self.assertIn('notes=2 changed=0 missing=0', section(self.run_report(), 'Advisor notes:')[0])

    def test_week_share_and_stop_flag_at_25_percent(self):
        # week starts Sunday 07:00 UTC; advisor $30 of $100 Fable: '30%' and 'STOP' in --brief
        advisor_30 = dict(ADVISOR, input_tokens=3000000, output_tokens=0)  # 3M at $10/Mtok
        self.session([
            asst('m_adv', iterations=[executor(), advisor_30], ts='2026-11-01T07:00:00.000Z'),
            asst('m_fable', model='claude-fable-5-1', u=usage([executor(input_tokens=7000000, output_tokens=0)]),
                 ts='2026-11-06T09:00:00.000Z'),
            # one minute before the week starts: belongs to the previous week
            asst('m_old', model='claude-fable-5-1', u=usage([executor(input_tokens=100000000, output_tokens=0)]),
                 ts='2026-11-01T06:59:00.000Z'),
        ])
        brief = self.run_report('--brief')
        self.assertLessEqual(len(brief.splitlines()), 3)
        share = brief.splitlines()[0]
        self.assertIn('30%', share)
        self.assertIn('$30.00 of $100.00', share)
        self.assertIn('STOP', share)
        weeks = section(self.run_report(), 'Weeks')
        self.assertTrue(any(line.strip().startswith('2026-11-01') and '30%' in line and 'STOP' in line
                            for line in weeks), weeks)
        self.assertTrue(any(line.strip().startswith('2026-10-25') and '0%' in line and 'STOP' not in line
                            for line in weeks), weeks)

    def test_week_share_at_or_below_25_percent_has_no_stop(self):
        advisor_25 = dict(ADVISOR, input_tokens=2500000, output_tokens=0)
        self.session([
            asst('m_adv', iterations=[executor(), advisor_25], ts='2026-11-02T07:00:00.000Z'),
            asst('m_fable', model='claude-fable-5-1', u=usage([executor(input_tokens=7500000, output_tokens=0)]),
                 ts='2026-11-06T09:00:00.000Z'),
        ])
        share = self.run_report('--brief').splitlines()[0]
        self.assertIn('25%', share)
        self.assertNotIn('STOP', share)

    def test_runs_compared_for_advisor_on_and_off(self):
        # sums findings, fix_rounds and stops per group, and derives failing_test totals from the per-task list
        self.records([
            run_record(advisor=True, tasks=2, fix_rounds=1, stops=0,
                       final_findings={'critical': 1, 'important': 2, 'minor': 0, 'unneeded': 1},
                       failing_test=[{'task': 1, 'result': 'passed'}, {'task': 2, 'result': 'failed'}]),
            run_record(advisor=True, tasks=3, fix_rounds=2, stops=1,
                       final_findings={'critical': 0, 'important': 1, 'minor': 4, 'unneeded': 0},
                       failing_test=[{'task': 1, 'result': 'passed'}, {'task': 2, 'result': 'couldnt_run'},
                                     {'task': 3, 'result': 'passed'}]),
            run_record(advisor=False, tasks=1, fix_rounds=0, stops=2,
                       final_findings={'critical': 0, 'important': 0, 'minor': 1, 'unneeded': 3},
                       failing_test=[{'task': 1, 'result': 'exempt'}, {'task': 2, 'result': 'no_tests'}]),
        ])
        groups = section(self.run_report(), 'Runs')
        on = [line for line in groups if 'advisor=on' in line]
        off = [line for line in groups if 'advisor=off' in line]
        self.assertEqual(len(on), 1, groups)
        self.assertEqual(len(off), 1, groups)
        self.assertIn('runs=2 tasks=5 critical=1 important=3 minor=4 unneeded=1 fix_rounds=3 stops=1 '
                      'passed=3 failed=1 no_tests=0 couldnt_run=1 exempt=0', on[0])
        self.assertIn('runs=1 tasks=1 critical=0 important=0 minor=1 unneeded=3 fix_rounds=0 stops=2 '
                      'passed=0 failed=0 no_tests=1 couldnt_run=0 exempt=1', off[0])

    def test_malformed_lines_are_skipped_and_counted(self):
        # truncated lines in a transcript and in the log: rc 0, 'skipped=2'
        good = json.dumps(asst('m1', iterations=[executor(), ADVISOR]))
        self.session([good[:40], good])
        self.records([json.dumps(run_record())[:30], run_record()])
        out = self.run_report()
        self.assertIn('skipped=2', out)
        self.assertIn('calls=1', section(out, 'Advisor calls:')[0])
        self.assertIn('runs=1', [line for line in section(out, 'Runs') if 'advisor=on' in line][0])

    def test_later_fix_commits_on_the_same_files_within_14_days(self):
        # real temp repo: a fix commit on a run's file at day 3 is listed; one on another file or at day 20 is not
        repo = os.path.join(self.root, 'repo with spaces')
        os.makedirs(repo)

        def git(*args, date=None):
            env = dict(CLEAN_ENV)
            if date:
                env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
            return subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True, text=True,
                                  env=env).stdout.strip()

        def change(name, message, date):
            with open(os.path.join(repo, name), 'a') as f:
                f.write(message + '\n')
            git('add', '-A')
            git('commit', '-q', '-m', message, date=date)
            return git('rev-parse', 'HEAD')

        git('init', '-q', '-b', 'main')
        git('config', 'user.email', 't@example.test')
        git('config', 'user.name', 't')
        git('config', 'commit.gpgsign', 'false')
        change('other file.txt', 'base other', '2026-09-30T10:00:00Z')
        base = change('run file.txt', 'base', '2026-09-30T11:00:00Z')
        head = change('run file.txt', 'feat: the run', '2026-10-01T10:00:00Z')
        change('run file.txt', 'fix: day three bug', '2026-10-04T12:00:00Z')
        change('run file.txt', 'FIX: shouted day five', '2026-10-06T12:00:00Z')
        change('run file.txt', 'feat: not a fix', '2026-10-07T12:00:00Z')
        change('run file.txt', 'chore: fix later in subject', '2026-10-07T13:00:00Z')
        change('other file.txt', 'fix: another file', '2026-10-04T13:00:00Z')
        change('run file.txt', 'fix: day twenty', '2026-10-21T12:00:00Z')
        self.records([run_record(date='2026-10-01T12:00:00Z', project=repo, branch='feature', base=base, head=head)])
        fixes = '\n'.join(section(self.run_report(), 'Later fix commits'))
        self.assertIn('fix: day three bug', fixes)
        self.assertIn('FIX: shouted day five', fixes)
        self.assertIn(repo, fixes)
        for absent in ('not a fix', 'fix later in subject', 'another file', 'day twenty', 'feat: the run'):
            self.assertNotIn(absent, fixes)

    def test_hook_decisions_counted_by_reason(self):
        # attachment hook_success stdout with ask or deny from each guard, grouped by hook and reason
        commit_guard = '/usr/bin/python3 -I "$HOME/.claude/hooks/guard-main-commit.py"'
        dispatch_guard = '/usr/bin/python3 -I "$HOME/.claude/hooks/guard-agent-dispatch.py"'
        rewrite = json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                                                     'permissionDecisionReason': 'auto-rewrite',
                                                     'updatedInput': {'command': 'git status --short'}}})
        self.session([hook_row(commit_guard, decision('ask', 'Commit on main')),
                      hook_row(commit_guard, decision('ask', 'Commit on main')),
                      hook_row(commit_guard, decision('ask', 'Branch unknown')),
                      hook_row('other-hook run', rewrite),
                      hook_row('other-hook run', decision('allow', 'auto-rewrite')),
                      hook_row(commit_guard, 'not json')])
        self.subagent('implementer', [hook_row(dispatch_guard, decision('deny', 'No model given'))])
        hooks = section(self.run_report(), 'Hook decisions:')
        body = [line.strip() for line in hooks[1:] if line.strip()]
        self.assertEqual(sorted(body), ['guard-agent-dispatch.py deny x1: No model given',
                                        'guard-main-commit.py ask x1: Branch unknown',
                                        'guard-main-commit.py ask x2: Commit on main'])

    def test_questions_per_1000_main_session_calls(self):
        # typed messages, AskUserQuestion and wrap-up with ask, as rates
        self.session([
            user('please build it'),
            user([{'type': 'text', 'text': 'and test it'}]),
            user('<system-reminder>meta</system-reminder>', isMeta=True),
            user([{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'out'}]),
            asst('m1', content=[tool_use('t1', 'Bash', {'command': 'ls'})]),
            asst('m1', content=[tool_use('t1', 'Bash', {'command': 'ls'})]),  # same block on a repeated row
            asst('m2', content=[tool_use('t2', 'AskUserQuestion', {'questions': []})]),
            asst('m3', content=[tool_use('t3', 'mcp__ccd_turn__wrap_up', {'recap': 'r', 'ask': 'Commit?'})]),
            asst('m4', content=[tool_use('t4', 'mcp__ccd_turn__wrap_up', {'recap': 'r', 'ask': ''})]),
        ])
        self.subagent('implementer', [user('subagent prompt'),
                                      asst('s1', content=[tool_use('s1t', 'AskUserQuestion', {})])])
        line = section(self.run_report(), 'Questions:')[0]
        self.assertIn('main_calls=4 typed=2 (500.0 per 1000) ask_user_question=1 (250.0 per 1000) '
                      'wrap_up_ask=1 (250.0 per 1000)', line)

    def test_typed_messages_use_origin_when_the_transcript_has_it(self):
        # newer transcripts mark typed rows origin.kind human; task notifications and injected rows are not typed
        human = {'kind': 'human'}
        self.session([
            user('please build it', origin=human),
            user('<task-notification><task-id>b1</task-id></task-notification>', origin={'kind': 'task-notification'}),
            user('<system-reminder>Linked sessions now</system-reminder>'),
            user('This session is being continued', isCompactSummary=True),
            asst('m1', content=[tool_use('t1', 'Bash', {'command': 'ls'})]),
        ], sid='new')
        # an older transcript without origin falls back to content type, still without compaction summaries
        self.session([user('older typed'), user('This session is being continued', isCompactSummary=True)], sid='old')
        self.assertIn('main_calls=1 typed=2 ', section(self.run_report(), 'Questions:')[0])

    def test_trial_progress_in_brief(self):
        # 5 full runs with advisor true: 'runs 5/5' and 'CHECKPOINT'
        self.records([run_record() for _ in range(5)] +
                     [run_record(path='small'), run_record(advisor=False)])
        trial = self.run_report('--brief').splitlines()[1]
        self.assertIn('runs 5/5, weeks 0/3', trial)
        self.assertIn('CHECKPOINT', trial)

    def test_trial_weeks_count_whole_weeks_since_first_advisor_run(self):
        self.records([run_record(date='2026-10-30T12:00:00Z'), run_record(path='small', date='2026-10-24T12:00:00Z')])
        trial = self.run_report('--brief').splitlines()[1]
        self.assertIn('runs 1/5, weeks 2/3', trial)
        self.assertNotIn('CHECKPOINT', trial)
        self.records([run_record(advisor=True, path='small', date='2026-10-17T11:00:00Z')])
        trial = self.run_report('--brief').splitlines()[1]
        self.assertIn('runs 1/5, weeks 3/3', trial)
        self.assertIn('CHECKPOINT', trial)

    def test_since_drops_older_rows_and_runs(self):
        self.session([asst('m_old', iterations=[executor(), ADVISOR], ts='2026-10-01T10:00:00.000Z'),
                      asst('m_new', iterations=[executor(), ADVISOR], ts='2026-11-03T10:00:00.000Z')])
        self.records([run_record(date='2026-10-01T12:00:00Z'), run_record(date='2026-11-03T12:00:00Z')])
        out = self.run_report('--since', '2026-11-01')
        self.assertIn('calls=1', section(out, 'Advisor calls:')[0])
        self.assertIn('runs=1', [line for line in section(out, 'Runs') if 'advisor=on' in line][0])

    def test_schema_lists_every_run_record_field(self):
        schema = json.loads(self.run_report('--schema'))
        self.assertEqual(sorted(schema), sorted(run_record()))

    def test_missing_inputs_give_an_empty_report(self):
        shutil.rmtree(self.projects)
        out = self.run_report()
        self.assertIn('calls=0', section(out, 'Advisor calls:')[0])
        self.assertIn('notes=0 changed=0 missing=0', out)
        self.assertIn('skipped=0', out)
        self.assertIn('main_calls=0', out)
        brief = self.run_report('--brief').splitlines()
        self.assertEqual(len(brief), 3)
        self.assertIn('0%', brief[0])
        self.assertNotIn('STOP', brief[0])
        self.assertIn('runs 0/5, weeks 0/3', brief[1])
        self.assertIn('Consultations with no note: 0', brief[2])


if __name__ == '__main__':
    unittest.main()
