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
           'simplify': {'ran': 'agents', 'applied': 0, 'skipped': 0}, 'failing_test': [],
           'fix_growth': {'task_added': 0, 'task_removed': 0, 'fix_added': 0, 'fix_removed': 0, 'unplanned_files': []}}
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

    def test_schema_lists_every_run_record_field(self):
        schema = json.loads(self.run_report('--schema'))
        self.assertEqual(sorted(schema), sorted(run_record()))

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
        self.session([asst('m1', iterations=[executor(), ADVISOR])])
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


STARTUP = '<karpathy-guidelines-session-start>guidelines</karpathy-guidelines-session-start>'
NOT_FOUND = "Agent type 'final-reviewer' not found. Available agents: general-purpose, Explore"
NO_RAN_KEY = {'applied': 0, 'skipped': 0}  # an old record, from before simplify.ran
RAN_NO = {'ran': 'no', 'applied': 0, 'skipped': 0}


class HealthCheck(Report):
    """--health reads transcripts: rows get increasing timestamps, and each run ends at its record-writing Bash row."""

    def setUp(self):
        super().setUp()
        self.clock = 0

    def ts(self):
        self.clock += 1
        return f'2026-11-05T10:{self.clock // 60:02d}:{self.clock % 60:02d}.000Z'

    def startup(self):
        return {'type': 'attachment', 'timestamp': self.ts(),
                'attachment': {'type': 'hook_success', 'hookName': 'SessionStart:startup', 'stdout': STARTUP}}

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

    def record_write(self, head, base='0000aaaa'):
        command = f'echo \'{{"base": "{base}", "head": "{head}"}}\' >> "$HOME/.claude/workflow-log.jsonl"'
        return asst('w-' + head, content=[tool_use('w' + head, 'Bash', {'command': command})], ts=self.ts())

    def health(self, *args):
        """The problem lines of --health. Information lines are checked by the tests about them."""
        return [line for line in self.run_report('--health', *args).splitlines() if line.startswith(('WARN', 'Health'))]

    def reviewed(self, tid, report, handback=None, **inp):
        """A launched dispatch whose subagent transcript ends with report as its last text, then a SubagentHandback
        whose message is handback when one is given."""
        rows = [asst('r-' + tid, content=[{'type': 'text', 'text': report}], ts=self.ts())]
        if handback is not None:
            rows.append(asst('h-' + tid, content=[tool_use('hb' + tid, 'SubagentHandback', {'message': handback})],
                             ts=self.ts()))
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
        self.session(self.agent('gp', subagent_type='general-purpose', model='opus', description='Implement Task 2: x'),
                     project='proj', sid='sess-2')
        # none of these: a dispatch that names no workflow role, a denied one, and one last week
        quiet = self.agent('ex', subagent_type='general-purpose', model='opus', description='Explore the code')
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
        rows = self.agent('ps', subagent_type='plan-simplifier', description='Plan simplify')
        rows += self.agent('pr', subagent_type='general-purpose', model='opus', description='Plan review of the plan')
        self.session(rows, project='proj', sid='sess-1')
        self.assertEqual(self.health(), ['Health: no problems'])

    def test_plan_review_without_plan_simplifier_is_flagged(self):
        self.session(self.agent('pr', subagent_type='plan-reviewer', description='Plan review'), project='proj')
        self.assertEqual(self.health(), ['WARN: session sess-1.jsonl: plan review without plan-simplifier: Plan review'])

    def test_a_check_that_mentions_plan_simplifier_does_not_count(self):
        rows = self.agent('ck', subagent_type='general-purpose', model='opus',
                          description='Builder check on plan-simplifier spec')
        rows += self.agent('pr', subagent_type='plan-reviewer', description='Plan review')
        self.session(rows, project='proj')
        self.assertEqual(self.health(), ['WARN: session sess-1.jsonl: plan review without plan-simplifier: Plan review'])

    def test_plan_simplifier_before_plan_review_is_clean(self):
        rows = self.agent('ps', subagent_type='plan-simplifier', description='Plan simplify')
        rows += self.agent('pr', subagent_type='plan-reviewer', description='Plan review')
        self.session(rows, project='proj')
        self.assertEqual(self.health(), ['Health: no problems'])

    def test_each_plan_review_needs_its_own_plan_simplifier(self):
        rows = self.agent('ps', subagent_type='plan-simplifier', description='Plan simplify')
        rows += self.agent('pr1', subagent_type='plan-reviewer', description='Plan review')
        rows += self.agent('pr2', subagent_type='plan-reviewer', description='Plan re-review')
        self.session(rows, project='proj')
        self.assertEqual(self.health(),
                         ['WARN: session sess-1.jsonl: plan review without plan-simplifier: Plan re-review'])

    def test_a_denied_plan_review_is_not_flagged(self):
        self.session(self.agent('pr', error='denied by hook', subagent_type='plan-reviewer', description='Plan review'),
                     project='proj')
        self.assertEqual(self.health(), ['Health: no problems'])

    def test_plan_review_last_week_is_not_flagged(self):
        self.session([asst('m-old', content=[tool_use('old', 'Agent', {'subagent_type': 'plan-reviewer',
                                                                       'description': 'Plan review', 'prompt': 'p'})],
                           ts='2026-10-30T10:00:00.000Z'),
                      user([{'type': 'tool_result', 'tool_use_id': 'old', 'content': 'report'}],
                           ts='2026-10-30T10:00:01.000Z')], project='proj')
        self.assertEqual(self.health(), ['Health: no problems'])

    def test_a_plan_simplifier_that_was_not_found_does_not_count(self):
        rows = self.agent('ps', error="Agent type 'plan-simplifier' not found", subagent_type='plan-simplifier',
                          description='Plan simplify')
        rows += self.agent('pr', subagent_type='plan-reviewer', description='Plan review')
        self.session(rows, project='proj')
        self.assertEqual(self.health(), ['WARN: session sess-1.jsonl: plan review without plan-simplifier: Plan review'])


if __name__ == '__main__':
    unittest.main()
