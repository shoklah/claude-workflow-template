"""Tests for the workflow report. They run the real script against a fixture transcript tree and run log in a temp folder.

Run with: /usr/bin/python3 -I tests/test_report.py
"""
import datetime
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


def asst(mid, content=None, model='claude-opus-5-5', iterations=None, ts='2026-11-05T10:00:00.000Z', u=None, effort=None):
    row = {'type': 'assistant', 'timestamp': ts,
           'message': {'id': mid, 'model': model, 'role': 'assistant',
                       'content': content if content is not None else [{'type': 'text', 'text': 'ok'}],
                       'usage': u or usage(iterations)}}
    if effort is not None:
        row['effort'] = effort
    return row


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


def startup_row(ts='2026-11-05T10:00:00.000Z'):
    """The SessionStart hook's row that injects the startup guidelines."""
    return {'type': 'attachment', 'timestamp': ts,
            'attachment': {'type': 'hook_success', 'hookName': 'SessionStart:startup', 'stdout': STARTUP}}


def decision(dec, reason):
    return json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': dec,
                                              'permissionDecisionReason': reason}}) + '\n'


def run_record(**over):
    rec = {'date': '2026-11-05T12:00:00Z', 'project': '/p', 'branch': 'b', 'base': 'x', 'head': 'y',
           'path': 'full', 'risk_area': False, 'stage': '1', 'advisor': True, 'fable_start': 10, 'fable_end': 20,
           'all_start': 10, 'all_end': 20, 'tasks': 1, 'fix_rounds': 0, 'stops': 0,
           'final_findings': {'critical': 0, 'important': 0, 'minor': 0, 'unneeded': 0},
           'simplify': {'ran': 'agents', 'applied': 0, 'skipped': 0}, 'failing_test': [],
           'fix_growth': {'task_added': 0, 'task_removed': 0, 'fix_added': 0, 'fix_removed': 0, 'unplanned_files': []}}
    rec.update(over)
    return rec


def plan_record(**over):
    """A schema 2 plan record, which leaves out the fields a plan record has no use for."""
    return {k: v for k, v in run_record(path='plan', schema=2, steps=[], **over).items()
            if k not in ('tasks', 'fix_rounds', 'failing_test', 'fix_growth', 'simplify', 'base', 'head')}


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

    def subagent(self, agent_type, rows, project='-Users-me-Work-proj', sid='sess-1', aid='a1', tid='t'):
        d = os.path.join(self.projects, project, sid, 'subagents')
        self.write_lines(os.path.join(d, f'agent-{aid}.jsonl'), rows)
        with open(os.path.join(d, f'agent-{aid}.meta.json'), 'w') as f:
            json.dump({'agentType': agent_type, 'description': 'd', 'toolUseId': tid, 'spawnDepth': 1}, f)

    def records(self, recs):
        self.write_lines(self.log, recs)

    def repo(self):
        """A temp git repo and change(name, message, date=None, text=None), which appends text (by default the
        message) to a file, commits it and returns the commit."""
        repo = os.path.join(self.root, 'repo with spaces')
        os.makedirs(repo)

        def git(*args, date=None):
            env = dict(CLEAN_ENV)
            if date:
                env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
            return subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True, text=True,
                                  env=env).stdout.strip()

        def change(name, message, date=None, text=None):
            with open(os.path.join(repo, name), 'a') as f:
                f.write(message + '\n' if text is None else text)
            git('add', '-A')
            git('commit', '-q', '-m', message, date=date)
            return git('rev-parse', 'HEAD')

        git('init', '-q', '-b', 'main')
        git('config', 'user.email', 't@example.test')
        git('config', 'user.name', 't')
        git('config', 'commit.gpgsign', 'false')
        return repo, change

    def run_report(self, *args, env=CLEAN_ENV):
        # No test reads the real ~/.claude/agents or export stamp; a test's own option comes later and wins.
        p = subprocess.run([PYTHON, '-I', SCRIPT, '--projects', self.projects, '--log', self.log, '--now', NOW,
                            '--agents', os.path.join(self.root, 'no agents'),
                            '--export-stamp', os.path.join(self.root, 'no stamp'), *args],
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stderr, '')
        return p.stdout

    def json(self, *args):
        return json.loads(self.run_report('--json', *args))

    def html(self):
        page = os.path.join(self.root, 'page.html')
        self.run_report('--html', page)
        with open(page, encoding='utf-8') as f:
            return f.read()

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

    def test_plan_simplifier_should_not_consult(self):
        self.subagent('plan-simplifier', [asst('m_s', iterations=[executor(), ADVISOR])], aid='s1')
        roles = {line.strip().split(':')[0]: line for line in section(self.run_report(), 'Advisor calls by role:')[1:]}
        self.assertIn('FLAG', roles['plan-simplifier'])

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
        self.session([startup_row(), asst('m1', iterations=[executor(), ADVISOR]),
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

    def test_cost_is_priced_by_model_and_cache_ttl(self):
        one_h = {'input_tokens': 0, 'output_tokens': 0, 'cache_read_input_tokens': 0,
                 'cache_creation_input_tokens': 1000000,
                 'cache_creation': {'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': 1000000}}
        self.session([asst('m1', model='claude-fable-5-1', u=dict(one_h, iterations=[dict(one_h, type='message')]))])
        self.assertIn('of $20.00)', section(self.run_report(), 'Weeks from Sunday')[1])

    def test_a_split_message_counts_once_from_its_fullest_row(self):
        partial = executor(input_tokens=1000000, output_tokens=1)
        full = executor(input_tokens=1000000, output_tokens=1000000)
        self.session([asst('same', model='claude-fable-5-1', u=dict(partial, iterations=[partial])),
                      asst('same', model='claude-fable-5-1', u=dict(full, iterations=[full]))])
        self.assertIn('of $60.00)', section(self.run_report(), 'Weeks from Sunday')[1])

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
        repo, change = self.repo()
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

    def test_now_drops_later_records_and_rows(self):
        self.session([user('typed early', ts='2026-11-05T10:00:00.000Z'),
                      user('typed late', ts='2026-11-08T10:00:00.000Z')])
        self.records([run_record(date='2026-11-08T09:00:00Z', branch='later')])
        out = self.run_report()  # NOW is 2026-11-07T12:00Z
        self.assertNotIn('later', out)
        self.assertIn('typed=1 ', out)

    def test_schema_lists_every_run_record_field(self):
        schema = json.loads(self.run_report('--schema'))
        self.assertLessEqual(set(run_record()), set(schema))
        self.assertEqual(sorted(set(schema) - set(run_record())),
                         ['archive', 'offers', 'plan', 'schema', 'started', 'steps', 'waves'])

    def test_schema_lists_the_schema_2_fields_and_outcomes(self):
        schema = json.loads(self.run_report('--schema'))
        for key in ('schema', 'started', 'plan', 'archive', 'steps'):
            self.assertIn(key, schema)
        for outcome in ('fixed', 'applied', 'declined', 'deferred', 'ruled', 'open', 'to_owner'):
            self.assertIn(outcome, schema['steps'])
        self.assertIn('"plan"', schema['path'])

    def test_schema_1_and_2_records_and_a_plan_record_are_all_read(self):
        step = {'step': 'final_review', 'agent': 'final-reviewer', 'model': 'fable', 'effort': 'high',
                'counts': {'critical': 0, 'important': 1, 'minor': 0, 'unneeded': 0},
                'findings': [{'id': 'F1', 'severity': 'important', 'summary': 's', 'outcome': 'fixed'}]}
        plan = plan_record(started='2026-11-05T09:00:00+01:00')
        self.records([run_record(branch='old'), run_record(branch='new', schema=2, steps=[step]), plan])
        out = self.run_report()
        self.assertEqual(len([l for l in out.splitlines() if l.startswith('    2026-11-05')]), 3)
        self.assertIn('path=plan', out)
        self.assertIn('runs 2/5', self.run_report('--brief'))  # the two Full runs count, the plan record doesn't

    def test_malformed_steps_are_dropped_not_fatal(self):
        steps = ['not a dict', {'step': 'nope'}, {'step': 'task_review', 'task': 1, 'round': 0,
                 'counts': {'critical': '1'}, 'findings': 'not a list'}]
        self.records([run_record(schema=2, steps=steps)])
        self.assertIn('path=full', self.run_report())

    def test_schema_lists_simplify_ran_and_fix_growth(self):
        schema = json.loads(self.run_report('--schema'))
        for name in ('"ran"', '"agents"', '"inline"', '"no"'):
            self.assertIn(name, schema['simplify'])
        for name in ('task_added', 'task_removed', 'fix_added', 'fix_removed', 'unplanned_files'):
            self.assertIn(name, schema['fix_growth'])

    def test_run_line_shows_simplify_mode_and_fix_growth(self):
        growth = {'task_added': 40, 'task_removed': 5, 'fix_added': 120, 'fix_removed': 10, 'unplanned_files': []}
        self.records([run_record(branch='feature/grow', simplify={'ran': 'inline', 'applied': 1, 'skipped': 0},
                                 fix_growth=growth)])
        line = [line for line in section(self.run_report(), 'Runs') if 'feature/grow' in line][0]
        self.assertIn('simplify inline', line)
        self.assertIn('fix +120/task +40', line)

    def test_brief_warns_when_simplify_did_not_run(self):
        # one consultation with no note, so the notes line would otherwise be there
        self.session([startup_row(), asst('m1', iterations=[executor(), ADVISOR])])
        self.records([run_record(branch='feature/skipped', simplify=RAN_NO),
                      run_record(branch='feature/last-week', date='2026-10-30T12:00:00Z',
                                 simplify=RAN_NO)])
        brief = self.run_report('--brief').splitlines()
        self.assertLessEqual(len(brief), 3)
        # the one more is this run's own "no session transcript found"; last week's run is not counted
        self.assertEqual(brief[2], 'WARN: simplify did not run on feature/skipped (2026-11-05) (+1 more, run --health)')
        self.assertNotIn('Consultations with no note', '\n'.join(brief))
        self.assertIn('missing=1', section(self.run_report(), 'Advisor notes:')[0])

    def test_brief_counts_further_warnings(self):
        no = RAN_NO
        self.records([run_record(branch='one', simplify=no), run_record(branch='two', simplify=no),
                      run_record(branch='three', simplify=no)])
        brief = self.run_report('--brief').splitlines()
        self.assertEqual(len(brief), 3)
        # the 2 other simplify warnings, then the 3 runs' "no session transcript found"
        self.assertEqual(brief[2], 'WARN: simplify did not run on one (2026-11-05) (+5 more, run --health)')

    def stamp(self, text):
        path = os.path.join(self.root, 'stamp.json')
        with open(path, 'w') as f:
            f.write(text)
        return path

    def test_brief_notes_commits_since_the_last_export(self):
        repo, change = self.repo()
        exported = change('a.txt', 'one')
        change('a.txt', 'two')
        head = change('a.txt', 'three')
        subprocess.run(['git', '-C', repo, 'update-ref', 'refs/remotes/origin/main', head], check=True, env=CLEAN_ENV)
        stamp = self.stamp(json.dumps({'repo': repo, 'sha': exported, 'date': '2026-11-08T17:44:28Z'}))
        brief = self.run_report('--brief', '--export-stamp', stamp).splitlines()
        self.assertEqual(len(brief), 3)
        self.assertTrue(brief[2].endswith(
            '; Template: 2 commits on main since the last export (2026-11-08T17:44:28Z), run /workflow-export'),
            brief[2])

    def test_brief_is_silent_without_a_usable_stamp(self):
        repo, change = self.repo()
        head = change('a.txt', 'one')
        subprocess.run(['git', '-C', repo, 'update-ref', 'refs/remotes/origin/main', head], check=True, env=CLEAN_ENV)
        date = '2026-11-08T17:44:28Z'
        cases = {
            'no stamp file': None,
            'not JSON': '{not json',
            'missing repo': json.dumps({'repo': os.path.join(self.root, 'gone'), 'sha': head, 'date': date}),
            'unknown sha': json.dumps({'repo': repo, 'sha': '0' * 40, 'date': date}),
            'template current': json.dumps({'repo': repo, 'sha': head, 'date': date}),
        }
        for name, text in cases.items():
            with self.subTest(name):
                path = os.path.join(self.root, 'missing.json') if text is None else self.stamp(text)
                brief = self.run_report('--brief', '--export-stamp', path).splitlines()
                self.assertEqual(len(brief), 3)
                self.assertNotIn('Template:', brief[2])

    def test_old_records_are_unknown_not_flagged(self):
        old = run_record(branch='feature/old', simplify={'applied': 0, 'skipped': 0})
        del old['fix_growth']
        self.records([old])
        # its only warning is the missing transcript, not "simplify did not run"
        self.assertEqual(self.run_report('--brief').splitlines()[2],
                         'WARN: feature/old (2026-11-05): no session transcript found')
        out = self.run_report()
        self.assertNotIn('WARN', out)
        line = [line for line in section(out, 'Runs') if 'feature/old' in line][0]
        self.assertIn('simplify unknown', line)
        self.assertNotIn('fix +', line)

    def test_json_has_its_version_and_top_level_keys(self):
        data = self.json()
        self.assertEqual(data['version'], 1)
        self.assertEqual(sorted(data), ['generated', 'now', 'runs', 'sessions', 'since', 'totals', 'version'])
        self.assertEqual(sorted(data['totals']),
                         ['advisor', 'health', 'models', 'offers', 'run_time', 'scoreboard', 'skills', 'weeks'])
        self.assertEqual(data['now'], '2026-11-07T12:00:00+00:00')
        self.assertIsNone(data['since'])

    def test_html_page_is_self_contained_and_escaped(self):
        step = {'step': 'final_review', 'counts': {'critical': 0, 'important': 1, 'minor': 0, 'unneeded': 0},
                'findings': [{'id': 'F1', 'severity': 'important', 'summary': '<b>x</b> & "y"', 'outcome': 'declined',
                              'reason': 'r'}]}
        self.records([run_record(branch='old'), run_record(branch='a&b', schema=2, steps=[step])])
        page = os.path.join(self.root, 'page.html')
        self.assertIn('Wrote ' + page, self.run_report('--html', page))
        with open(page, encoding='utf-8') as f:
            html = f.read()
        self.assertTrue(html.startswith('<!DOCTYPE html>'))
        self.assertIn('<title>Workflow dashboard</title>', html)
        self.assertNotIn('<script', html)
        self.assertIsNone(re.search(r'\b(src|href)\s*=', html))
        self.assertIn('&lt;b&gt;x&lt;/b&gt; &amp; &quot;y&quot;', html)
        self.assertIn('a&amp;b', html)
        self.assertIn('before steps', html)
        self.assertEqual(html.count('<details class="run"'), 2)
        titles = ('Rules and steps', 'Whole workflow', 'Skills', 'Models', 'Advisor', 'Runs', 'Health')
        positions = [html.index(f'<h2>{title}</h2>') for title in titles]
        self.assertEqual(positions, sorted(positions))

    def test_run_time_compares_full_runs_with_and_without_waves(self):
        wave = [{'wave': 'A', 'tasks': [1, 2], 'conflicts': 0}]
        self.records([run_record(schema=2, started='2026-11-05T10:00:00Z', waves=wave, steps=[]),
                      run_record(schema=2, started='2026-11-05T11:00:00Z', steps=[]),
                      run_record(schema=2, started='2026-11-05T11:30:00Z', steps=[]),
                      run_record(schema=2, steps=[]),
                      run_record(schema=2, started='2026-11-05T08:00:00Z', path='small', steps=[])])
        self.assertEqual(self.json()['totals']['run_time'],
                         {'with_waves': {'runs': 1, 'timed': 1, 'median_minutes': 120.0},
                          'without_waves': {'runs': 3, 'timed': 2, 'median_minutes': 45.0}})
        html = self.html()
        self.assertIn('<tr><td>with waves</td><td>1</td><td>1</td><td>120</td></tr>', html)
        self.assertIn('<tr><td>without waves</td><td>3</td><td>2</td><td>45</td></tr>', html)

    def test_a_run_across_the_weekly_reset_has_unknown_points(self):
        self.records([run_record(),
                      run_record(fable_start=90, fable_end=5, all_start=80, all_end=6),
                      run_record(started='2026-11-01T06:00:00Z', date='2026-11-01T09:00:00Z',
                                 fable_start=30, fable_end=35, all_start=30, all_end=35)])
        data = self.json()
        self.assertEqual([(run['fable_points'], run['all_points']) for run in data['runs']],
                         [(None, None), (10, 10), (None, None)])
        weeks = data['totals']['weeks']
        self.assertEqual([(w['week'], w['fable_points'], w['all_points']) for w in weeks],
                         [('2026-11-01', 10, 10)])
        self.assertIn('Points: Fable 10, all models 10.', self.html())

    def test_html_defaults_to_the_home_dashboard_path(self):
        os.makedirs(os.path.join(self.root, '.claude'))
        page = os.path.join(self.root, '.claude', 'workflow-dashboard.html')
        self.assertEqual(self.run_report('--html', env=dict(CLEAN_ENV, HOME=self.root)), f'Wrote {page}\n')
        with open(page, encoding='utf-8') as f:
            self.assertTrue(f.read().startswith('<!DOCTYPE html>'))

    def test_html_shows_a_schema_1_runs_cleaned_totals(self):
        # A count given as a string reads as 0, on the run as on the scoreboard.
        self.records([run_record(final_findings={'critical': '2', 'important': 1},
                                 simplify={'ran': 'agents', 'applied': '3', 'skipped': 1})])
        run = self.json()['runs'][0]
        self.assertEqual(run['final_findings'], {'critical': 0, 'important': 1, 'minor': 0, 'unneeded': 0})
        self.assertEqual(run['simplify'], {'ran': 'agents', 'applied': 0, 'skipped': 1})
        html = self.html()
        self.assertIn('<td>critical=0 important=1 minor=0 unneeded=0</td>', html)
        self.assertIn('<td>ran=agents applied=0 skipped=1</td>', html)
        self.assertNotIn('critical=2', html)
        self.assertNotIn('applied=3', html)

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

    def test_simplify_spend_is_unknown_not_zero(self):
        # simplify's agents are no step kind, so no dispatch feeds its row
        self.records([run_record()])
        board = self.json()['totals']['scoreboard']
        self.assertEqual(board['simplify']['ran'], 1)
        self.assertIsNone(board['simplify']['tokens'])
        self.assertIsNone(board['simplify']['cost'])
        row = re.search(r'<tr><td>simplify</td><td>1</td>.*?</tr>', self.html()).group(0)
        self.assertTrue(row.endswith('<td>–</td><td>–</td></tr>'), row)

    def test_json_into_a_reader_that_stops_early_exits_cleanly(self):
        self.records([run_record(branch=f'b{i}') for i in range(400)])
        p = subprocess.Popen([PYTHON, '-I', SCRIPT, '--projects', self.projects, '--log', self.log, '--now', NOW,
                              '--agents', os.path.join(self.root, 'no agents'), '--json'],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=CLEAN_ENV)
        self.assertEqual(len(p.stdout.read(300)), 300)
        p.stdout.close()
        stderr = p.stderr.read()
        p.stderr.close()
        self.assertEqual(p.wait(timeout=60), 0, stderr)
        self.assertEqual(stderr, b'')


STARTUP = '<karpathy-guidelines-session-start>guidelines</karpathy-guidelines-session-start>'
NOT_FOUND = "Agent type 'final-reviewer' not found. Available agents: general-purpose, Explore"
NO_RAN_KEY = {'applied': 0, 'skipped': 0}  # an old record, from before simplify.ran
RAN_NO = {'ran': 'no', 'applied': 0, 'skipped': 0}
SIMPLIFY_STEP = {'step': 'simplify', 'counts': {'applied': 0, 'skipped': 0}, 'findings': []}
FINAL_STEP = {'step': 'final_review', 'counts': {'critical': 0, 'important': 0, 'minor': 0, 'unneeded': 0},
              'findings': []}


class HealthCheck(Report):
    """--health reads transcripts: rows get increasing timestamps, and each run ends at its record-writing Bash row."""

    def setUp(self):
        super().setUp()
        self.clock = 0

    def ts(self):
        self.clock += 1
        return f'2026-11-05T10:{self.clock // 60:02d}:{self.clock % 60:02d}.000Z'

    def startup(self):
        return startup_row(self.ts())

    def simplify(self):
        return asst('simplify-' + str(self.clock), content=[tool_use('s' + str(self.clock), 'Skill', {'skill': 'simplify'})],
                    ts=self.ts())

    def agent(self, tid, error=None, **inp):
        """An Agent dispatch and its tool_result: an error when error is given, else a launched agent's report."""
        result = {'type': 'tool_result', 'tool_use_id': tid, 'content': error or 'report'}
        if error:
            result['is_error'] = True
        return [asst('m-' + tid, content=[tool_use(tid, 'Agent', dict(inp, prompt='p'))], ts=self.ts()),
                user([result], ts=self.ts())]

    def record_write(self, head, base='0000aaaa', started=None):
        fields = f'"base": "{base}", "head": "{head}"' + (f', "started": "{started}"' if started else '')
        command = f'echo \'{{{fields}}}\' >> "$HOME/.claude/workflow-log.jsonl"'
        return asst('w-' + head, content=[tool_use('w' + head, 'Bash', {'command': command})], ts=self.ts())

    def loaded(self, name):
        """The row a skill load writes."""
        return user([{'type': 'text', 'text': f'Base directory for this skill: /x/skills/{name}\n\n# {name}'}],
                    ts=self.ts(), isMeta=True)

    def health(self, *args):
        """The problem lines of --health. Information lines are checked by the tests about them."""
        return [line for line in self.run_report('--health', *args).splitlines() if line.startswith(('WARN', 'Health'))]

    def reviewed(self, tid, report, handback=None, sub_model=None, sub_effort=None, **inp):
        """A launched dispatch whose subagent transcript ends with report as its last text, then a SubagentHandback
        whose message is handback when one is given. Its rows run on sub_model and sub_effort when given."""
        ran = dict({'model': sub_model} if sub_model else {}, effort=sub_effort)
        rows = [asst('r-' + tid, content=[{'type': 'text', 'text': report}], ts=self.ts(), **ran)]
        if handback is not None:
            rows.append(asst('h-' + tid, content=[tool_use('hb' + tid, 'SubagentHandback', {'message': handback})],
                             ts=self.ts(), **ran))
        self.subagent(inp.get('subagent_type') or 'general-purpose', rows, project='proj', aid=tid, tid=tid)
        return self.agent(tid, **inp)

    def silent_case(self):
        rows = [self.startup(), self.simplify()]
        rows += self.agent('fr', error=NOT_FOUND, subagent_type='final-reviewer', description='Final review')
        for family in 'BCDEF':
            rows += self.agent('gp' + family, subagent_type='general-purpose', model='fable',
                               description=f'Final review of family {family} diff')
        rows.append(self.record_write('c0ffee01'))
        self.session(rows, project='proj')
        self.records([run_record(branch='migration/x', head='c0ffee01', simplify=NO_RAN_KEY)])

    def test_guard_refusals_are_counted_in_hook_decisions(self):
        reason = 'No simplify since the last final review in this session\'s transcript. Run the simplify skill.'
        rows = self.agent('fr', error=reason, subagent_type='final-reviewer', description='Final review')
        rows += self.agent('gp', error='This dispatch has no model and no named agent, so it would run',
                           subagent_type='general-purpose', description='x')
        self.session(rows, project='proj')
        hooks = section(self.run_report(), 'Hook decisions:')
        self.assertIn('  guard-agent-dispatch.py deny x1: no_simplify', hooks)
        self.assertIn('  guard-agent-dispatch.py deny x1: no_model', hooks)
        self.assertEqual(section(self.run_report('--since', '2026-11-06'), 'Hook decisions:'), ['Hook decisions:'])

    def test_the_known_silent_case_is_flagged(self):
        self.silent_case()
        out = self.health()
        prefix = 'WARN: migration/x (2026-11-05): '
        self.assertTrue(all(line.startswith(prefix) for line in out), out)
        unsimplified = [line for line in out if 'final review without simplify' in line]
        self.assertEqual(unsimplified, [prefix + f'final review without simplify: Final review of family {f} diff'
                                        for f in 'CDEF'])
        self.assertEqual(len([line for line in out if 'not found' in line]), 1)
        self.assertEqual(len([line for line in out if 'named role by general-purpose' in line]), 5)
        self.assertEqual(len(out), 10, out)

    def test_two_runs_in_one_session_are_kept_apart(self):
        rows = [self.startup()]
        rows += self.agent('fr1', error=NOT_FOUND, subagent_type='final-reviewer', description='Final review')
        rows.append(self.record_write('aaaa1111'))
        rows += self.agent('fr2', subagent_type='final-reviewer', description='Final review of the second run')
        rows.append(self.record_write('bbbb2222'))
        self.session(rows, project='proj')
        self.records([run_record(branch='one', head='aaaa1111', simplify=NO_RAN_KEY),
                      run_record(branch='two', head='bbbb2222', simplify=NO_RAN_KEY)])
        self.assertEqual(self.health(), [
            "WARN: one (2026-11-05): agent type 'final-reviewer' not found",
            'WARN: two (2026-11-05): final review without simplify: Final review of the second run',
        ])

    def test_a_run_ends_where_its_head_is_written_not_where_it_is_the_next_base(self):
        # run two's session sorts first, and its record names run one's head as its base
        first = [self.startup()] + self.agent('im1', subagent_type='implementer', description='Implement Task 1')
        first += self.agent('fr1', error=NOT_FOUND, subagent_type='final-reviewer', description='Final review')
        first.append(self.record_write('aaaa1111'))
        self.session(first, project='proj', sid='sess-b')
        second = [self.startup()] + self.agent('fr2', subagent_type='final-reviewer',
                                               description='Final review of the second run')
        second.append(self.record_write('bbbb2222', base='aaaa1111'))
        self.session(second, project='proj', sid='sess-a')
        self.records([run_record(branch='one', head='aaaa1111', simplify=NO_RAN_KEY),
                      run_record(branch='two', head='bbbb2222', simplify=NO_RAN_KEY)])
        self.assertEqual(self.health(), [
            "WARN: one (2026-11-05): agent type 'final-reviewer' not found",
            'WARN: two (2026-11-05): final review without simplify: Final review of the second run',
        ])

    def test_next_record_naming_the_head_as_its_base_does_not_take_the_run(self):
        rows = [self.startup()]
        rows += self.agent('fr1', error=NOT_FOUND, subagent_type='final-reviewer', description='Final review')
        rows.append(self.record_write('aaaa1111'))
        rows.append(self.record_write('bbbb2222', base='aaaa1111'))
        self.session(rows, project='proj')
        self.records([run_record(branch='one', head='aaaa1111', simplify=NO_RAN_KEY),
                      run_record(branch='two', head='bbbb2222', base='aaaa1111', simplify=NO_RAN_KEY)])
        self.assertEqual(self.health(), ["WARN: one (2026-11-05): agent type 'final-reviewer' not found"])

    def two_runs_with_an_error_in_the_first(self, first_date, second_date):
        rows = [self.startup()]
        rows += self.agent('fr1', error=NOT_FOUND, subagent_type='final-reviewer', description='Final review')
        rows.append(self.record_write('aaaa1111'))
        rows.append(self.record_write('bbbb2222', base='aaaa1111'))
        self.session(rows, project='proj')
        self.records([run_record(branch='one', head='aaaa1111', date=first_date, simplify=NO_RAN_KEY),
                      run_record(branch='two', head='bbbb2222', base='aaaa1111', date=second_date, simplify=NO_RAN_KEY)])

    def test_a_previous_run_before_since_still_ends_at_its_record_write(self):
        self.two_runs_with_an_error_in_the_first('2026-11-06T12:00:00Z', '2026-11-07T10:00:00Z')
        self.assertEqual(self.health('--since', '2026-11-07'), ['Health: no problems'])

    def test_a_previous_run_last_week_still_ends_at_its_record_write(self):
        self.two_runs_with_an_error_in_the_first('2026-10-30T12:00:00Z', '2026-11-05T12:00:00Z')
        self.assertEqual(self.run_report('--brief').splitlines()[2], 'Consultations with no note: 0')

    def probe_then_run(self, started=None):
        """A not-found probe at 10:00:03Z, then from 10:00:31Z a simplify, a final review and the record write."""
        rows = [self.startup()]
        rows += self.agent('probe', error=NOT_FOUND, subagent_type='final-reviewer', description='Final review')
        self.clock = 30
        rows += [self.simplify()] + self.agent('fr', subagent_type='final-reviewer', description='Final review')
        rows.append(self.record_write('c0ffee01', started=started))
        self.session(rows, project='proj')

    def test_a_schema_2_run_starts_at_started_not_at_the_session_start(self):
        started = '2026-11-05T11:00:30+01:00'  # 10:00:30Z, after the probe's rows
        self.probe_then_run(started)
        final = FINAL_STEP
        simplify = SIMPLIFY_STEP
        self.records([run_record(head='c0ffee01', path='small', schema=2, started=started, steps=[simplify, final])])
        self.assertEqual(self.health(), ['Health: no problems'])

    def test_a_schema_1_run_still_starts_at_the_session_start(self):
        # The same rows without "started": the probe is inside the run, as today.
        self.probe_then_run()
        self.records([run_record(head='c0ffee01')])
        self.assertTrue(any('not found' in line for line in self.health()))

    def test_a_plan_record_and_a_small_run_in_one_session_keep_their_own_rows(self):
        plan_started, small_started = '2026-11-05T10:00:01Z', '2026-11-05T10:00:40Z'
        rows = [self.startup(), self.loaded('brainstorming'), self.loaded('writing-plans')]
        self.clock = 19
        rows.append(self.record_write('', started=plan_started))  # 10:00:20Z
        # A simplify between the two windows belongs to neither run.
        self.clock = 24
        rows.append(self.simplify())
        self.clock = 40
        rows += self.agent('fr', subagent_type='final-reviewer', description='Final review')
        rows.append(self.record_write('sma11001', started=small_started))
        self.session(rows, project='proj')
        plan = plan_record(branch='plan-rec', started=plan_started)
        final = FINAL_STEP
        small = run_record(branch='small-run', head='sma11001', path='small', schema=2, started=small_started,
                           simplify=NO_RAN_KEY, steps=[final])
        self.records([plan, small])
        self.assertEqual(self.health(), ['WARN: small-run (2026-11-05): final review without simplify: Final review'])

    def test_clean_run_has_no_problems(self):
        rows = [self.startup(), self.simplify()]
        rows += self.agent('fr', subagent_type='final-reviewer', description='Final review')
        rows.append(self.record_write('c0ffee01'))
        self.session(rows, project='proj')
        self.records([run_record(head='c0ffee01')])
        self.assertEqual(self.health(), ['Health: no problems'])

    def test_a_command_reading_the_log_and_naming_the_head_does_not_start_the_run(self):
        rows = [self.startup(), self.simplify()]
        rows += self.agent('fr', subagent_type='final-reviewer', description='Final review')
        command = 'echo "fix wave c0ffee01" >> notes.md; tail -1 "$HOME/.claude/workflow-log.jsonl"'
        rows.append(asst('read', content=[tool_use('read', 'Bash', {'command': command})], ts=self.ts()))
        rows.append(self.record_write('c0ffee01'))
        self.session(rows, project='proj')
        self.records([run_record(head='c0ffee01')])
        self.assertEqual(self.health(), ['Health: no problems'])

    def test_not_found_text_in_another_tools_error_is_not_flagged(self):
        rows = [self.startup(), self.simplify()]
        rows.append(asst('grep', content=[tool_use('grep', 'Bash', {'command': 'grep -n "not found" plan.md'})],
                         ts=self.ts()))
        rows.append(user([{'type': 'tool_result', 'tool_use_id': 'grep', 'is_error': True, 'content': NOT_FOUND}],
                         ts=self.ts()))
        rows += self.agent('fr', subagent_type='final-reviewer', description='Final review')
        rows.append(self.record_write('c0ffee01'))
        self.session(rows, project='proj')
        self.records([run_record(head='c0ffee01')])
        self.assertEqual(self.health(), ['Health: no problems'])

    def test_record_says_ran_but_transcript_has_no_simplify(self):
        self.session([self.startup(), self.record_write('c0ffee01')], project='proj')
        self.records([run_record(head='c0ffee01', simplify={'ran': 'inline', 'applied': 1, 'skipped': 0})])
        self.assertEqual(self.health(), [
            'WARN: b (2026-11-05): record says simplify ran (inline) but the transcript has no simplify call'])

    def test_record_says_no_but_transcript_has_simplify(self):
        self.session([self.startup(), self.simplify(), self.record_write('c0ffee01')], project='proj')
        self.records([run_record(head='c0ffee01', simplify=RAN_NO)])
        self.assertEqual(self.health(), [
            'WARN: simplify did not run on b (2026-11-05)',
            'WARN: b (2026-11-05): record says simplify did not run but the transcript has a simplify call'])

    def test_ran_no_is_warned_once(self):
        rows = [self.startup()]
        rows += self.agent('fr', subagent_type='final-reviewer', description='Final review')
        rows.append(self.record_write('c0ffee01'))
        self.session(rows, project='proj')
        self.records([run_record(head='c0ffee01', simplify=RAN_NO)])
        self.assertEqual(self.health(), ['WARN: simplify did not run on b (2026-11-05)'])
        self.assertEqual(self.run_report('--brief').splitlines()[2], 'WARN: simplify did not run on b (2026-11-05)')

    def test_missing_startup_guidelines_is_flagged(self):
        self.session([self.simplify(), self.record_write('c0ffee01')], project='proj')
        self.records([run_record(head='c0ffee01')])
        self.assertEqual(self.health(), ['WARN: b (2026-11-05): startup guidelines missing from the session'])

    def test_modelless_general_purpose_dispatch_that_launched_is_flagged(self):
        rows = [self.startup()]
        rows += self.agent('gp', subagent_type='general-purpose', description='Explore the code')
        rows += self.agent('untyped', error='denied by hook', description='Look around')
        rows.append(self.record_write('c0ffee01'))
        self.session(rows, project='proj')
        self.records([run_record(head='c0ffee01', simplify=NO_RAN_KEY)])
        self.assertEqual(self.health(), [
            'WARN: b (2026-11-05): model-less general-purpose dispatch launched: Explore the code'])

    def test_sdd_template_descriptions_count_as_named_roles(self):
        launched = ['Implement Task 2: x', 'Review Task 2 (spec + quality)', 'Re-review Task 2 fix round 1',
                    'Spec review Task 1', 'Code quality review Task 1']
        main = [self.startup()]
        for i, description in enumerate(launched[:3]):
            main += self.agent(f'gp{i}', subagent_type='general-purpose', model='opus', description=description)
        main += self.agent('denied', error='denied by hook', subagent_type='general-purpose', model='opus',
                           description='Implement Task 3: y')
        # one from a subagent and one from a workflow subagent, both inside the run's span
        self.subagent('implementer', self.agent('gp3', subagent_type='general-purpose', model='opus',
                                                description=launched[3]), project='proj')
        self.write_lines(os.path.join(self.projects, 'proj', 'sess-1', 'subagents', 'workflows', 'w1', 'agent-w.jsonl'),
                         self.agent('gp4', subagent_type='general-purpose', model='opus', description=launched[4]))
        main.append(self.record_write('c0ffee01'))
        self.session(main, project='proj')
        self.records([run_record(head='c0ffee01', simplify=NO_RAN_KEY)])
        self.assertEqual(sorted(self.health()),
                         sorted(f'WARN: b (2026-11-05): named role by general-purpose: {d}' for d in launched))

    def test_run_with_no_record_write_warns(self):
        self.records([run_record(head='c0ffee01')])
        self.assertEqual(self.health(), ['WARN: b (2026-11-05): no session transcript found'])

    def test_brief_shows_first_warning_and_count(self):
        self.silent_case()
        # last week's run shows in --health but is not counted in this week's brief
        self.records([run_record(branch='old', head='dead0001', date='2026-10-30T12:00:00Z')])
        health = self.health()
        self.assertEqual(len(health), 11)
        self.assertIn('WARN: old (2026-10-30): no session transcript found', health)
        brief = self.run_report('--brief').splitlines()
        self.assertEqual(len(brief), 3)
        self.assertEqual(brief[2], health[0] + ' (+9 more, run --health)')


    def test_review_without_findings_line_is_flagged(self):
        rows = [self.startup(), self.simplify()]
        rows += self.reviewed('tr1', 'Looks fine. The Findings: line comes later.', subagent_type='task-reviewer',
                              description='Review Task 1')
        rows += self.reviewed('tr2', 'Done.\n`Findings: critical=0 important=0 minor=1`', subagent_type='task-reviewer',
                              description='Review Task 2')
        rows += self.reviewed('gp', 'All good.', subagent_type='general-purpose', model='opus',
                              description='Re-review Task 2 fix round 1')
        # not reviews: their reports need no Findings: line
        rows += self.reviewed('im', 'Implemented.', subagent_type='implementer', description='Implement Task 3')
        rows += self.reviewed('fc', 'Finding 1: fixed.', subagent_type='fix-checker', description='Fix-check Task 2')
        rows += self.reviewed('fr', 'Findings: critical=0 important=0 minor=0 unneeded=0', subagent_type='final-reviewer',
                              description='Final review')
        rows.append(self.record_write('c0ffee01'))
        self.session(rows, project='proj')
        self.records([run_record(head='c0ffee01')])
        self.assertEqual(self.health(), [
            'WARN: b (2026-11-05): review report has no Findings: line: Review Task 1',
            'WARN: b (2026-11-05): review report has no Findings: line: Re-review Task 2 fix round 1',
            'WARN: b (2026-11-05): named role by general-purpose: Re-review Task 2 fix round 1',
        ])

    def test_findings_line_is_read_from_the_handback_message(self):
        rows = [self.startup(), self.simplify()]
        rows += self.reviewed('fr', 'Findings: critical=0 important=0 minor=0 unneeded=0',
                              handback='Report.\nFindings: critical=0 important=1 minor=2 unneeded=0',
                              subagent_type='final-reviewer', description='Final review')
        rows.append(self.record_write('c0ffee01'))
        self.session(rows, project='proj')
        self.records([run_record(head='c0ffee01')])
        self.assertEqual(self.health(), [
            "WARN: b (2026-11-05): record's final_findings critical=0 important=0 minor=0 unneeded=0 differ from the "
            "final review's critical=0 important=1 minor=2 unneeded=0"])

    def test_record_findings_must_match_the_final_reviewers_line(self):
        rows = [self.startup(), self.simplify()]
        rows += self.reviewed('fr1', 'Findings: critical=0 important=2 minor=0 unneeded=0', subagent_type='final-reviewer',
                              description='Final review')
        rows.append(self.simplify())
        rows += self.reviewed('fr2', 'Findings: critical=0 important=0 minor=1 unneeded=1', subagent_type='final-reviewer',
                              description='Final review after the fix wave')
        rows += self.reviewed('fc', 'Finding 1: fixed.\nFindings: critical=0 important=5 minor=0 unneeded=0',
                              subagent_type='general-purpose', model='opus', description='Fix-check final review findings')
        rows.append(self.record_write('c0ffee01'))
        # the second run's record matches its final review
        rows.append(self.simplify())
        rows += self.reviewed('fr3', 'Findings: critical=1 important=0 minor=0 unneeded=0', subagent_type='final-reviewer',
                              description='Final review')
        rows.append(self.record_write('c0ffee02'))
        self.session(rows, project='proj')
        self.records([run_record(head='c0ffee01', final_findings={'critical': 0, 'important': 2, 'minor': 0, 'unneeded': 0}),
                      run_record(branch='two', head='c0ffee02',
                                 final_findings={'critical': 1, 'important': 0, 'minor': 0, 'unneeded': 0})])
        self.assertEqual(self.health(), [
            "WARN: b (2026-11-05): record's final_findings critical=0 important=2 minor=0 unneeded=0 differ from the "
            "final review's critical=0 important=0 minor=1 unneeded=1",
            'WARN: b (2026-11-05): named role by general-purpose: Fix-check final review findings'])

    def test_not_proportionate_verdicts_are_counted_not_flagged(self):
        rows = [self.startup(), self.simplify()]
        rows += self.reviewed('fc1', 'Finding 1: fixed, not proportionate: a parallel function.\nFinding 2: fixed.',
                              subagent_type='fix-checker', description='Fix-check Task 1')
        rows += self.reviewed('fc2', 'Finding 1: Fixed, not proportionate, a test-only parameter.',
                              subagent_type='fix-checker', description='Fix-check Task 1 round 2')
        rows += self.reviewed('tr', 'fixed, not proportionate\nFindings: critical=0 important=0 minor=0',
                              subagent_type='task-reviewer', description='Review Task 1')
        rows += self.reviewed('fr', 'Findings: critical=0 important=0 minor=0 unneeded=0', subagent_type='final-reviewer',
                              description='Final review')
        rows.append(self.record_write('c0ffee01'))
        self.session(rows, project='proj')
        self.records([run_record(head='c0ffee01', fix_growth=None)])
        self.assertEqual(self.run_report('--health').splitlines(),
                         ['Health: no problems', 'b (2026-11-05): fixed, not proportionate: 2'])
        self.assertEqual(self.run_report('--brief').splitlines()[2], 'Consultations with no note: 0')

    def run_on_repo(self, repo, runs):
        """One session holding the runs, each a (record, final review report): simplify, the final review, then the
        record write."""
        rows = [self.startup()]
        for i, (rec, report) in enumerate(runs):
            rows.append(self.simplify())
            rows += self.reviewed(f'fr{i}', report, subagent_type='final-reviewer', description='Final review')
            rows.append(self.record_write(rec['head'], base=rec['base']))
        self.session(rows, project='proj')
        self.records([dict(rec, project=repo) for rec, _ in runs])

    def test_conventions_counted_and_flagged_only_after_an_open_finding(self):
        repo, change = self.repo()
        # the heading sits more than git's 3 lines of context above the added entries
        base = change('CLAUDE.md', 'base', text='# Project\n\n## Code conventions\n\n- rule 1\n- rule 2\n- rule 3\n- rule 4\n')
        added = change('CLAUDE.md', 'feat: record two conventions', text='- new rule\n\n- second new rule\n')
        elsewhere = change('CLAUDE.md', 'feat: a note elsewhere', text='\n## Notes\n\n- a note\n')
        none = change('code.txt', 'feat: code only')
        open_finding = ('### Important\n\n1. The new helper is a convention: record it under `## Code conventions`.\n\n'
                        'Findings: critical=0 important=1 minor=0 unneeded=0')
        minor_only = ('### Important\n\n1. A bug in code.txt.\n\n### Minor\n\n1. Maybe note it under '
                      '`## Code conventions`.\n\nFindings: critical=0 important=1 minor=1 unneeded=0')
        found = {'critical': 0, 'important': 1, 'minor': 0, 'unneeded': 0}
        self.run_on_repo(repo, [
            (run_record(branch='added', base=base, head=added, final_findings=found), open_finding),
            (run_record(branch='elsewhere', base=added, head=elsewhere, final_findings=found), open_finding),
            (run_record(branch='minor', base=elsewhere, head=none, final_findings=dict(found, minor=1)), minor_only),
        ])
        self.assertEqual(self.health(), [
            'WARN: elsewhere (2026-11-05): final review asked for a ## Code conventions entry but the branch added none'])

    def test_fix_growth_mismatch_is_flagged(self):
        repo, change = self.repo()

        def lines(n):
            return ''.join(f'line {i}\n' for i in range(n))

        base = change('a.txt', 'base')
        change('a.txt', 'feat: task one', text=lines(10))
        change('a.txt', 'fix: something else', text=lines(5))
        first = change('a.txt', 'fix: address review findings: round 1', text=lines(40))
        change('a.txt', 'fix: address review findings: round 1', text=lines(100))
        second = change('a.txt', 'feat: task two', text=lines(3))
        third = change('a.txt', 'feat: task three', text=lines(7))
        growth = {'task_added': 15, 'task_removed': 0, 'fix_added': 10, 'fix_removed': 0, 'unplanned_files': []}
        report = 'Findings: critical=0 important=0 minor=0 unneeded=0'
        self.run_on_repo(repo, [
            # git says fix_added=40: 10 is off by 30 lines and 75%
            (run_record(branch='off', base=base, head=first, fix_growth=growth), report),
            # git says 100: 70 is off by 30 lines but only 30%
            (run_record(branch='near', base=first, head=second, fix_growth=dict(growth, fix_added=70)), report),
            # git says 0: 15 is off by 100% but only 15 lines
            (run_record(branch='small', base=second, head=third, fix_growth=dict(growth, fix_added=15)), report),
        ])
        self.assertEqual(self.health(), ["WARN: off (2026-11-05): record's fix_added 10 differs from git's 40"])

    def test_unusable_git_is_skipped_without_warn(self):
        # the default project /p is not there; the second project is a folder but not a repo
        self.session([self.startup(), self.simplify(), self.record_write('y'), self.simplify(),
                      self.record_write('z', base='y')], project='proj')
        self.records([run_record(), run_record(branch='two', project=self.root, base='y', head='z')])
        self.assertEqual(self.run_report('--health').splitlines(),
                         ['Health: no problems', 'Git evidence unavailable for 2 runs'])
        self.assertEqual(self.run_report('--brief').splitlines()[2], 'Consultations with no note: 0')

    def test_session_with_workflow_agents_but_no_record_is_flagged(self):
        rows = [self.startup(), self.simplify()]
        rows += self.agent('fr', subagent_type='final-reviewer', description='Final review')
        rows.append(self.record_write('c0ffee01'))
        rows += self.agent('im', subagent_type='implementer', description='Implement Task 1')
        self.session(rows, project='proj', sid='sess-1')
        self.session([self.startup()] + self.agent('gp', subagent_type='general-purpose', model='opus',
                                                   description='Implement Task 2: x'), project='proj', sid='sess-2')
        # none of these: a dispatch that names no workflow role, a denied one, and one last week
        quiet = [self.startup()] + self.agent('ex', subagent_type='general-purpose', model='opus',
                                              description='Explore the code')
        quiet += self.agent('den', error='denied by hook', subagent_type='implementer', description='Implement Task 3')
        quiet += [asst('m-old', content=[tool_use('old', 'Agent', {'subagent_type': 'implementer', 'prompt': 'p'})],
                       ts='2026-10-30T10:00:00.000Z'),
                  user([{'type': 'tool_result', 'tool_use_id': 'old', 'content': 'report'}], ts='2026-10-30T10:00:01.000Z')]
        self.session(quiet, project='proj', sid='sess-3')
        self.records([run_record(head='c0ffee01')])
        self.assertEqual(self.health(), [
            'WARN: session sess-1.jsonl ran workflow agents but wrote no run record',
            'WARN: session sess-2.jsonl ran workflow agents but wrote no run record'])
        self.assertEqual(self.run_report('--brief').splitlines()[2],
                         'WARN: session sess-1.jsonl ran workflow agents but wrote no run record (+1 more, run --health)')

    def test_planning_session_without_record_is_not_flagged(self):
        # a planning session dispatches a plan reviewer and writes no run record, by design
        rows = [self.startup()] + self.agent('ps', subagent_type='plan-simplifier', description='Plan simplify')
        rows += self.agent('pr', subagent_type='general-purpose', model='opus', description='Plan review of the plan')
        self.session(rows, project='proj', sid='sess-1')
        self.assertEqual(self.health(), ['Health: no problems'])

    def test_plan_review_without_plan_simplifier_is_flagged(self):
        self.session([self.startup()] + self.agent('pr', subagent_type='plan-reviewer', description='Plan review'),
                     project='proj')
        self.assertEqual(self.health(), ['WARN: session sess-1.jsonl: plan review without plan-simplifier: Plan review'])

    def test_a_check_that_mentions_plan_simplifier_does_not_count(self):
        rows = [self.startup()] + self.agent('ck', subagent_type='general-purpose', model='opus',
                                             description='Builder check on plan-simplifier spec')
        rows += self.agent('pr', subagent_type='plan-reviewer', description='Plan review')
        self.session(rows, project='proj')
        self.assertEqual(self.health(), ['WARN: session sess-1.jsonl: plan review without plan-simplifier: Plan review'])

    def test_plan_simplifier_before_plan_review_is_clean(self):
        rows = [self.startup()] + self.agent('ps', subagent_type='plan-simplifier', description='Plan simplify')
        rows += self.agent('pr', subagent_type='plan-reviewer', description='Plan review')
        self.session(rows, project='proj')
        self.assertEqual(self.health(), ['Health: no problems'])

    def test_each_plan_review_needs_its_own_plan_simplifier(self):
        rows = [self.startup()] + self.agent('ps', subagent_type='plan-simplifier', description='Plan simplify')
        rows += self.agent('pr1', subagent_type='plan-reviewer', description='Plan review')
        rows += self.agent('pr2', subagent_type='plan-reviewer', description='Plan re-review')
        self.session(rows, project='proj')
        self.assertEqual(self.health(),
                         ['WARN: session sess-1.jsonl: plan review without plan-simplifier: Plan re-review'])

    def test_a_denied_plan_review_is_not_flagged(self):
        self.session([self.startup()] + self.agent('pr', error='denied by hook', subagent_type='plan-reviewer',
                                                   description='Plan review'), project='proj')
        self.assertEqual(self.health(), ['Health: no problems'])

    def test_plan_review_last_week_is_not_flagged(self):
        self.session([asst('m-old', content=[tool_use('old', 'Agent', {'subagent_type': 'plan-reviewer',
                                                                       'description': 'Plan review', 'prompt': 'p'})],
                           ts='2026-10-30T10:00:00.000Z'),
                      user([{'type': 'tool_result', 'tool_use_id': 'old', 'content': 'report'}],
                           ts='2026-10-30T10:00:01.000Z')], project='proj')
        old = datetime.datetime(2026, 10, 30, 10, tzinfo=datetime.timezone.utc).timestamp()
        os.utime(os.path.join(self.projects, 'proj', 'sess-1.jsonl'), (old, old))
        self.assertEqual(self.health(), ['Health: no problems'])

    def test_a_window_ends_at_now(self):
        # NOW is 2026-11-07T12:00Z: the probe at 12:30Z is after it, so outside the run's window
        started = '2026-11-05T10:00:00Z'
        rows = [self.startup(), self.simplify()] + self.agent('fr', subagent_type='final-reviewer',
                                                             description='Final review')
        late = self.agent('probe', error=NOT_FOUND, subagent_type='final-reviewer', description='Final review')
        late.append(self.record_write('c0ffee01', started=started))
        for row in late:
            row['timestamp'] = '2026-11-07T12:30:00.000Z'
        self.session(rows + late, project='proj')
        final = FINAL_STEP
        simplify = SIMPLIFY_STEP
        self.records([run_record(head='c0ffee01', path='small', schema=2, started=started, date='2026-11-07T11:00:00Z',
                                 steps=[simplify, final])])
        self.assertEqual(self.health(), ['Health: no problems'])

    def test_dispatches_after_now_are_not_flagged(self):
        # NOW is 2026-11-07T12:00Z: an implementer and a plan review on the 8th are ignored
        late = [self.startup(),
                asst('m-im', content=[tool_use('im', 'Agent', {'subagent_type': 'implementer', 'prompt': 'p'})],
                     ts='2026-11-08T10:00:00.000Z'),
                user([{'type': 'tool_result', 'tool_use_id': 'im', 'content': 'report'}], ts='2026-11-08T10:00:01.000Z'),
                asst('m-pr', content=[tool_use('pr', 'Agent', {'subagent_type': 'plan-reviewer',
                                                               'description': 'Plan review', 'prompt': 'p'})],
                     ts='2026-11-08T10:00:02.000Z'),
                user([{'type': 'tool_result', 'tool_use_id': 'pr', 'content': 'report'}], ts='2026-11-08T10:00:03.000Z')]
        self.session(late, project='proj')
        self.assertEqual(self.health(), ['Health: no problems'])

    def test_a_plan_simplifier_that_was_not_found_does_not_count(self):
        rows = [self.startup()] + self.agent('ps', error="Agent type 'plan-simplifier' not found",
                                             subagent_type='plan-simplifier', description='Plan simplify')
        rows += self.agent('pr', subagent_type='plan-reviewer', description='Plan review')
        self.session(rows, project='proj')
        self.assertEqual(self.health(), ['WARN: session sess-1.jsonl: plan review without plan-simplifier: Plan review'])

    def pins(self, skills=False):
        """A fixture agents folder: final-reviewer pinned fable/high and fix-checker sonnet/medium, listing their
        skills only when skills is true."""
        folder = os.path.join(self.root, 'agents')
        os.makedirs(folder, exist_ok=True)
        for name, model, effort, listed in (('final-reviewer', 'fable', 'high', ['andrej-karpathy-skills:karpathy-guidelines']),
                                            ('fix-checker', 'sonnet', 'medium', [])):
            lines = ['---', f'name: {name}', 'description: d', f'model: {model}', f'effort: {effort}']
            if skills and listed:
                lines += ['skills:'] + [f'  - {skill}' for skill in listed]
            with open(os.path.join(folder, name + '.md'), 'w') as f:
                f.write('\n'.join(lines + ['---', 'Body.']) + '\n')
        return folder

    def schema2_run(self, steps, dispatch_rows, loads=('subagent-driven-development',), **over):
        rows = [self.startup()] + [self.loaded(name) for name in loads] + [self.simplify()] + dispatch_rows
        rows.append(self.record_write('c0ffee01', started='2026-11-05T10:00:00Z'))
        self.session(rows, project='proj')
        self.records([run_record(head='c0ffee01', schema=2, started='2026-11-05T10:00:00Z', steps=steps, **over)])

    def test_steps_match_launched_agents_and_count_lines(self):
        final = {'step': 'final_review', 'counts': {'critical': 0, 'important': 1, 'minor': 0, 'unneeded': 0},
                 'findings': [{'id': 'F1', 'severity': 'important', 'summary': 's', 'outcome': 'fixed',
                               'verdicts': ['fixed']}]}
        fix = {'step': 'final_fix_check', 'counts': {'fixed': 1, 'not_proportionate': 0, 'open': 0}}
        simplify = SIMPLIFY_STEP
        self.schema2_run([simplify, final, fix],
                         self.reviewed('fr', 'Findings: critical=0 important=1 minor=0 unneeded=0',
                                       sub_model='claude-fable-5-1', sub_effort='high',
                                       subagent_type='final-reviewer', description='Final review')
                         + self.reviewed('fc', 'Verdicts: fixed=1 not_proportionate=0 open=0',
                                         sub_model='claude-sonnet-5-5', sub_effort='medium',
                                         subagent_type='fix-checker', description='Fix-check final findings'),
                         final_findings={'critical': 0, 'important': 1, 'minor': 0, 'unneeded': 0})
        self.assertEqual(self.health('--agents', self.pins()), ['Health: no problems'])

    def test_a_missing_step_a_count_mismatch_and_a_missing_line_are_warned(self):
        simplify = SIMPLIFY_STEP
        plan = {'step': 'plan_simplify', 'counts': {'cuts': 0, 'flags': 0}, 'findings': []}
        task = {'step': 'task_review', 'task': 1, 'round': 0, 'counts': {'critical': 0, 'important': 1, 'minor': 0},
                'findings': [{'id': 'F1', 'severity': 'important', 'summary': 's', 'outcome': 'fixed',
                              'verdicts': ['fixed']}]}
        fix = {'step': 'fix_check', 'task': 1, 'round': 1, 'counts': {'fixed': 1, 'not_proportionate': 0, 'open': 0}}
        rows = self.reviewed('ps', 'Nothing to cut.', subagent_type='plan-simplifier', description='Plan simplify')
        rows += self.reviewed('tr', 'Findings: critical=0 important=0 minor=0', subagent_type='task-reviewer',
                              description='Review Task 1')
        rows += self.reviewed('fc', 'Finding 1: fixed.', subagent_type='fix-checker', description='Fix-check Task 1')
        # no final_review step for this launched final review
        rows += self.reviewed('fr', 'Findings: critical=0 important=0 minor=0 unneeded=0',
                              subagent_type='final-reviewer', description='Final review')
        self.schema2_run([simplify, plan, task, fix], rows)
        prefix = 'WARN: b (2026-11-05): '
        self.assertEqual(self.health(), [prefix + line for line in (
            'plan simplify report has no Findings: line: Plan simplify',
            'fix-check report has no Verdicts: line: Fix-check Task 1',
            'record has 0 final_review steps, transcript launched 1',
            "task_review step 1 counts critical=0 important=1 minor=0 differ from its report's "
            'critical=0 important=0 minor=0',
        )])

    def test_a_malformed_step_for_a_launched_agent_shows_as_a_step_count_warn(self):
        # the fix-check step isn't a dict, the final review's counts are strings, simplify's findings isn't a list
        simplify = {'step': 'simplify', 'counts': {'applied': 2, 'skipped': 0}, 'findings': 'not a list'}
        final = {'step': 'final_review', 'counts': {'critical': '0', 'important': '1', 'minor': '0', 'unneeded': '0'},
                 'findings': []}
        rows = self.reviewed('fr', 'Findings: critical=0 important=1 minor=0 unneeded=0',
                             subagent_type='final-reviewer', description='Final review')
        rows += self.reviewed('fc', 'Verdicts: fixed=1 not_proportionate=0 open=0', subagent_type='fix-checker',
                              description='Fix-check final findings')
        self.schema2_run([simplify, final, 'final_fix_check'], rows,
                         final_findings={'critical': 0, 'important': 1, 'minor': 0, 'unneeded': 0})
        prefix = 'WARN: b (2026-11-05): '
        self.assertEqual(self.health(), [prefix + line for line in (
            'record has 0 final_fix_check steps, transcript launched 1',
            "final_review step 1 counts critical=0 important=0 minor=0 unneeded=0 differ from its report's "
            'critical=0 important=1 minor=0 unneeded=0',
            "simplify step 1 findings don't add up to its counts",
        )])
        self.assertIn('path=full', self.run_report())

    def test_a_schema_1_fix_check_without_verdicts_is_not_warned(self):
        rows = [self.startup(), self.simplify()]
        rows += self.reviewed('ps', 'Nothing to cut.', subagent_type='plan-simplifier', description='Plan simplify')
        rows += self.reviewed('fc', 'Finding 1: fixed.', subagent_type='fix-checker', description='Fix-check Task 1')
        rows.append(self.record_write('c0ffee01'))
        self.session(rows, project='proj')
        self.records([run_record(head='c0ffee01')])
        self.assertEqual(self.health(), ['Health: no problems'])

    def test_findings_that_do_not_add_up_are_warned(self):
        simplify = {'step': 'simplify', 'counts': {'applied': 1, 'skipped': 0},
                    'findings': [{'id': 'S1', 'summary': 's', 'outcome': 'declined', 'reason': 'r'}]}
        plan = {'step': 'plan_simplify', 'counts': {'cuts': 1, 'flags': 1},
                'findings': [{'id': 'C1', 'kind': 'cut', 'summary': 's', 'outcome': 'applied'},
                             {'id': 'C2', 'kind': 'cut', 'summary': 's', 'outcome': 'applied'}]}
        review = {'step': 'plan_review', 'counts': {'findings': 2},
                  'findings': [{'id': 'P1', 'severity': 'important', 'summary': 's', 'outcome': 'applied'}]}
        task = {'step': 'task_review', 'task': 1, 'round': 0, 'counts': {'critical': 0, 'important': 1, 'minor': 1},
                'findings': [{'id': 'F1', 'severity': 'important', 'summary': 's', 'outcome': 'fixed',
                              'verdicts': ['open', 'fixed']},
                             {'id': 'F2', 'severity': 'critical', 'summary': 's', 'outcome': 'fixed'}]}
        fix = {'step': 'fix_check', 'task': 1, 'round': 1, 'counts': {'fixed': 1, 'not_proportionate': 0, 'open': 0}}
        rows = self.reviewed('ps', 'Findings: cuts=1 flags=1', subagent_type='plan-simplifier', description='Plan simplify')
        rows += self.reviewed('pr', 'Findings: findings=2', subagent_type='plan-reviewer', description='Plan review')
        rows += self.reviewed('tr', 'Findings: critical=0 important=1 minor=1', subagent_type='task-reviewer',
                              description='Review Task 1')
        rows += self.reviewed('fc', 'Verdicts: fixed=1 not_proportionate=0 open=0', subagent_type='fix-checker',
                              description='Fix-check Task 1')
        self.schema2_run([simplify, plan, review, task, fix], rows)
        prefix = 'WARN: b (2026-11-05): '
        self.assertEqual(self.health(), [prefix + line for line in (
            "plan_simplify step 1 findings don't add up to its counts",
            "plan_review step 1 findings don't add up to its counts",
            "task_review step 1 findings don't add up to its counts",
            "simplify step 1 findings don't add up to its counts",
            "fix-check verdicts in findings don't add up to the fix-check steps' counts",
        )])

    def test_an_agent_off_its_pin_is_warned_unless_the_stage_allows_it(self):
        prefix = 'WARN: b (2026-11-05): '
        cases = (
            # (record fields, dispatch input, subagent model and effort, expected)
            ({}, {}, ('claude-opus-5-5', 'high'),
             [prefix + 'final-reviewer ran on claude-opus-5-5/high, pinned fable/high: Final review']),
            ({'stage': 'out'}, {}, ('claude-opus-5-5', 'xhigh'), ['Health: no problems']),
            ({'stage': '3'}, {'model': 'opus'}, ('claude-opus-5-5', 'high'), ['Health: no problems']),
            ({'risk_area': True}, {'effort': 'xhigh'}, ('claude-fable-5-1', 'xhigh'), ['Health: no problems']),
            ({'risk_area': True}, {}, ('claude-fable-5-1', 'xhigh'),
             [prefix + 'final-reviewer ran on claude-fable-5-1/xhigh, pinned fable/high: Final review']),
        )
        for over, inp, (model, effort), expected in cases:
            with self.subTest(over=over, inp=inp):
                self.tearDown()
                self.setUp()
                simplify = SIMPLIFY_STEP
                final = FINAL_STEP
                self.schema2_run([simplify, final],
                                 self.reviewed('fr', 'Findings: critical=0 important=0 minor=0 unneeded=0',
                                               sub_model=model, sub_effort=effort, subagent_type='final-reviewer',
                                               description='Final review', **inp), **over)
                self.assertEqual(self.health('--agents', self.pins()), expected)

    def test_a_risk_area_small_run_is_warned(self):
        self.final_run(path='small', risk_area=True)
        self.assertEqual(self.health(), ['WARN: b (2026-11-05): risk area on the Small path'])

    def wave_run(self, waves, with_check=True):
        """A clean Full run reviewing tasks 3 and 4 and, unless with_check is false, checking wave A, recording waves."""
        clean = {'critical': 0, 'important': 0, 'minor': 0}
        reviews = [('t3', 3, 'Review Task 3'), ('t4', 4, 'Review Task 4')]
        steps = [{'step': 'task_review', 'task': n, 'round': 0, 'counts': clean, 'findings': []} for _, n, _ in reviews]
        if with_check:
            reviews.append(('wa', None, 'Wave A check'))
            steps.append({'step': 'task_review', 'wave': 'A', 'round': 0, 'counts': clean, 'findings': []})
        rows = []
        for tid, _, description in reviews:
            rows += self.reviewed(tid, 'Findings: critical=0 important=0 minor=0', subagent_type='task-reviewer',
                                  description=description)
        self.final_run(rows, loads=('subagent-driven-development',), steps=steps, waves=waves)

    def test_a_wave_with_its_check_is_clean(self):
        self.wave_run([{'wave': 'A', 'tasks': [3, 4], 'conflicts': 0}])
        self.assertEqual(self.health(), ['Health: no problems'])

    def test_a_wave_without_its_check_is_warned(self):
        self.wave_run([{'wave': 'A', 'tasks': [3, 4], 'conflicts': 0}], with_check=False)
        self.assertEqual(self.health(), ['WARN: b (2026-11-05): wave A has no wave check step'])

    def test_malformed_waves_entries_are_dropped_or_read_as_absent(self):
        self.wave_run(['not a dict', {'tasks': [3]}, {'wave': 'B', 'tasks': 'x', 'conflicts': 'y'}], with_check=False)
        self.assertEqual(self.health(), ['WARN: b (2026-11-05): wave B has no wave check step'])

    def test_archive_missing_or_started_differing_is_an_information_line(self):
        cases = (
            (None, ['b (2026-11-05): archive missing']),
            ('Ledger.\nStarted: 2026-11-05T11:00:00+01:00\nStarted: 2026-11-05T09:00:00Z\n', []),
            ('Started: 2026-11-05T09:00:00Z\n', ['b (2026-11-05): archive Started: differs']),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.tearDown()
                self.setUp()
                archive = os.path.join(self.root, 'archive', '2026-11-05-b.md')
                if text is not None:
                    self.write_lines(archive, [text.rstrip('\n')])
                self.final_run(loads=('subagent-driven-development',), archive=archive, fix_growth=None)
                self.assertEqual(self.run_report('--health').splitlines(), ['Health: no problems'] + expected)


    def final_run(self, dispatch_rows=None, loads=(), steps=(), **over):
        """A clean schema 2 Full run of steps, then a simplify and a final review, after dispatch_rows, loading loads."""
        self.schema2_run([*steps, SIMPLIFY_STEP, FINAL_STEP],
                         (dispatch_rows or []) + self.agent('fr', subagent_type='final-reviewer',
                                                            description='Final review'),
                         loads=loads, **over)

    def test_a_full_run_without_sdd_is_warned(self):
        self.final_run()
        self.assertEqual(self.health(),
                         ['WARN: b (2026-11-05): required skill subagent-driven-development did not run'])

    def test_a_plan_record_needs_brainstorming_and_writing_plans(self):
        started = '2026-11-05T10:00:00Z'
        self.session([self.startup(), self.loaded('brainstorming'), self.record_write('', started=started)],
                     project='proj')
        self.records([plan_record(started=started)])
        self.assertEqual(self.health(), ['WARN: b (2026-11-05): required skill writing-plans did not run'])

    def test_an_agent_without_its_pinned_skill_is_warned(self):
        prefix = 'WARN: b (2026-11-05): '
        skill = 'andrej-karpathy-skills:karpathy-guidelines'
        for loads, expected in (
                ('karpathy-guidelines', ['Health: no problems']),
                ('simplify', [prefix + f'final-reviewer ran without its skill {skill}: Final review'])):
            with self.subTest(loads=loads):
                self.tearDown()
                self.setUp()
                # An agent's preloaded skill: a command-message block, then the skill's text.
                rows = [user([{'type': 'text', 'text': f'<command-message>{skill}</command-message>'},
                              {'type': 'text', 'text': f'Base directory for this skill: /x/skills/{loads}\n\n# Skill'}],
                             ts=self.ts(), isMeta=True)]
                rows.append(asst('r-fr', content=[{'type': 'text', 'text': 'Findings: critical=0 important=0 minor=0 '
                                                                           'unneeded=0'}],
                                 model='claude-fable-5-1', ts=self.ts(), effort='high'))
                self.subagent('final-reviewer', rows, project='proj', aid='fr', tid='fr')
                self.final_run(loads=('subagent-driven-development',))
                self.assertEqual(self.health('--agents', self.pins(skills=True)), expected)

    def test_typed_grill_me_without_the_skill_is_warned_and_with_it_is_clean(self):
        warned = ["WARN: session sess-1.jsonl: typed 'grill-me' but grill-me never loaded"]
        for later, expected in (([], warned), (['grill-me'], ['Health: no problems'])):
            with self.subTest(later=later):
                self.tearDown()
                self.setUp()
                # a load before the trigger doesn't meet it
                rows = [self.startup(), self.loaded('grill-me'), user('Grill me on this design', ts=self.ts())]
                self.session(rows + [self.loaded(name) for name in later], project='proj')
                self.assertEqual(self.health(), expected)

    def test_a_session_with_no_run_and_no_startup_guidelines_is_warned(self):
        rows = self.agent('ps', subagent_type='plan-simplifier', description='Plan simplify')
        rows += self.agent('pr', subagent_type='plan-reviewer', description='Plan review')
        self.session(rows, project='proj')
        # the per-run check covers a session with a run, so it gets no second line
        self.session([self.simplify(), self.record_write('c0ffee01')], project='proj', sid='sess-2')
        self.records([run_record(head='c0ffee01')])
        self.assertEqual(self.health(), ['WARN: b (2026-11-05): startup guidelines missing from the session',
                                         'WARN: session sess-1.jsonl: startup guidelines missing'])

    def test_a_chat_only_session_this_week_without_startup_guidelines_is_warned(self):
        self.session([user('Hello', ts=self.ts())], project='proj')
        # last week's session, by its file's time, is not
        self.session([user('Hello', ts='2026-10-30T10:00:00.000Z')], project='proj', sid='sess-2')
        old = datetime.datetime(2026, 10, 30, 10, tzinfo=datetime.timezone.utc).timestamp()
        os.utime(os.path.join(self.projects, 'proj', 'sess-2.jsonl'), (old, old))
        self.assertEqual(self.health(), ['WARN: session sess-1.jsonl: startup guidelines missing'])

    def test_a_compaction_summary_quoting_grill_me_is_not_a_trigger(self):
        rows = [self.startup(), user('Grill me on this design', ts=self.ts()), self.loaded('grill-me'),
                user('Summary: the owner asked "grill me" on the design.', ts=self.ts(), isCompactSummary=True)]
        self.session(rows, project='proj')
        self.assertEqual(self.health(), ['Health: no problems'])

    def test_a_tool_result_quoting_the_skill_text_does_not_count_as_loaded(self):
        quoted = 'Base directory for this skill: /x/skills/subagent-driven-development\n\n# SDD'
        rows = [asst('m-rd', content=[tool_use('rd', 'Read', {'file_path': '/x/notes.md'})], ts=self.ts()),
                user([{'type': 'tool_result', 'tool_use_id': 'rd', 'content': quoted}], ts=self.ts()),
                {'type': 'attachment', 'timestamp': self.ts(),
                 'attachment': {'type': 'invoked_skills',
                                'skills': [{'name': 'subagent-driven-development', 'content': quoted}]}}]
        self.final_run(rows)
        self.assertEqual(self.health(),
                         ['WARN: b (2026-11-05): required skill subagent-driven-development did not run'])

    def test_json_carries_waves_and_the_wave_check_as_written(self):
        waves = [{'wave': 'A', 'tasks': [3, 4], 'conflicts': 0}]
        self.wave_run(waves)
        run = self.json()['runs'][0]
        self.assertEqual(run['record']['waves'], waves)
        self.assertEqual([s['wave'] for s in run['steps']], [None, None, 'A', None, None])

    def test_a_runs_cleaned_waves_reach_json_and_the_page(self):
        self.records([run_record(schema=2, steps=[], waves=[{'wave': 'A', 'tasks': [3, 4], 'conflicts': 1},
                                                            {'wave': 'B', 'tasks': 'x'}, 'not a dict'])])
        run = self.json()['runs'][0]
        self.assertEqual(run['waves'], [{'wave': 'A', 'tasks': [3, 4], 'conflicts': 1},
                                        {'wave': 'B', 'tasks': [], 'conflicts': 0}])
        html = self.html()
        self.assertIn('waves=2', html)
        self.assertIn('<tr><th>wave</th><th>tasks</th><th>conflicts</th></tr>\n<tr><td>A</td><td>3 4</td><td>1</td></tr>'
                      '\n<tr><td>B</td><td></td><td>0</td></tr>', html)

    def test_html_labels_the_wave_check_step(self):
        self.wave_run([{'wave': 'A', 'tasks': [3, 4], 'conflicts': 0}])
        self.assertIn('<td>wave A</td>', self.html())

    def test_offers_are_cleaned_and_reach_json(self):
        offers = [{'skill': 'grill-me', 'outcome': 'accepted'}, {'skill': 'some-skill', 'outcome': 'not_offered'},
                  {'skill': 3, 'outcome': 'accepted'}, {'skill': 'x', 'outcome': 'maybe'}, 'junk']
        self.records([dict(plan_record(), schema=3, offers=offers)])
        self.assertEqual(self.json()['runs'][0]['offers'],
                         [{'skill': 'grill-me', 'outcome': 'accepted', 'reason': None},
                          {'skill': 'some-skill', 'outcome': 'not_offered', 'reason': None}])

    def test_offer_totals_and_page_rows(self):
        self.records([dict(plan_record(), schema=3, offers=[{'skill': 'grill-me', 'outcome': 'accepted'}]),
                      dict(plan_record(), schema=3, offers=[{'skill': 'grill-me', 'outcome': 'skipped'},
                                                            {'skill': 'some-skill', 'outcome': 'updated'}])])
        self.assertEqual(self.json()['totals']['offers'],
                         {'grill-me': {'offered': 2, 'accepted': 1, 'skipped': 1, 'not_offered': 0, 'updated': 0},
                          'some-skill': {'offered': 0, 'accepted': 0, 'skipped': 0, 'not_offered': 0, 'updated': 1}})
        html = self.html()
        self.assertIn('<td>offer grill-me</td><td>2</td>', html)
        self.assertIn('offers=grill-me:skipped some-skill:updated', html)

    def test_a_schema_3_plan_record_missing_a_required_offer_is_warned(self):
        started = '2026-11-05T10:00:00Z'
        offers = [{'skill': 'some-skill', 'outcome': 'accepted'}]
        for schema, rec in ((3, dict(plan_record(started=started), schema=3, offers=offers)),
                            (2, plan_record(started=started))):
            with self.subTest(schema=schema):
                self.tearDown()
                self.setUp()
                self.session([self.startup(), self.loaded('brainstorming'), self.loaded('writing-plans'),
                              self.record_write('', started=started)], project='proj')
                self.records([rec])
                warns = [line for line in self.health() if 'offer' in line]
                if schema == 3:
                    self.assertIn('WARN: b (2026-11-05): no grill-me offer recorded', warns)
                else:
                    self.assertEqual(warns, [])

    def test_json_keeps_old_and_new_records_and_fills_what_each_has(self):
        old = run_record(branch='old', head='dead0001',
                         final_findings={'critical': 0, 'important': 0, 'minor': 2, 'unneeded': 0},
                         simplify={'ran': 'agents', 'applied': 3, 'skipped': 1})
        self.records([old])
        final = {'step': 'final_review', 'counts': {'critical': 0, 'important': 1, 'minor': 0, 'unneeded': 0},
                 'findings': [{'id': 'F1', 'severity': 'important', 'summary': 's', 'outcome': 'fixed',
                               'verdicts': ['fixed']}]}
        fix = {'step': 'final_fix_check', 'counts': {'fixed': 1, 'not_proportionate': 0, 'open': 0}}
        simplify = SIMPLIFY_STEP
        # a row before started is outside the window
        early = asst('early', model='claude-haiku-4-5', ts='2026-11-05T09:00:00.000Z')
        rows = [early] + self.reviewed('fr', 'Findings: critical=0 important=1 minor=0 unneeded=0',
                                       sub_model='claude-fable-5-1', sub_effort='high',
                                       subagent_type='final-reviewer', description='Final review')
        rows += self.reviewed('fc', 'Verdicts: fixed=1 not_proportionate=0 open=0', sub_model='claude-sonnet-5-5',
                              sub_effort='medium', subagent_type='fix-checker', description='Fix-check final findings')
        self.schema2_run([simplify, final, fix], rows,
                         final_findings={'critical': 0, 'important': 1, 'minor': 0, 'unneeded': 0})
        data = self.json()
        first, second = data['runs']
        self.assertEqual((first['schema'], first['record'], first['window'], first['minutes']), (1, old, None, None))
        self.assertEqual(first['warns'], ['WARN: old (2026-11-05): no session transcript found'])
        self.assertIsNone(first['dispatches'])
        transcript = os.path.join(self.projects, 'proj', 'sess-1.jsonl')
        self.assertEqual(second['schema'], 2)
        self.assertEqual(second['window'], {'transcript': transcript, 'start': '2026-11-05T10:00:00.000Z',
                                            'end': '2026-11-05T10:00:10.000Z'})
        self.assertEqual(second['minutes'], 120.0)
        self.assertEqual(second['warns'], [])
        self.assertEqual(sorted(second['models']), ['claude-fable-5-1', 'claude-opus-5-5', 'claude-sonnet-5-5'])
        self.assertEqual(second['main'], {'model': 'claude-opus-5-5', 'effort': None})
        self.assertEqual([{k: d[k] for k in ('kind', 'agent', 'description', 'requested_model', 'model', 'effort',
                                             'skills', 'counts')} for d in second['dispatches']], [
            {'kind': 'final_review', 'agent': 'final-reviewer', 'description': 'Final review', 'requested_model': None,
             'model': 'claude-fable-5-1', 'effort': 'high', 'skills': [],
             'counts': {'critical': 0, 'important': 1, 'minor': 0, 'unneeded': 0}},
            {'kind': 'final_fix_check', 'agent': 'fix-checker', 'description': 'Fix-check final findings',
             'requested_model': None, 'model': 'claude-sonnet-5-5', 'effort': 'medium', 'skills': [],
             'counts': {'fixed': 1, 'not_proportionate': 0, 'open': 0}}])
        board = data['totals']['scoreboard']
        final_row = board['final_review']
        self.assertAlmostEqual(final_row.pop('cost'), 0.0006)  # 10 input and 10 output tokens on Fable 5.1
        self.assertEqual(final_row, {'ran': 2, 'found': 2,
                                     'counts': {'critical': 0, 'important': 1, 'minor': 2, 'unneeded': 0},
                                     'outcomes': {'fixed': 1},
                                     'tokens': {'input': 10, 'output': 10, 'cache_read': 0, 'cache_write': 0}})
        self.assertEqual({k: board['simplify'][k] for k in ('ran', 'found', 'counts', 'outcomes')},
                         {'ran': 2, 'found': 1, 'counts': {'applied': 3, 'skipped': 1}, 'outcomes': {}})
        self.assertEqual(board['plan_review']['ran'], 0)
        self.assertEqual(board['startup'], {'with': 1, 'without': 0})
        self.assertEqual(board['stop_rule'], {'stops': 0})
        self.assertEqual(data['totals']['models']['agents']['final-reviewer'],
                         {'requested': {'-/-': 1}, 'actual': {'claude-fable-5-1/high': 1}, 'mismatches': 0})
        self.assertEqual(data['totals']['models']['main']['full'],
                         {'recommended': {'1': 'opus/medium', '2': 'opus/medium', '3': 'opus/medium',
                                          'out': 'opus/medium'},
                          'actual': {'claude-opus-5-5/-': 1}, 'differs': 0})
        self.assertEqual(data['totals']['health'],
                         {'warns': ['WARN: old (2026-11-05): no session transcript found'],
                          'info': ['Git evidence unavailable for 2 runs']})

    def test_json_prices_date_suffixed_ids_and_leaves_unknown_models_unpriced(self):
        self.session([asst('h', model='claude-haiku-4-5-20251001', u=usage(None, 1000000, 0), ts=self.ts()),
                      asst('u', model='claude-unknown-1', ts=self.ts())], project='proj')
        data = self.json()
        self.assertEqual(data['runs'], [])
        self.assertEqual(data['sessions'], [{
            'file': os.path.join(self.projects, 'proj', 'sess-1.jsonl'), 'skills': [], 'typed': [],
            'models': {'claude-haiku-4-5-20251001': {'input': 1000000, 'output': 0, 'cache_read': 0, 'cache_write': 0,
                                                     'cost': 1.0},
                       'claude-unknown-1': {'input': 10, 'output': 10, 'cache_read': 0, 'cache_write': 0,
                                            'cost': None}},
            'efforts': {}, 'startup': False, 'warns': ['WARN: session sess-1.jsonl: startup guidelines missing']}])
        self.assertEqual(data['totals']['scoreboard']['startup'], {'with': 0, 'without': 1})

    def test_json_weeks_rows(self):
        rows = [self.startup(), asst('big', u=usage(None, 1000000, 0), ts=self.ts()),
                self.record_write('c0ffee01', started='2026-11-05T10:00:00Z')]
        self.session(rows, project='proj')
        # Points count only from records with both ends as numbers: last week's has none, this week's last has one.
        last_week = run_record(date='2026-10-30T12:00:00Z', started='2026-10-30T11:00:00Z', tasks=2, fix_rounds=1,
                               stops=1, final_findings={'critical': 1, 'important': 0, 'minor': 0, 'unneeded': 0},
                               all_end='20')
        del last_week['fable_end']
        half = run_record(head='h4', date='2026-11-06T13:00:00Z', fable_start=40, fable_end='41', all_start=32,
                          all_end=33)
        del half['all_start']
        self.records([
            last_week,
            run_record(head='c0ffee01', path='small', started='2026-11-05T10:00:00Z', tasks=3, fable_start=20,
                       fable_end=35, all_start=20, all_end=30),
            run_record(head='h3', date='2026-11-06T12:00:00Z', path='small', started='2026-11-06T11:30:00Z',
                       fable_start=35, fable_end=40, all_start=30, all_end=32,
                       final_findings={'critical': 0, 'important': 0, 'minor': 2, 'unneeded': 0}),
            half,
        ])
        data = self.json()
        # last week's run has no window, so its cost is unknown, not 0
        weeks = data['totals']['weeks']
        self.assertAlmostEqual(weeks[1].pop('cost'), 4.00024)  # 1M input tokens and the record write's row on Opus
        self.assertEqual(weeks, [
            {'week': '2026-10-25', 'runs': {'full': 1}, 'tasks': 2,
             'findings': {'critical': 1, 'important': 0, 'minor': 0, 'unneeded': 0}, 'fix_rounds': 1, 'stops': 1,
             'fable_points': None, 'all_points': None, 'cost': None, 'minutes_per_run': 60.0, 'later_fixes': 0},
            {'week': '2026-11-01', 'runs': {'full': 1, 'small': 2}, 'tasks': 5,
             'findings': {'critical': 0, 'important': 0, 'minor': 2, 'unneeded': 0}, 'fix_rounds': 0, 'stops': 0,
             'fable_points': 20, 'all_points': 12, 'minutes_per_run': 75.0, 'later_fixes': 0},
        ])
        # a schema 1 run's window has no required-skill check, so its missing skills are unknown
        self.assertIsNotNone(data['runs'][1]['window'])
        self.assertIsNone(data['runs'][1]['missing_skills'])

    def test_json_skill_outcomes(self):
        plan_started, full_started = '2026-11-05T10:00:00Z', '2026-11-05T10:00:20Z'
        brainstorming, writing_plans = self.loaded('brainstorming'), self.loaded('writing-plans')
        spec = asst('m-wr', content=[tool_use('wr', 'Write', {'file_path': '/r/docs/superpowers/specs/x.md',
                                                              'content': 'c'})], ts=self.ts())
        rows = [self.startup(), brainstorming, spec, writing_plans, self.record_write('', started=plan_started)]
        self.clock = 20
        sdd, tdd, simplify = (self.loaded(name) for name in
                              ('subagent-driven-development', 'test-driven-development', 'simplify'))
        rows += [sdd, tdd, simplify, user('Grill me on this', ts=self.ts()),
                 self.record_write('f00d0001', started=full_started)]
        self.session(rows, project='proj')
        self.records([plan_record(started=plan_started),
                      run_record(head='f00d0001', schema=2, started=full_started, steps=[],
                                 simplify={'ran': 'agents', 'applied': 2, 'skipped': 1},
                                 failing_test=[{'task': 1, 'result': 'passed'}])])
        data = self.json()
        plan, full = data['runs']
        self.assertEqual(plan['skills'], [
            {'name': 'brainstorming', 'time': brainstorming['timestamp'], 'outcome': '/r/docs/superpowers/specs/x.md'},
            {'name': 'writing-plans', 'time': writing_plans['timestamp'], 'outcome': None}])
        self.assertEqual(full['skills'], [
            {'name': 'subagent-driven-development', 'time': sdd['timestamp'], 'outcome': True},
            {'name': 'test-driven-development', 'time': tdd['timestamp'], 'outcome': [{'task': 1, 'result': 'passed'}]},
            {'name': 'simplify', 'time': simplify['timestamp'], 'outcome': {'applied': 2, 'skipped': 1}}])
        skills = data['totals']['skills']
        self.assertEqual(skills['brainstorming'],
                         {'runs': 1, 'last': brainstorming['timestamp'], 'outcomes': {'spec': 1}, 'missing': 0})
        self.assertEqual(skills['writing-plans'],
                         {'runs': 1, 'last': writing_plans['timestamp'], 'outcomes': {'none': 1}, 'missing': 0})
        self.assertEqual(skills['grill-me'], {'runs': 0, 'last': None, 'outcomes': {}, 'missing': 1})

    def test_json_survives_a_malformed_stage_and_counts_the_runs_refusals(self):
        reason = 'No simplify since the last final review in this session\'s transcript.'
        rows = [self.startup()] + self.agent('fr', error=reason, subagent_type='final-reviewer',
                                             description='Final review')
        self.session(rows + [self.record_write('c0ffee01')], project='proj')
        self.records([run_record(head='c0ffee01', stage=['2'])])
        data = self.json()
        self.assertEqual(data['runs'][0]['refusals'], {'no_simplify': 1})
        self.assertEqual(data['totals']['models']['main']['full']['actual'], {'claude-opus-5-5/-': 1})

    def test_json_runs_are_oldest_first(self):
        undated = run_record(branch='undated')
        del undated['date']
        self.records([run_record(branch='later', date='2026-11-06T12:00:00Z'), undated,
                      run_record(branch='first', date='2026-11-05T12:00:00+01:00'),
                      run_record(branch='second', date='2026-11-05T11:00:00Z')])
        self.assertEqual([run['record']['branch'] for run in self.json()['runs']],
                         ['first', 'second', 'later', 'undated'])

    def test_json_advisor_and_rule_rows(self):
        advisor_30 = dict(ADVISOR, input_tokens=3000000, output_tokens=0)  # $30 at Fable 5.1's input price
        reason = 'No simplify since the last final review in this session\'s transcript.'
        rows = [self.startup(), asst('m_adv', iterations=[executor(), advisor_30], ts=self.ts()),
                asst('m_fable', model='claude-fable-5-1', u=usage([executor(input_tokens=7000000, output_tokens=0)]),
                     ts=self.ts())]
        self.session(rows + self.agent('fr', error=reason, subagent_type='final-reviewer', description='Final review'),
                     project='proj')
        ruled = {'step': 'task_review', 'task': 1, 'round': 0, 'counts': {'critical': 0, 'important': 1, 'minor': 0},
                 'findings': [{'id': 'F1', 'severity': 'important', 'summary': 's', 'outcome': 'ruled', 'reason': 'r'}]}
        third = {'step': 'fix_check', 'task': 1, 'round': 3, 'counts': {'fixed': 0, 'not_proportionate': 0, 'open': 1}}
        self.records([run_record(schema=2, stops=2, steps=[ruled, third]), run_record(stops=1)])
        totals = self.json()['totals']
        self.assertEqual(totals['advisor'], {
            'calls': 1, 'tokens': {'input': 3000000, 'output': 0, 'cache_read': 0, 'cache_write': 0}, 'cost': 30.0,
            'by_role': {'main': {'calls': 1, 'flagged': False}}, 'failed': {}, 'notes': 0, 'changed': 0, 'missing': 1,
            'weeks': [{'week': '2026-11-01', 'advisor': 30.0, 'fable': 70.0, 'share': 30.0, 'stop': True}],
            'trial': 'runs 2/5, weeks 0/3'})
        board = totals['scoreboard']
        self.assertEqual(board['advisor'], {'calls': 1, 'notes': 0, 'changed': 0, 'cost': 30.0, 'share': 30.0})
        self.assertEqual(board['dispatch_guard'], {'no_simplify': 1})
        self.assertEqual(board['fix_round_cap'], {'runs': 1, 'ruled': 1})
        self.assertEqual(board['stop_rule'], {'stops': 3})
        self.assertEqual(board['task_review']['outcomes'], {'ruled': 1})

    def test_a_malformed_archive_path_reads_as_missing(self):
        self.final_run(loads=('subagent-driven-development',), archive='/p/a\u0000b.md', fix_growth=None)
        self.assertEqual(self.run_report('--health').splitlines(),
                         ['Health: no problems', 'b (2026-11-05): archive missing'])
        self.assertEqual(len(self.json()['runs']), 1)
        page = os.path.join(self.root, 'page.html')
        self.assertIn('Wrote ' + page, self.run_report('--html', page))

    def test_an_injected_row_quoting_grill_me_is_not_a_trigger(self):
        rows = [self.startup(), user('please build it', ts=self.ts(), origin={'kind': 'human'}),
                user('The owner said grill me.', ts=self.ts(), origin={'kind': 'task-notification'})]
        self.session(rows, project='proj')
        self.assertEqual(self.health(), ['Health: no problems'])

    def test_a_simplify_that_found_nothing_reads_none(self):
        self.final_run(loads=('subagent-driven-development', 'simplify'))
        self.assertEqual(self.json()['totals']['skills']['simplify']['outcomes'], {'none': 1})

if __name__ == '__main__':
    unittest.main()
