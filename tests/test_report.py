"""Tests for the workflow report. They run the real script against a fixture transcript tree and run log in a temp folder.

Run with: /usr/bin/python3 -I tests/test_report.py
"""
import datetime
import glob
import hashlib
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
OVER_CAP = {'input_tokens': 300_001, 'output_tokens': 1}  # the usage of one call past CONTEXT_CAP

# The spec's phase order, as PHASES in scripts/workflow-report.py: keep them the same.
PHASES = ('design', 'plan_writing', 'plan_simplify', 'plan_review', 'implement', 'task_review', 'fix_check',
          'simplify', 'final_review', 'final_fix_check', 'coordination', 'unattributed')


# Copied from scripts/workflow-report.py's unit_key, since tests can't import the script: keep them the same.
def key(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]


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


def phase_inputs(unit):
    """The input tokens of each of a unit's phases, by phase name."""
    return {p['name']: sum(m['input'] for m in p['models'].values()) for p in unit['phases']}


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

    def report_args(self, *args):
        # No test reads the real ~/.claude/agents, export stamp or cache; a test's own option comes later and wins.
        return [PYTHON, '-I', SCRIPT, '--projects', self.projects, '--log', self.log, '--now', NOW,
                '--agents', os.path.join(self.root, 'no agents'), '--export-stamp', os.path.join(self.root, 'no stamp'),
                '--cache', os.path.join(self.root, 'cache'), *args]

    def report_process(self, *args, env=CLEAN_ENV):
        return subprocess.run(self.report_args(*args), capture_output=True, text=True, env=env, timeout=60)

    def run_report(self, *args, env=CLEAN_ENV):
        p = self.report_process(*args, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stderr, '')
        return p.stdout

    def json(self, *args):
        data = json.loads(self.run_report('--json', *args))
        for unit in data['runs'] + data['sessions']:
            self.check_phases(unit)
        return data

    def check_phases(self, unit):
        """The spec's rule: a unit's phases add up to its models and its advisor."""
        if unit['models'] is None:
            self.assertIsNone(unit['phases'])
            return
        names = [p['name'] for p in unit['phases']]
        self.assertEqual(names, [n for n in PHASES if n in names])
        self.assertEqual(set().union(*(p['models'] for p in unit['phases'])), set(unit['models']))
        for model, total in unit['models'].items():
            entries = [p['models'][model] for p in unit['phases'] if model in p['models']]
            # The token kinds, TOKENS' labels in scripts/workflow-report.py: keep them the same.
            for kind in ('input', 'output', 'cache_read', 'cache_write'):
                self.assertEqual(sum(e[kind] for e in entries), total[kind])
            if total['cost'] is None:
                self.assertEqual([(e['cost'], e['cost_by_kind']) for e in entries], [(None, None)] * len(entries))
            else:
                self.assertAlmostEqual(sum(e['cost'] for e in entries), total['cost'], delta=1e-6)
                for e in entries:
                    self.assertAlmostEqual(sum(e['cost_by_kind'].values()), e['cost'], delta=1e-6)
        self.assertEqual(sum(p['advisor']['calls'] for p in unit['phases']), unit['advisor']['calls'])
        self.assertAlmostEqual(sum(p['advisor']['cost'] for p in unit['phases']), unit['advisor']['cost'], delta=1e-6)

    def kinds(self, *args):
        return [w['kind'] for w in self.json(*args)['totals']['health']['warnings']]

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

    def test_sweeper_should_not_consult(self):
        self.subagent('sweeper', [asst('m_s', iterations=[executor(), ADVISOR])], aid='s1')
        roles = {line.strip().split(':')[0]: line for line in section(self.run_report(), 'Advisor calls by role:')[1:]}
        self.assertIn('FLAG', roles['sweeper'])

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
                         ['archive', 'categories', 'category_list', 'offers', 'plan', 'schema', 'started', 'steps', 'waves'])

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

    def test_schema_4_steps_carry_the_agent_id(self):
        out = self.run_report('--schema')
        schema = json.loads(out)
        self.assertEqual(schema['schema'], 'int, record format: 5; a record without it is 1')
        self.assertIn('"agent_id"', schema['steps'])
        self.assertIn('every kind but simplify', schema['steps'])

    def test_a_step_agent_id_is_read_as_a_string_or_null(self):
        steps = [{'step': 'final_review', 'agent_id': 'abc123', 'counts': {}, 'findings': []},
                 {'step': 'task_review', 'agent_id': 7, 'counts': {}, 'findings': []},
                 {'step': 'simplify', 'counts': {}, 'findings': []}]
        self.records([run_record(schema=4, steps=steps)])
        self.assertEqual([s['agent_id'] for s in self.json()['runs'][0]['steps']], ['abc123', None, None])

    def test_schema_5_labels_are_cleaned_and_reach_json(self):
        steps = [
            {'step': 'plan_review', 'counts': {'findings': 3}, 'strengths': [
                {'category': 'scope', 'summary': 's'}, 'x', {'category': ['scope'], 'summary': 3}],
             'findings': [{'id': 'F1', 'category': 'code_facts', 'validity': 'valid'},
                          {'id': 'F2', 'category': 'correctness', 'validity': 'maybe'},
                          {'id': 'F3', 'category': {'a': 1}, 'validity': ['valid']}]},
            {'step': 'task_review', 'task': 1, 'round': 0, 'counts': {}, 'strengths': 'not a list',
             'findings': [{'id': 'F4', 'category': 'nope', 'validity': 'invalid'}]},
            {'step': 'simplify', 'counts': {}, 'strengths': [{'category': 'reuse', 'summary': 's'}],
             'findings': [{'id': 'F5', 'category': 'reuse', 'validity': 'unclear'}]},
            {'step': 'fix_check', 'task': 1, 'round': 1, 'counts': {},
             'findings': [{'id': 'F6', 'category': 'tests', 'validity': 'valid'}]}]
        self.records([run_record(schema=5, categories=1, steps=steps),
                      run_record(branch='old', schema=4, steps=[{'step': 'task_review', 'findings': [{'id': 'F1'}]}]),
                      run_record(branch='odd', schema=5, categories=True, steps=[])])
        runs = {r['record']['branch']: r for r in self.json()['runs']}
        new = runs['b']
        self.assertEqual(new['categories'], 1)
        self.assertEqual([[(f['category'], f['validity']) for f in s['findings']] for s in new['steps']],
                         [[('code_facts', 'valid'), (None, None), (None, None)], [(None, 'invalid')],
                          [('reuse', 'unclear')], [(None, 'valid')]])
        self.assertEqual([s['strengths'] for s in new['steps']],
                         [[{'category': 'scope', 'summary': 's'}, {'category': None, 'summary': None}], None, None, None])
        old = runs['old']['steps'][0]
        self.assertEqual((old['strengths'], old['findings'][0]['category'], old['findings'][0]['validity']),
                         (None, None, None))
        self.assertIsNone(runs['old']['categories'])
        self.assertIsNone(runs['odd']['categories'])

    def test_json_carries_the_category_list(self):
        cats = self.json()['categories']
        self.assertEqual((cats['version'], sorted(cats['validity'])), (1, ['invalid', 'unclear', 'valid']))
        self.assertTrue(cats['rule'].startswith('Name the problem, not its fix'))
        self.assertEqual([(a['area'], a['steps']) for a in cats['areas']],
                         [('plan', ['plan_simplify', 'plan_review']), ('implementation', ['task_review', 'final_review']),
                          ('cleanup', ['simplify'])])
        plan, implementation, cleanup = (a['list'] for a in cats['areas'])
        self.assertEqual([c['id'] for c in plan],
                         ['scope', 'task_split', 'spec_gap', 'risk_missed', 'test_plan', 'code_facts'])
        self.assertEqual([c['id'] for c in implementation],
                         ['correctness', 'error_handling', 'data_safety', 'security_privacy', 'reuse', 'conventions',
                          'simplicity', 'tests', 'naming', 'performance', 'docs'])
        self.assertEqual(cleanup, implementation)
        self.assertTrue(all(sorted(c) == ['id', 'label', 'means'] for c in plan + implementation))

    def test_schema_5_documents_the_labels_and_prints_the_list(self):
        schema = json.loads(self.run_report('--schema'))
        for name in ('"category"', '"validity"', '"strengths"'):
            self.assertIn(name, schema['steps'])
        self.assertEqual(schema['category_list'], self.json()['categories'])

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
        self.assertEqual(self.kinds('--since', '2026-11-02'), ['simplify_not_run', 'no_transcript'])
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
        self.assertEqual(sorted(data), ['categories', 'generated', 'now', 'runs', 'sessions', 'since', 'stage_table', 'totals', 'version'])
        self.assertEqual(sorted(data['totals']),
                         ['advisor', 'context', 'health', 'models', 'offers', 'run_time', 'scoreboard', 'skills',
                          'weeks'])
        self.assertEqual(data['now'], '2026-11-07T12:00:00+00:00')
        self.assertIsNone(data['since'])

    def test_json_exports_the_stage_table_once(self):
        data = self.json()
        self.assertEqual(data['stage_table'],
                         {path: row['recommended'] for path, row in data['totals']['models']['main'].items()})

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
        self.assertNotIn('<th>strength</th>', html)
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

    def test_html_shows_categories_validity_and_strengths(self):
        step = {'step': 'task_review', 'wave': 'A', 'round': 0, 'counts': {'critical': 0, 'important': 0, 'minor': 1},
                'findings': [{'id': 'F1', 'severity': 'minor', 'summary': 's', 'outcome': 'deferred',
                              'category': 'correctness', 'validity': 'valid'}],
                'strengths': [{'category': 'tests', 'summary': '<i>pinned</i>'}]}
        self.records([run_record(schema=5, categories=1, steps=[step])])
        html = self.html()
        self.assertIn('<th>category</th><th>summary</th><th>outcome</th><th>validity</th><th>reason</th>', html)
        self.assertIn('<td>correctness</td><td>s</td><td>deferred</td><td>valid</td>', html)
        self.assertIn('<tr><td>task review</td><td>wave A</td><td>tests</td><td>&lt;i&gt;pinned&lt;/i&gt;</td></tr>', html)

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
        p = subprocess.Popen(self.report_args('--json'), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=CLEAN_ENV)
        self.assertEqual(len(p.stdout.read(300)), 300)
        p.stdout.close()
        stderr = p.stderr.read()
        p.stderr.close()
        self.assertEqual(p.wait(timeout=60), 0, stderr)
        self.assertEqual(stderr, b'')

    def test_a_session_has_its_transcripts_start_end_and_project(self):
        self.session([user('hi', ts='2026-11-05T10:00:00.000Z', cwd=None),
                      user('hi', ts='2026-11-05T10:00:01.000Z', cwd='/home/dev/demo'),
                      dict(asst('m1', ts='2026-11-05T10:01:00.000Z'), cwd='/home/dev/demo/.claude/worktrees/w1'),
                      {'type': 'queue-operation', 'timestamp': '2026-10-30T09:00:00.000Z'},  # out of order, before --since
                      dict(user('later', ts='2026-11-08T09:00:00.000Z'), cwd='/home/dev/other')])  # after --now
        self.session([asst('m2', ts=None)], sid='sess-2')
        facts = {os.path.basename(s['file']): {k: s[k] for k in ('start', 'end', 'project')}
                 for s in self.json('--since', '2026-11-01')['sessions']}
        self.assertEqual(facts, {'sess-1.jsonl': {'start': '2026-10-30T09:00:00.000Z', 'end': '2026-11-08T09:00:00.000Z',
                                                  'project': '/home/dev/demo'},
                                 'sess-2.jsonl': {'start': None, 'end': None, 'project': None}})

    def test_a_sessions_slash_commands_are_kept_by_name_and_time_only(self):
        human = {'origin': {'kind': 'human'}}
        at = lambda m: f'2026-11-05T10:{m:02d}:00.000Z'
        self.session([
            user('<command-name>/model</command-name>\n<command-message>model</command-message>\n'
                 '<command-args>secret-arg-text</command-args>', ts=at(0)),  # a built-in: no origin
            user('<command-message>demo-skill</command-message>\n<command-name>/demo-skill</command-name>', ts=at(2),
                 **human),
            user([{'type': 'text', 'text': '<command-name>/listed</command-name>'}], ts=at(3), **human),
            user('please run <command-name>/quoted</command-name>', ts=at(4), **human),
            user('<command-name>/meta</command-name>', ts=at(5), isMeta=True),
            user('<command-name>/summary</command-name>', ts=at(6), isCompactSummary=True),
            user('<command-name>/injected</command-name>', ts=at(7), origin={'kind': 'task-notification'}),
            user('<command-name>noslash</command-name>', ts=at(8), **human),
            asst('m1', content=[{'type': 'text', 'text': '<command-name>/echoed</command-name>'}], ts=at(10)),
            {'type': 'api-request-blob', 'timestamp': at(11),
             'message': {'role': 'user', 'content': '<command-name>/blob</command-name>'}},
            user('<command-name>/late</command-name>', ts='2026-11-08T09:00:00.000Z', **human),  # after --now
        ])
        out = self.run_report('--json')
        self.assertEqual(json.loads(out)['sessions'][0]['commands'],
                         [{'name': 'model', 'time': at(0)}, {'name': 'demo-skill', 'time': at(2)},
                          {'name': 'listed', 'time': at(3)}])
        self.assertNotIn('secret-arg-text', out)
        for path in glob.glob(os.path.join(self.root, 'cache', '**', '*.json'), recursive=True):
            with open(path) as f:
                self.assertNotIn('secret-arg-text', f.read())

    def test_a_cache_written_before_session_facts_is_read_again(self):
        self.session([dict(asst('m1'), cwd='/home/dev/demo')])
        self.json()
        for entry in glob.glob(os.path.join(self.root, 'cache', 'transcripts', '*-usage.json')):
            with open(entry) as f:
                stored = json.load(f)
            stored['version'] = 1  # CR6's, before these facts
            for name in ('first', 'cwd'):
                stored['value'].pop(name)
            with open(entry, 'w') as f:
                json.dump(stored, f)
        self.assertEqual(self.json()['sessions'][0]['project'], '/home/dev/demo')


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

    def agent(self, tid, error=None, agent_id=None, **inp):
        """An Agent dispatch and its tool_result: an error when error is given, else a launched agent's report, whose
        row carries agent_id as its toolUseResult's agentId when given."""
        result = {'type': 'tool_result', 'tool_use_id': tid, 'content': error or 'report'}
        if error:
            result['is_error'] = True
        extra = {} if agent_id is None else {'toolUseResult': {'agentId': agent_id}}
        return [asst('m-' + tid, content=[tool_use(tid, 'Agent', dict(inp, prompt='p'))], ts=self.ts()),
                user([result], ts=self.ts(), **extra)]

    def handoff(self, prompt='Handoff 1: go'):
        """A start_session call whose brief is prompt, and its result naming the session it started. The app's session
        id never names the new session's transcript; the brief in its first user row, as briefed writes it, does."""
        tid = f'h{self.clock}'
        return [asst('a-' + tid, [tool_use(tid, 'mcp__ccd_session__start_session', {'prompt': prompt})], ts=self.ts()),
                user([{'type': 'tool_result', 'tool_use_id': tid, 'content': '(session_id: local_9999, name: n)'}],
                     ts=self.ts())]

    def briefed(self, prompt='Handoff 1: go'):
        """The first user row of a session that start_session started with the brief prompt."""
        return user(f'Task (from the session that started this one):\n{prompt}', ts=self.ts())

    def record_write(self, head, base='0000aaaa', started=None, command=None):
        """The Bash row that appends the record with head to the log: an echo of its JSON, or command when given."""
        fields = f'"base": "{base}", "head": "{head}"' + (f', "started": "{started}"' if started else '')
        command = command or f'echo \'{{{fields}}}\' >> "$HOME/.claude/workflow-log.jsonl"'
        return asst('w-' + head, content=[tool_use('w' + head, 'Bash', {'command': command})], ts=self.ts())

    def loaded(self, name):
        """The row a skill load writes."""
        return user([{'type': 'text', 'text': f'Base directory for this skill: /x/skills/{name}\n\n# {name}'}],
                    ts=self.ts(), isMeta=True)

    def said(self, tokens):
        """A main-session message of tokens input tokens and no output."""
        return asst(f'said-{self.clock}', u=usage(None, tokens, 0), ts=self.ts())

    def health(self, *args):
        """The problem lines of --health. Information lines are checked by the tests about them."""
        return [line for line in self.run_report('--health', *args).splitlines() if line.startswith(('WARN', 'Health'))]

    def reviewed(self, tid, report, handback=None, sub_model=None, sub_effort=None, u=None, sid='sess-1', **inp):
        """A launched dispatch whose subagent transcript, under the main transcript sid, ends with report as its last
        text, then a SubagentHandback whose message is handback when one is given. Its rows run on sub_model and
        sub_effort when given, and its report row has usage u when given."""
        ran = dict({'model': sub_model} if sub_model else {}, effort=sub_effort)
        rows = [asst('r-' + tid, content=[{'type': 'text', 'text': report}], ts=self.ts(), u=u, **ran)]
        if handback is not None:
            rows.append(asst('h-' + tid, content=[tool_use('hb' + tid, 'SubagentHandback', {'message': handback})],
                             ts=self.ts(), **ran))
        self.subagent(inp.get('subagent_type') or 'general-purpose', rows, project='proj', sid=sid, aid=tid, tid=tid)
        return self.agent(tid, **inp)

    def handed_off(self, calls):
        """An old session aaaa that dispatches an implementer, then makes calls, (prompt, result) pairs of
        start_session calls whose result row is left out when result is None, then writes one more row; and a new
        session bbbb, whose first user row holds every call's brief, that runs the final review and writes the record
        of a run started before both."""
        started = '2026-11-05T10:00:00Z'
        old = [asst('a1', ts=self.ts())]
        old += self.reviewed('im', 'DONE', sid='aaaa', subagent_type='implementer', description='Implement Task 1')
        for n, (prompt, result) in enumerate(calls):
            old.append(asst(f'call{n}', [tool_use(f'h{n}', 'mcp__ccd_session__start_session',
                                                  {'prompt': prompt, 'background': 'x' * 80})], ts=self.ts()))
            if result is not None:
                old.append(user([{'type': 'tool_result', 'tool_use_id': f'h{n}', 'content': result}], ts=self.ts()))
        old.append(asst('a3', ts=self.ts()))  # after the handoff: the old session's own row
        self.session(old, project='proj', sid='aaaa')
        new = [self.briefed('\n'.join(prompt for prompt, _ in calls))]
        new += self.reviewed('fr', 'ok\nFindings: critical=0 important=0 minor=0 unneeded=0 strengths=0', sid='bbbb',
                             subagent_type='final-reviewer', description='Final review')
        new.append(self.record_write('c0ffee01', started=started))
        self.session(new, project='proj', sid='bbbb')
        steps = [{'step': 'final_review', 'agent_id': 'fr', 'findings': [], 'strengths': [],
                  'counts': {'critical': 0, 'important': 0, 'minor': 0, 'unneeded': 0}}]
        self.records([run_record(head='c0ffee01', started=started, schema=4, steps=steps)])

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
        self.assertEqual(self.kinds(), ['agent_not_found', 'final_review_without_simplify'])

    def test_titles_come_from_the_last_custom_title_row(self):
        def title_row(text):
            return {'type': 'custom-title', 'customTitle': text, 'sessionId': 's'}
        rows = [self.startup(), title_row('First name'), self.simplify(), title_row(7), title_row('Second name')]
        rows += self.reviewed('fr', 'Findings: critical=0 important=0 minor=0 unneeded=0',
                              subagent_type='final-reviewer', description='Final review')
        rows.append(self.record_write('c0ffee01', started='2026-11-05T10:00:00Z'))
        self.session(rows, project='proj')
        self.session([title_row('Chat'), asst('x', ts='2026-11-05T11:00:00.000Z')], project='proj', sid='sess-2')
        self.session([asst('y', ts='2026-11-05T11:00:00.000Z')], project='proj', sid='sess-3')
        self.records([run_record(head='c0ffee01', schema=2, started='2026-11-05T10:00:00Z', steps=[]),
                      run_record(head='nowhere', branch='lost')])
        data = self.json()
        self.assertEqual([r['title'] for r in data['runs']], ['Second name', None])
        self.assertEqual(sorted((os.path.basename(s['file']), s['title']) for s in data['sessions']),
                         [('sess-1.jsonl', 'Second name'), ('sess-2.jsonl', 'Chat'), ('sess-3.jsonl', None)])

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
        self.assertEqual(self.kinds(), ['simplify_record_differs'])

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
        self.assertEqual(self.kinds(), ['model_less_dispatch'])

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
        self.assertEqual(self.kinds(), ['named_role_by_general_purpose'] * len(launched))

    def test_run_with_no_record_write_warns(self):
        self.records([run_record(head='c0ffee01')])
        self.assertEqual(self.health(), ['WARN: b (2026-11-05): no session transcript found'])

    def test_a_record_written_in_python_or_shell_quotes_still_ends_its_run(self):
        """A record built in a Python dict or an echo "..." string quotes its values with ' or \\"."""
        python = ('/usr/bin/python3 -c "import json; print(json.dumps({\'started\': \'2026-11-05T10:00:00Z\', '
                  '\'head\': \'c0ffee01\'}))" >> ~/.claude/workflow-log.jsonl')
        escaped = 'echo "{\\"head\\": \\"c0ffee02\\"}" >> ~/.claude/workflow-log.jsonl'
        self.session([self.startup(), self.simplify(), self.record_write('c0ffee01', command=python)], project='proj')
        self.session([self.startup(), self.simplify(), self.record_write('c0ffee02', command=escaped)], project='proj',
                     sid='sess-2')
        self.records([run_record(head='c0ffee01', started='2026-11-05T10:00:00Z'),
                      run_record(head='c0ffee02', branch='b2')])
        self.assertEqual(self.health(), ['Health: no problems'])

    def test_brief_shows_first_warning_and_count(self):
        self.silent_case()
        # last week's run, within --health's 7 days, shows in --health but is not counted in this week's brief
        self.records([run_record(branch='old', head='dead0001', date='2026-11-01T05:00:00Z')])
        health = self.health()
        self.assertEqual(len(health), 11)
        self.assertIn('WARN: old (2026-11-01): no session transcript found', health)
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
        self.assertEqual(self.kinds(), ['final_findings_differ', 'named_role_by_general_purpose'])

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
        self.assertEqual(self.kinds(), ['conventions_not_added'])

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
        self.assertEqual(self.kinds(), ['fix_added_differs'])

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
        self.assertEqual(self.kinds(), ['unrecorded_session', 'unrecorded_session'])
        self.assertEqual(self.run_report('--brief').splitlines()[2],
                         'WARN: session sess-1.jsonl ran workflow agents but wrote no run record (+1 more, run --health)')

    def test_a_handed_off_run_reads_its_whole_chain(self):
        self.handed_off([('Handoff 1: continue at task 2', 'Started session "n" (session_id: local_9999, name: n).')])
        data = self.json()
        [run] = data['runs']
        a = os.path.join(self.projects, 'proj', 'aaaa.jsonl')
        self.assertEqual([d['agent'] for d in run['dispatches']], ['implementer', 'final-reviewer'])
        self.assertEqual([d['step'] for d in run['dispatches']], [None, 0])
        # aaaa's three main calls up to the handoff call, and bbbb's dispatch and record write
        self.assertEqual(run['context']['calls'], 5)
        self.assertNotIn('unrecorded_session', self.kinds())
        self.assertNotIn('step_agent_unknown', self.kinds())
        session_a = next(s for s in data['sessions'] if s['file'] == a)
        # only the row after the handoff is the old session's
        self.assertEqual(session_a['models']['claude-opus-5-5']['input'], 10)
        self.assertEqual(session_a['context']['calls'], 1)

    def test_a_start_session_without_the_marker_joins_no_chain(self):
        self.handed_off([('Implement the plan. Handoff 1: quoted in the text', '(session_id: local_9999, name: n)'),
                         ('Handoff 2: torn', None)])
        [run] = self.json()['runs']
        self.assertEqual([d['agent'] for d in run['dispatches']], ['final-reviewer'])
        # the old transcript's implementer dispatch is outside every run
        self.assertIn('unrecorded_session', self.kinds())

    def test_a_start_session_that_started_no_session_joins_no_chain(self):
        self.handed_off([('Handoff 1: proposed', 'Proposed "x" (task_id: task_1)')])
        [run] = self.json()['runs']
        self.assertEqual([d['agent'] for d in run['dispatches']], ['final-reviewer'])
        self.assertIn('unrecorded_session', self.kinds())

    def test_a_predecessor_row_belongs_to_the_run_whose_window_on_it_ends_first(self):
        # aaaa's refused dispatch, before both handoffs, is in both runs' chains but only r1's window on aaaa holds it
        old = self.agent('gp', error='This dispatch has no model and no named agent, so it would run',
                         subagent_type='general-purpose', description='x')
        old += self.handoff('Handoff 1: one') + self.handoff('Handoff 2: two')
        self.session(old, project='proj', sid='aaaa')
        self.session([self.briefed('Handoff 1: one'), self.record_write('c0ffee01')], project='proj', sid='bbbb')
        self.session([self.briefed('Handoff 2: two'), self.record_write('c0ffee02')], project='proj', sid='cccc')
        self.records([run_record(branch='r1', head='c0ffee01'), run_record(branch='r2', head='c0ffee02')])
        refusals = {r['record']['branch']: r['refusals'] for r in self.json()['runs']}
        self.assertEqual(refusals, {'r1': {'no_model': 1}, 'r2': {}})

    def test_a_session_after_a_handoff_keeps_only_its_own_rows(self):
        old = [self.loaded('brainstorming')] + self.handoff()
        self.session(old, project='proj', sid='aaaa')
        self.session([self.briefed(), asst('b1', ts=self.ts())], project='proj', sid='bbbb')
        skills = {os.path.basename(s['file']): [k['name'] for k in s['skills']] for s in self.json()['sessions']}
        self.assertEqual(skills, {'aaaa.jsonl': ['brainstorming'], 'bbbb.jsonl': []})

    def test_a_session_after_a_handoff_takes_no_phase_from_its_predecessor(self):
        old = [self.loaded('brainstorming')] + self.handoff()
        self.session(old, project='proj', sid='aaaa')
        self.session([self.briefed(), asst('b1', ts=self.ts())], project='proj', sid='bbbb')
        phases = {os.path.basename(s['file']): [p['name'] for p in s['phases']] for s in self.json()['sessions']}
        self.assertEqual(phases['bbbb.jsonl'], ['unattributed'])

    def test_a_handed_off_run_starts_after_the_last_record_its_chain_wrote(self):
        # R0 is recorded in aaaa before the handoff; R1, recorded in bbbb with no started, starts after R0's record
        old = self.reviewed('im', 'DONE', sid='aaaa', subagent_type='implementer', description='Implement Task 1')
        old += [self.record_write('c0ffee00')] + self.handoff()
        self.session(old, project='proj', sid='aaaa')
        new = [self.briefed()]
        new += self.reviewed('fr', 'ok', sid='bbbb', subagent_type='final-reviewer', description='Final review')
        new.append(self.record_write('c0ffee01'))
        self.session(new, project='proj', sid='bbbb')
        self.records([run_record(branch='r0', head='c0ffee00'), run_record(branch='r1', head='c0ffee01')])
        agents = {r['record']['branch']: [d['agent'] for d in r['dispatches']] for r in self.json()['runs']}
        self.assertEqual(agents, {'r0': ['implementer'], 'r1': ['final-reviewer']})

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
        self.assertEqual(self.kinds(), ['plan_review_without_simplify'])

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
        """A fixture agents folder: final-reviewer pinned opus/high and fix-checker sonnet/medium, listing their
        skills only when skills is true."""
        folder = os.path.join(self.root, 'agents')
        os.makedirs(folder, exist_ok=True)
        for name, model, effort, listed in (('final-reviewer', 'opus', 'high', ['andrej-karpathy-skills:karpathy-guidelines']),
                                            ('fix-checker', 'sonnet', 'medium', [])):
            lines = ['---', f'name: {name}', 'description: d', f'model: {model}', f'effort: {effort}']
            if skills and listed:
                lines += ['skills:'] + [f'  - {skill}' for skill in listed]
            with open(os.path.join(folder, name + '.md'), 'w') as f:
                f.write('\n'.join(lines + ['---', 'Body.']) + '\n')
        return folder

    def schema2_run(self, steps, dispatch_rows, loads=('subagent-driven-development',), schema=2, **over):
        rows = [self.startup()] + [self.loaded(name) for name in loads] + [self.simplify()] + dispatch_rows
        rows.append(self.record_write('c0ffee01', started='2026-11-05T10:00:00Z'))
        self.session(rows, project='proj')
        self.records([run_record(head='c0ffee01', schema=schema, started='2026-11-05T10:00:00Z', steps=steps, **over)])

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

    def test_dispatches_carry_their_id_start_and_end(self):
        reviewed = self.reviewed('fr', 'Findings: critical=0 important=0 minor=0 unneeded=0',
                                 subagent_type='final-reviewer', description='Final review')
        launched = self.agent('gp', agent_id='abc123', subagent_type='general-purpose', model='opus', description='x')
        odd = self.agent('gq', subagent_type='general-purpose', model='opus', description='y')
        odd[1]['toolUseResult'] = 'not a dict'
        self.schema2_run([SIMPLIFY_STEP], reviewed + launched + odd)
        with open(os.path.join(self.projects, 'proj', 'sess-1', 'subagents', 'agent-fr.jsonl')) as f:
            sub_rows = [json.loads(line) for line in f]
        dispatches = self.json()['runs'][0]['dispatches']
        self.assertEqual([(d['id'], d['time'], d['end']) for d in dispatches], [
            ('fr', reviewed[-2]['timestamp'], sub_rows[-1]['timestamp']),
            ('abc123', launched[0]['timestamp'], None),
            (None, odd[0]['timestamp'], None)])

    def test_steps_tie_to_dispatches_by_agent_id_or_when_alone(self):
        def step(kind, agent_id=None, **more):
            return dict({'step': kind, 'agent_id': agent_id, 'counts': {}, 'findings': []}, **more)
        rows = self.reviewed('tr1', 'Findings: critical=0 important=0 minor=0', subagent_type='task-reviewer', description='Review Task 1')
        rows += self.reviewed('tr2', 'Findings: critical=0 important=0 minor=0', subagent_type='task-reviewer', description='Review Task 2')
        rows += self.reviewed('fc', 'Verdicts: fixed=0 not_proportionate=0 open=0', subagent_type='fix-checker', description='Fix-check Task 1')
        rows += self.reviewed('fr', 'Findings: critical=0 important=0 minor=0 unneeded=0', subagent_type='final-reviewer', description='Final review')
        steps = [step('task_review', 'tr2', task=2, round=0), step('task_review', 'tr1', task=1, round=0),
                 step('fix_check', task=1, round=1), step('final_review', 'nope'), SIMPLIFY_STEP]
        call = self.simplify()
        session = [self.startup(), self.loaded('subagent-driven-development'), call, self.loaded('simplify')] + rows
        session.append(self.record_write('c0ffee01', started='2026-11-05T10:00:00Z'))
        self.session(session, project='proj')
        self.records([run_record(head='c0ffee01', schema=4, started='2026-11-05T10:00:00Z', steps=steps),
                      run_record(head='nowhere', branch='lost', schema=4, steps=[step('final_review', 'x')])])
        run, lost = self.json()['runs']
        self.assertEqual([(s['time'], s['end']) for s in lost['steps']], [(None, None)])
        by_id = {d['id']: d for d in run['dispatches']}
        self.assertEqual([d['step'] for d in run['dispatches']], [1, 0, 2, None])
        self.assertEqual([s['time'] for s in run['steps']],
                         [by_id['tr2']['time'], by_id['tr1']['time'], by_id['fc']['time'], None, call['timestamp']])
        self.assertEqual([s['end'] for s in run['steps']][:3], [by_id['tr2']['end'], by_id['tr1']['end'], by_id['fc']['end']])
        self.assertEqual([s['step'] for s in run['skills'] if s['name'] == 'simplify'], [4])
        self.assertTrue(all(s['step'] is None for s in run['skills'] if s['name'] != 'simplify'))

    def test_a_schema_2_run_ties_its_single_final_review_but_not_two_task_reviews(self):
        final = FINAL_STEP
        task = {'step': 'task_review', 'task': 1, 'round': 0, 'counts': {'critical': 0, 'important': 0, 'minor': 0}, 'findings': []}
        rows = self.reviewed('t1', 'Findings: critical=0 important=0 minor=0', subagent_type='task-reviewer', description='Review Task 1')
        rows += self.reviewed('t2', 'Findings: critical=0 important=0 minor=0', subagent_type='task-reviewer', description='Review Task 2')
        rows += self.reviewed('fr', 'Findings: critical=0 important=0 minor=0 unneeded=0', subagent_type='final-reviewer', description='Final review')
        self.schema2_run([SIMPLIFY_STEP, task, dict(task, task=2), final], rows)
        run = self.json()['runs'][0]
        self.assertEqual([d['step'] for d in run['dispatches']], [None, None, 3])
        self.assertEqual([s['time'] is None for s in run['steps']], [False, True, True, False])
        self.assertEqual([s['agent_id'] for s in run['steps']], [None] * 4)

    def test_schema_4_steps_are_compared_by_agent_id_not_by_order(self):
        counts_a = {'critical': 0, 'important': 1, 'minor': 0}
        counts_b = {'critical': 0, 'important': 0, 'minor': 2}
        rows = self.reviewed('ta', 'Findings: critical=0 important=1 minor=0', subagent_type='task-reviewer', description='Review Task 1')
        rows += self.reviewed('tb', 'Findings: critical=0 important=0 minor=2', subagent_type='task-reviewer', description='Review Task 2')
        finding = lambda sev: {'id': 'F', 'severity': sev, 'summary': 's', 'outcome': 'deferred'}
        steps = [{'step': 'task_review', 'task': 2, 'round': 0, 'agent_id': 'tb', 'counts': counts_b, 'findings': [finding('minor')] * 2},
                 {'step': 'task_review', 'task': 1, 'round': 0, 'agent_id': 'ta', 'counts': counts_a, 'findings': [finding('important')]},
                 SIMPLIFY_STEP]
        self.schema2_run(steps, rows, schema=4)
        self.assertEqual(self.health(), ['Health: no problems'])

    def test_a_missing_unknown_mismatched_or_shared_agent_id_is_warned_and_ties_nothing(self):
        rows = self.reviewed('tr', 'Findings: critical=0 important=0 minor=0', subagent_type='task-reviewer', description='Review Task 1')
        rows += self.reviewed('fc', 'Verdicts: fixed=0 not_proportionate=0 open=0', subagent_type='fix-checker', description='Fix-check Task 1')
        rows += self.reviewed('fr', 'Findings: critical=0 important=0 minor=0 unneeded=0', subagent_type='final-reviewer', description='Final review')
        counts = {'critical': 0, 'important': 0, 'minor': 0}
        steps = [{'step': 'task_review', 'task': 1, 'round': 0, 'counts': counts, 'findings': []},
                 {'step': 'fix_check', 'task': 1, 'round': 1, 'agent_id': 'tr', 'counts': {'fixed': 0, 'not_proportionate': 0, 'open': 0}},
                 dict(FINAL_STEP, agent_id='nope'),
                 {'step': 'final_fix_check', 'agent_id': 'fc', 'counts': {'fixed': 0, 'not_proportionate': 0, 'open': 0}},
                 {'step': 'final_fix_check', 'agent_id': 'fc', 'counts': {'fixed': 0, 'not_proportionate': 0, 'open': 0}},
                 SIMPLIFY_STEP]
        self.schema2_run(steps, rows, schema=4)
        prefix = 'WARN: b (2026-11-05): '
        lines = self.health()
        self.assertIn(prefix + 'record has 2 final_fix_check steps, transcript launched 0', lines)
        self.assertIn(prefix + 'task_review step 1 has no agent id', lines)
        self.assertIn(prefix + "fix_check step 1's agent id tr belongs to a task_review dispatch", lines)
        self.assertIn(prefix + "final_review step 1's agent id nope matches no launched dispatch", lines)
        self.assertIn(prefix + 'final_fix_check step 1 and final_fix_check step 2 share agent id fc', lines)
        run = self.json()['runs'][0]
        # The task review has no id, so the JSON still ties it one-and-one; the health check warned instead of comparing.
        self.assertEqual([d['step'] for d in run['dispatches']], [0, None, None])
        kinds = {w['kind']: w for w in run['warnings']}
        self.assertEqual(kinds['step_agent_mismatch']['step'], {'step': 'fix_check', 'task': 1, 'round': 1, 'wave': None})
        self.assertEqual(kinds['step_agent_mismatch']['agent'], 'task-reviewer')

    def labelled_run(self, task_strengths, task_line, final_strengths, final_line, findings=None):
        """A schema 5 run: one task review with one minor finding and a final review, tied by agent id."""
        finding = {'id': 'F1', 'severity': 'minor', 'summary': 's', 'outcome': 'deferred', 'category': 'tests',
                   'validity': 'valid'}
        task = {'step': 'task_review', 'wave': 'A', 'round': 0, 'agent_id': 'tr',
                'counts': {'critical': 0, 'important': 0, 'minor': 1}, 'findings': findings or [finding]}
        final = dict(FINAL_STEP, agent_id='fr')
        if task_strengths is not None:
            task['strengths'] = task_strengths
        if final_strengths is not None:
            final['strengths'] = final_strengths
        rows = self.reviewed('tr', task_line, subagent_type='task-reviewer', description='Review wave A')
        rows += self.reviewed('fr', final_line, subagent_type='final-reviewer', description='Final review')
        self.schema2_run([task, SIMPLIFY_STEP, final], rows, schema=5, categories=1)

    def test_a_labelled_schema_5_run_is_healthy(self):
        strength = {'category': 'correctness', 'summary': 's'}
        self.labelled_run([], 'Findings: critical=0 important=0 minor=1 strengths=0',
                          [strength], 'Findings: critical=0 important=0 minor=0 unneeded=0')
        self.assertEqual(self.health(), ['Health: no problems'])

    def test_a_strength_with_no_summary_is_warned(self):
        self.labelled_run([{'category': 'tests'}], 'Findings: critical=0 important=0 minor=1 strengths=1',
                          [], 'Findings: critical=0 important=0 minor=0 unneeded=0 strengths=0')
        self.assertIn('WARN: b (2026-11-05): task_review step 1 strengths are missing, more than three, or lack a '
                      'category or summary', self.health())

    def test_schema_5_label_and_strength_gaps_are_warned(self):
        unlabelled = [{'id': 'F1', 'severity': 'minor', 'summary': 's', 'outcome': 'deferred', 'category': 'nope',
                       'validity': 'valid'}]
        self.labelled_run([{'category': 'tests', 'summary': 's'}] * 4, 'Findings: critical=0 important=0 minor=1 strengths=2',
                          None, 'Findings: critical=0 important=0 minor=0 unneeded=0 strengths=0', findings=unlabelled)
        prefix = 'WARN: b (2026-11-05): '
        self.assertEqual(sorted(l for l in self.health() if 'strength' in l or 'categor' in l), sorted([
            prefix + 'task_review step 1 findings F1 have no category or validity',
            prefix + 'task_review step 1 strengths are missing, more than three, or lack a category or summary',
            prefix + "task_review step 1 has 4 strengths, its report's count line says 2",
            prefix + 'final_review step 1 strengths are missing, more than three, or lack a category or summary']))
        kinds = {w['kind']: w for w in self.json()['runs'][0]['warnings']}
        self.assertEqual(kinds['findings_unlabelled']['step'], {'step': 'task_review', 'task': None, 'round': 0, 'wave': 'A'})
        self.assertIn('strengths_unrecorded', kinds)

    def mismatched_run(self, loads=('subagent-driven-development',)):
        """A plan simplify with no Findings: line, a task review whose counts differ, a fix-check with no Verdicts:
        line and a launched final review with no step."""
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
        self.schema2_run([simplify, plan, task, fix], rows, loads=loads)

    def test_a_missing_step_a_count_mismatch_and_a_missing_line_are_warned(self):
        self.mismatched_run()
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

    def warned_run_and_session(self):
        """mismatched_run with no skill loaded, and a second session with no startup row."""
        self.mismatched_run(loads=())
        self.session([asst('x', ts='2026-11-05T11:00:00.000Z')], project='proj', sid='sess-2')

    def test_warnings_mirror_warns_in_every_list(self):
        self.warned_run_and_session()
        data = self.json()
        session = next(s for s in data['sessions'] if any(w['kind'] == 'startup_missing' for w in s['warnings']))
        run, health = data['runs'][0], data['totals']['health']
        for unit in (run, session, health):
            self.assertEqual([w['text'] for w in unit['warnings']], unit['warns'])
            self.assertTrue(all(isinstance(w['kind'], str) and w['kind'] for w in unit['warnings']))
        self.assertTrue(run['warnings'])
        self.assertTrue(all(w['run'] == {'date': '2026-11-05T12:00:00Z', 'project': '/p', 'branch': 'b'}
                            and w['session'] is None for w in run['warnings']))
        self.assertEqual([w['kind'] for w in session['warnings']], ['startup_missing'])
        self.assertEqual(session['warnings'][0]['session'], session['file'])
        self.assertIsNone(session['warnings'][0]['run'])
        self.assertEqual(sorted(health['warnings'][0]), ['agent', 'kind', 'run', 'session', 'skill', 'step', 'text'])
        self.assertEqual(len(health['warnings']), len(run['warnings']) + len(session['warnings']))

    def test_warning_kinds_name_their_step_skill_and_agent(self):
        self.warned_run_and_session()
        by_kind = {}
        for w in self.json()['runs'][0]['warnings']:
            by_kind.setdefault(w['kind'], w)
        self.assertEqual(by_kind['step_counts_differ']['step'],
                         {'step': 'task_review', 'task': 1, 'round': 0, 'wave': None})
        missing = by_kind['count_line_missing']
        self.assertEqual((missing['step']['step'], missing['agent']), ('plan_simplify', 'plan-simplifier'))
        self.assertEqual(by_kind['step_count_differs']['step'],
                         {'step': 'final_review', 'task': None, 'round': None, 'wave': None})
        required = by_kind['required_skill_missing']
        self.assertEqual((required['skill'], required['step'], required['agent']),
                         ('subagent-driven-development', None, None))

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
        self.assertEqual(self.kinds(), ['findings_dont_add_up'] * 4 + ['verdicts_dont_add_up'])

    def test_an_agent_off_its_pin_is_warned_unless_the_stage_allows_it(self):
        prefix = 'WARN: b (2026-11-05): '
        after = {'date': '2026-11-11T00:00:00Z'}
        cases = (
            # (record fields, dispatch input, subagent model and effort, expected)
            ({}, {}, ('claude-opus-5-5', 'high'), ['Health: no problems']),
            ({'risk_area': True}, {}, ('claude-opus-5-5', 'high'), ['Health: no problems']),
            ({'stage': 'out'}, {}, ('claude-opus-5-5', 'xhigh'), ['Health: no problems']),
            ({'stage': '3'}, {'model': 'fable'}, ('claude-fable-5-1', 'high'), ['Health: no problems']),
            ({'risk_area': True}, {'model': 'fable', 'effort': 'xhigh'}, ('claude-fable-5-1', 'xhigh'),
             ['Health: no problems']),
            ({'risk_area': True}, {}, ('claude-fable-5-1', 'xhigh'),
             [prefix + 'final-reviewer ran on claude-fable-5-1/xhigh, pinned opus/high: Final review']),
            ({}, {'model': 'fable', 'effort': 'xhigh'}, ('claude-fable-5-1', 'xhigh'),
             [prefix + 'final-reviewer ran on claude-fable-5-1/xhigh, pinned opus/high: Final review']),
            ({}, {}, ('claude-fable-5-1', 'high'), ['Health: no problems']),
            (after, {}, ('claude-fable-5-1', 'high'),
             ['WARN: b (2026-11-11): final-reviewer ran on claude-fable-5-1/high, pinned opus/high: Final review']),
            ({'date': '2026-11-10T23:00:00Z'}, {}, ('claude-fable-5-1', 'high'), ['Health: no problems']),
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
                args = ('--agents', self.pins(), '--now', '2026-11-11T12:00:00Z')
                self.assertEqual(self.health(*args), expected)
                self.assertEqual(self.kinds(*args), ['agent_off_pin'] if expected[0].startswith('WARN') else [])

    def test_a_risk_area_small_run_is_warned(self):
        self.final_run(path='small', risk_area=True)
        self.assertEqual(self.health(), ['WARN: b (2026-11-05): risk area on the Small path'])
        self.assertEqual(self.kinds(), ['risk_area_small_path'])

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
        self.assertEqual(self.kinds(), ['wave_unchecked'])

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
                self.assertEqual(self.kinds('--agents', self.pins(skills=True)),
                                 ['agent_skill_missing'] if expected[0].startswith('WARN') else [])

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
                self.assertEqual(self.kinds(), ['typed_skill_not_loaded'] if expected == warned else [])

    def test_a_forks_copied_typed_trigger_warns_once_and_not_when_the_fork_loads_the_skill(self):
        # A fork copies its parent's trigger row, so the row warns once, on the first transcript by path, and not when
        # either transcript loads the skill after it.
        for later, fork in (((), 'sess-0'), ((), 'sess-2'), (('grill-me',), 'sess-0'), (('grill-me',), 'sess-2')):
            with self.subTest(later=later, fork=fork):
                self.tearDown()
                self.setUp()
                rows = [self.startup(), user('grill me', ts=self.ts()), asst('shared', ts=self.ts())]
                self.session(rows, project='proj')
                self.session(rows + [self.loaded(name) for name in later] + [asst('own', ts=self.ts())],
                             project='proj', sid=fork)
                first = min(fork, 'sess-1')
                self.assertEqual(self.health(), ['Health: no problems'] if later else
                                 [f"WARN: session {first}.jsonl: typed 'grill-me' but grill-me never loaded"])

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
        # last week's session, by its rows, is not: the app rewrites old transcripts' metadata rows, so a file's time
        # says nothing about when it ran
        self.session([user('Hello', ts='2026-10-30T10:00:00.000Z'), {'type': 'cost-state', 'sessionId': 's'}],
                     project='proj', sid='sess-2')
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

    def test_the_startup_hooks_using_superpowers_block_counts_as_a_load(self):
        hook = startup_row(self.ts())
        hook['attachment']['stdout'] = ('<using-superpowers-session-start>rules</using-superpowers-session-start>\n'
                                        + STARTUP)
        self.session([hook, self.said(1000)], project='proj')
        self.assertEqual(self.json()['sessions'][0]['skills'],
                         [{'name': 'using-superpowers', 'time': hook['timestamp'], 'outcome': 'ran'}])

    def test_the_hooks_using_superpowers_block_alone_counts_as_a_load(self):
        hook = startup_row(self.ts())
        hook['attachment']['stdout'] = '<using-superpowers-session-start>rules</using-superpowers-session-start>\n'
        self.session([hook, self.said(1000)], project='proj')
        self.assertEqual([s['name'] for s in self.json()['sessions'][0]['skills']], ['using-superpowers'])

    def test_a_queued_prompt_quoting_the_startup_tags_is_not_a_load(self):
        queued = {'type': 'attachment', 'timestamp': self.ts(),
                  'attachment': {'type': 'queued_command', 'prompt': '<using-superpowers-session-start>' + STARTUP}}
        self.session([self.startup(), queued, self.said(1000)], project='proj')
        self.assertEqual(self.json()['sessions'][0]['skills'], [])

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
                    self.assertIn('offer_not_recorded', self.kinds())
                else:
                    self.assertEqual(warns, [])
                    self.assertNotIn('offer_not_recorded', self.kinds())

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
        self.assertIsNone(first['phases'])
        self.assertIsNone(first['context'])
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
                          'warnings': [{'text': 'WARN: old (2026-11-05): no session transcript found',
                                        'kind': 'no_transcript',
                                        'run': {'date': '2026-11-05T12:00:00Z', 'project': '/p', 'branch': 'old'},
                                        'session': None, 'step': None, 'skill': None, 'agent': None}],
                          'info': ['Git evidence unavailable for 2 runs']})

    def test_json_prices_date_suffixed_ids_and_leaves_unknown_models_unpriced(self):
        self.session([asst('h', model='claude-haiku-4-5-20251001', u=usage(None, 1000000, 0), ts=self.ts()),
                      asst('u', model='claude-unknown-1', ts=self.ts())], project='proj')
        data = self.json()
        self.assertEqual(data['runs'], [])
        file = os.path.join(self.projects, 'proj', 'sess-1.jsonl')
        self.assertEqual(data['sessions'], [{
            'file': file, 'start': '2026-11-05T10:00:01.000Z', 'end': '2026-11-05T10:00:02.000Z', 'project': None,
            'commands': [], 'skills': [], 'typed': [],
            'models': {'claude-haiku-4-5-20251001': {'input': 1000000, 'output': 0, 'cache_read': 0, 'cache_write': 0,
                                                     'cost': 1.0},
                       'claude-unknown-1': {'input': 10, 'output': 10, 'cache_read': 0, 'cache_write': 0,
                                            'cost': None}},
            'phases': [{'name': 'unattributed', 'advisor': {'calls': 0, 'cost': 0.0}, 'models': {
                'claude-haiku-4-5-20251001': {'input': 1000000, 'output': 0, 'cache_read': 0, 'cache_write': 0,
                                              'cost': 1.0,
                                              'cost_by_kind': {'input': 1.0, 'output': 0.0, 'cache_read': 0.0,
                                                               'cache_write': 0.0}},
                'claude-unknown-1': {'input': 10, 'output': 10, 'cache_read': 0, 'cache_write': 0, 'cost': None,
                                     'cost_by_kind': None}}}],
            'efforts': {}, 'startup': False,
            'advisor': {'calls': 0, 'cost': 0.0, 'tokens': {'input': 0, 'output': 0, 'cache_read': 0, 'cache_write': 0},
                        'by_role': {}, 'failed': {}, 'weeks': []},
            'notes': {'notes': 0, 'changed': 0, 'missing': 0}, 'refusals': {},
            'context': {'calls': 2, 'tokens': 1000010, 'median_tokens': 500005.0, 'max_tokens': 1000000,
                        'over_cap': True, 'calls_over_cap': 1, 'handoff': None},
            'tool_results': {'read': 0, 'edit': 0, 'run': 0, 'bash_other': 0, 'agent': 0, 'mcp': 0, 'other': 0},
            'sweeps': {'inline': 0, 'delegated': 0},
            'warns': ['WARN: session sess-1.jsonl: startup guidelines missing'],
            'warnings': [{'text': 'WARN: session sess-1.jsonl: startup guidelines missing', 'kind': 'startup_missing',
                          'run': None, 'session': file, 'step': None, 'skill': None, 'agent': None}],
            'title': None, 'key': key(file)}])
        self.assertEqual(data['totals']['scoreboard']['startup'], {'with': 0, 'without': 1})

    def test_json_phase_costs_split_by_kind_and_cache_lifetime(self):
        u = {'input_tokens': 1000000, 'output_tokens': 1000000, 'cache_read_input_tokens': 1000000,
             'cache_creation_input_tokens': 2000000,
             'cache_creation': {'ephemeral_5m_input_tokens': 1000000, 'ephemeral_1h_input_tokens': 1000000}}
        self.session([asst('m', model='claude-fable-5-1', u=u, ts=self.ts())], project='proj')
        session = self.json()['sessions'][0]
        [phase] = session['phases']
        entry = phase['models']['claude-fable-5-1']
        self.assertEqual(phase['name'], 'unattributed')
        for kind, dollars in {'input': 10.0, 'output': 50.0, 'cache_read': 0.25, 'cache_write': 32.5}.items():
            self.assertAlmostEqual(entry['cost_by_kind'][kind], dollars)
        self.assertAlmostEqual(entry['cost'], 92.75)
        self.assertAlmostEqual(session['models']['claude-fable-5-1']['cost'], 92.75)

    def test_json_phases_place_a_full_run(self):
        rows = [self.startup(), self.loaded('subagent-driven-development'), self.said(1000)]
        rows += self.reviewed('im1', 'ok', u=usage(None, 20000, 0), subagent_type='implementer',
                              description='Implement Task 1')
        rows += self.reviewed('tr1', 'ok', u=usage(None, 30000, 0), subagent_type='task-reviewer',
                              description='Review Task 1')
        rows += self.reviewed('fc1', 'ok', u=usage(None, 40000, 0), subagent_type='fix-checker',
                              description='Fix-check Task 1')
        rows += self.reviewed('ex1', 'ok', u=usage(None, 50000, 0), subagent_type='Explore',
                              description='Find the callers')
        rows += [self.simplify(), asst('adv', iterations=[executor(2000, 0), ADVISOR], ts=self.ts())]
        rows += self.agent('fx', error=NOT_FOUND, subagent_type='final-reviewer', description='Final review')
        rows += self.reviewed('sa1', 'ok', u=usage(None, 60000, 0), subagent_type='general-purpose', model='opus',
                              description='Reuse pass')
        rows += self.reviewed('fr', 'ok', u=usage(None, 70000, 0), subagent_type='final-reviewer',
                              description='Final review')
        rows += [self.said(3000)]
        rows += self.reviewed('im2', 'ok', u=usage(None, 80000, 0), subagent_type='implementer',
                              description='Fix the final findings')
        rows += self.reviewed('fc2', 'ok', u=usage(None, 90000, 0), subagent_type='fix-checker',
                              description='Fix-check final findings')
        rows.append(self.record_write('c0ffee01', started='2026-11-05T10:00:00Z'))
        self.session(rows, project='proj')
        self.records([run_record(head='c0ffee01', started='2026-11-05T10:00:00Z')])
        run = self.json()['runs'][0]
        self.assertEqual(phase_inputs(run), {
            'implement': 100000, 'task_review': 30000, 'fix_check': 40000,
            'simplify': 62030,  # the simplify call, the advisor's message, the errored and the launched dispatch's
            'final_review': 70000, 'final_fix_check': 90000,
            'coordination': 54080})  # 1000 + 3000 + Explore's 50000 + seven launches and the record write
        simplify = next(p for p in run['phases'] if p['name'] == 'simplify')
        self.assertEqual(simplify['advisor']['calls'], 1)
        self.assertAlmostEqual(simplify['advisor']['cost'], 0.78)
        final = next(p for p in run['phases'] if p['name'] == 'final_review')['models']['claude-opus-5-5']
        self.assertAlmostEqual(final['cost_by_kind']['input'], 0.28)

    def test_json_phases_place_a_plan_run(self):
        rows = [self.startup(), self.said(1000), self.loaded('brainstorming')]
        rows += self.reviewed('ex', 'ok', u=usage(None, 2000, 0), subagent_type='Explore', description='Read the code')
        rows += [self.said(3000), self.loaded('writing-plans'), self.said(4000)]
        rows += self.reviewed('ps', 'ok', u=usage(None, 5000, 0), subagent_type='plan-simplifier',
                              description='Plan simplify')
        rows += self.reviewed('pr', 'ok', u=usage(None, 6000, 0), subagent_type='plan-reviewer',
                              description='Plan review')
        rows.append(self.record_write('c0ffee01', started='2026-11-05T10:00:00Z'))
        self.session(rows, project='proj')
        self.records([plan_record(started='2026-11-05T10:00:00Z')])
        self.assertEqual(phase_inputs(self.json()['runs'][0]),
                         {'design': 6010, 'plan_writing': 4030, 'plan_simplify': 5000, 'plan_review': 6000})

    def test_json_phases_follow_the_loads_of_a_full_run_that_designed_itself(self):
        rows = [self.startup(), self.loaded('brainstorming'), self.said(1000), self.loaded('writing-plans'),
                self.said(2000), self.loaded('subagent-driven-development'), self.said(3000),
                self.record_write('c0ffee01', started='2026-11-05T10:00:00Z')]
        self.session(rows, project='proj')
        self.records([run_record(head='c0ffee01', started='2026-11-05T10:00:00Z')])
        self.assertEqual(phase_inputs(self.json()['runs'][0]),
                         {'design': 1000, 'plan_writing': 2000, 'coordination': 3010})

    def test_json_phases_place_a_small_run(self):
        rows = [self.startup(), self.said(1000), self.loaded('brainstorming'), self.said(2000),
                self.loaded('test-driven-development'), self.said(3000), self.simplify(), self.said(4000)]
        rows += self.reviewed('fr', 'ok', u=usage(None, 5000, 0), subagent_type='final-reviewer',
                              description='Final review')
        rows += [self.said(6000)]
        rows += self.reviewed('fc', 'ok', u=usage(None, 7000, 0), subagent_type='fix-checker',
                              description='Fix-check final findings')
        rows.append(self.record_write('c0ffee01', started='2026-11-05T10:00:00Z'))
        self.session(rows, project='proj')
        self.records([run_record(path='small', head='c0ffee01', started='2026-11-05T10:00:00Z')])
        self.assertEqual(phase_inputs(self.json()['runs'][0]), {
            'design': 2000, 'implement': 10030, 'simplify': 4010, 'final_review': 5000, 'final_fix_check': 7000})

    def test_json_phases_place_a_session_with_no_run(self):
        no_time = asst('no-time', u=usage(None, 8000, 0))
        del no_time['timestamp']
        rows = [self.said(1000), self.loaded('brainstorming'),
                asst('adv', iterations=[executor(100, 0), ADVISOR], ts=self.ts())]
        rows += self.reviewed('ex', 'ok', u=usage(None, 3000, 0), subagent_type='Explore', description='Read the code')
        rows += [self.said(2000), self.loaded('writing-plans'), self.said(4000),
                 asst('u', model='claude-unknown-1', u=usage(None, 9000, 0), ts=self.ts()),
                 self.loaded('subagent-driven-development')]
        rows += self.reviewed('im', 'ok', u=usage(None, 5000, 0), subagent_type='general-purpose', model='opus',
                              description='Implement Task 1')
        rows += [self.said(6000), no_time]
        self.session(rows, project='proj')
        # a subagent transcript no dispatch of the session launched
        self.subagent('general-purpose', [asst('wf', u=usage(None, 7000, 0), ts=self.ts())], project='proj',
                      aid='wf', tid='nowhere')
        session = self.json()['sessions'][0]
        self.assertEqual(phase_inputs(session), {'design': 5110, 'plan_writing': 13000, 'implement': 5000,
                                                 'coordination': 6010, 'unattributed': 16000})
        self.assertEqual(session['advisor']['calls'], 1)
        self.assertAlmostEqual(session['advisor']['cost'], 0.78)
        writing = next(p for p in session['phases'] if p['name'] == 'plan_writing')
        self.assertEqual(writing['models']['claude-unknown-1'], {'input': 9000, 'output': 0, 'cache_read': 0,
                                                                 'cache_write': 0, 'cost': None,
                                                                 'cost_by_kind': None})

    def test_json_phases_start_at_the_window_and_simplify_runs_to_its_end(self):
        rows = [self.startup(), self.loaded('writing-plans'), self.said(1000)]  # before the run's start
        started = '2026-11-05T10:00:03.500Z'
        rows += [self.said(2000), user('<command-name>/simplify</command-name>', ts=self.ts()), self.said(3000),
                 self.record_write('c0ffee01', started=started)]
        self.assertGreater(rows[3]['timestamp'], started)
        self.session(rows, project='proj')
        self.records([run_record(path='small', head='c0ffee01', started=started)])
        self.assertEqual(phase_inputs(self.json()['runs'][0]), {'implement': 2000, 'simplify': 3010})

    def test_json_a_runs_simplify_span_ends_with_its_window_in_its_sessions_rows(self):
        rows = [self.startup(), self.loaded('writing-plans'), self.said(1000)]  # before the run's start
        started = '2026-11-05T10:00:03.500Z'
        rows += [self.said(2000), user('<command-name>/simplify</command-name>', ts=self.ts()), self.said(3000),
                 self.record_write('c0ffee01', started=started), self.said(4000)]  # the last after the run's end
        self.session(rows, project='proj')
        self.records([run_record(path='small', head='c0ffee01', started=started)])
        data = self.json()
        self.assertEqual(phase_inputs(data['runs'][0]), {'implement': 2000, 'simplify': 3010})
        self.assertEqual(phase_inputs(data['sessions'][0]), {'plan_writing': 5000})

    def test_json_phases_of_an_unknown_path_start_unattributed(self):
        self.session([self.startup(), self.said(1000), self.record_write('c0ffee01', started='2026-11-05T10:00:00Z')],
                     project='proj')
        self.records([run_record(path='medium', head='c0ffee01', started='2026-11-05T10:00:00Z')])
        self.assertEqual(phase_inputs(self.json()['runs'][0]), {'unattributed': 1010})

    def test_json_a_split_message_takes_the_phase_of_its_fullest_row(self):
        self.session([asst('same', u=usage(None, 500, 1), ts=self.ts()), self.loaded('brainstorming'),
                      asst('same', u=usage(None, 500, 2000), ts=self.ts())], project='proj')
        [phase] = self.json()['sessions'][0]['phases']
        self.assertEqual((phase['name'], phase['models']['claude-opus-5-5']['output']), ('design', 2000))

    def test_json_units_carry_context_per_main_call(self):
        full = {'input_tokens': 100, 'cache_read_input_tokens': 199_900, 'cache_creation_input_tokens': 0,
                'output_tokens': 10}
        self.session([asst('m1', u=full, ts='2026-11-05T10:00:01.000Z'),
                      asst('m2', u={'input_tokens': 50, 'output_tokens': 5}, ts='2026-11-05T10:00:02.000Z')],
                     project='proj')
        self.subagent('implementer', [asst('s1', u=full, ts='2026-11-05T10:00:03.000Z')], project='proj')
        [session] = self.json()['sessions']
        self.assertEqual(session['context'], {'calls': 2, 'tokens': 200_050, 'median_tokens': 100_025.0,
                                              'max_tokens': 200_000, 'over_cap': False, 'calls_over_cap': 0,
                                              'handoff': None})

    def test_json_context_counts_calls_past_the_cap_until_a_handoff_or_compaction(self):
        rows = [asst('m1', u=OVER_CAP, ts='2026-11-05T10:00:01.000Z'), asst('m2', u=OVER_CAP, ts='2026-11-05T10:00:02.000Z'),
                asst('m3', [tool_use('h1', 'mcp__ccd_session__start_session',
                                     {'prompt': 'Handoff 1: go', 'background': 'x' * 80})],
                     u=OVER_CAP, ts='2026-11-05T10:00:03.000Z'),
                user([{'type': 'tool_result', 'tool_use_id': 'h1', 'content': '(session_id: local_cccc, name: n)'}],
                     ts='2026-11-05T10:00:04.000Z'),
                asst('m4', u=OVER_CAP, ts='2026-11-05T10:00:05.000Z')]
        self.session(rows, sid='aaaa')
        self.session([asst('c1', u=OVER_CAP, ts='2026-11-05T11:00:01.000Z'),
                      user('summary', ts='2026-11-05T11:00:02.000Z', isCompactSummary=True),
                      asst('c2', u=OVER_CAP, ts='2026-11-05T11:00:03.000Z')], sid='dddd')
        by_file = {os.path.basename(s['file']): s['context'] for s in self.json()['sessions']}
        # m3 is the call at the handoff's time, so m2 and m3 are past the cap; m4 after the handoff is not
        self.assertEqual((by_file['aaaa.jsonl']['calls_over_cap'], by_file['aaaa.jsonl']['handoff']),
                         (2, '2026-11-05T10:00:03.000Z'))
        self.assertEqual((by_file['dddd.jsonl']['calls_over_cap'], by_file['dddd.jsonl']['handoff']), (0, None))

    def test_json_context_takes_a_handoff_made_by_the_first_call_over_the_cap(self):
        self.session([asst('m1', ts='2026-11-05T10:00:01.000Z'),
                      asst('m2', [tool_use('h1', 'mcp__ccd_session__start_session',
                                           {'prompt': 'Handoff 1: go', 'background': 'x' * 80})],
                           u=OVER_CAP, ts='2026-11-05T10:00:02.000Z'),
                      user([{'type': 'tool_result', 'tool_use_id': 'h1',
                             'content': '(session_id: local_cccc, name: n)'}], ts='2026-11-05T10:00:03.000Z'),
                      asst('m3', u=OVER_CAP, ts='2026-11-05T10:00:04.000Z')])
        [session] = self.json()['sessions']
        self.assertEqual((session['context']['calls_over_cap'], session['context']['handoff']),
                         (0, '2026-11-05T10:00:02.000Z'))

    def test_json_context_ignores_a_subagents_compaction(self):
        sub = [asst('sub', ts='2026-11-05T09:30:03.000Z'),
               user('summary', ts='2026-11-05T09:30:04.000Z', isCompactSummary=True)]

        def main(p):
            return [asst(p + '1', u=OVER_CAP, ts='2026-11-05T09:30:01.000Z'),
                    asst(p + '2', u=OVER_CAP, ts='2026-11-05T09:30:02.000Z'),
                    asst(p + '3', u=OVER_CAP, ts='2026-11-05T09:30:05.000Z')]
        # the same transcripts once as a session and once in a run's window, with their own message ids
        self.session(main('p'), project='proj')
        self.subagent('implementer', sub, project='proj')
        self.session(main('r') + [self.record_write('c0ffee01', started='2026-11-05T09:00:00Z')], project='run')
        self.subagent('implementer', sub, project='run')
        self.records([run_record(head='c0ffee01', started='2026-11-05T09:00:00Z')])
        data = self.json()
        [run] = data['runs']
        session = next(s for s in data['sessions'] if s['file'] == os.path.join(self.projects, 'proj', 'sess-1.jsonl'))
        # the run's record-writing row is one more call past the cap
        self.assertEqual((run['context']['calls_over_cap'], session['context']['calls_over_cap']), (3, 2))

    def test_json_units_carry_tool_result_chars_by_class(self):
        def call(tid, name, inp, ts):
            return asst(tid, [tool_use(tid, name, inp)], ts=ts)

        def result(tid, content, ts):
            return user([{'type': 'tool_result', 'tool_use_id': tid, 'content': content}], ts=ts)
        rows = [call('r1', 'Read', {'file_path': '/a'}, '2026-11-05T10:00:01.000Z'),
                result('r1', 'seven c', '2026-11-05T10:00:02.000Z'),
                call('b1', 'Bash', {'command': 'cd /x && cat y'}, '2026-11-05T10:00:03.000Z'),
                result('b1', [{'type': 'text', 'text': 'abc'}], '2026-11-05T10:00:04.000Z'),
                call('b2', 'Bash', {'command': 'git log -1'}, '2026-11-05T10:00:05.000Z'),
                result('b2', 'xx', '2026-11-05T10:00:06.000Z'),
                call('b5', 'Bash', {'command': 'cd /x && grep -n a f'}, '2026-11-05T10:00:06.100Z'),
                result('b5', 'ggg', '2026-11-05T10:00:06.200Z'),
                call('b6', 'Bash', {'command': 'git log'}, '2026-11-05T10:00:06.300Z'),
                result('b6', 'hhhhh', '2026-11-05T10:00:06.400Z'),
                call('b7', 'Bash', {'command': 'cat /a'}, '2026-11-05T10:00:06.500Z'),
                result('b7', 'iiiiiiii', '2026-11-05T10:00:06.600Z'),
                call('b3', 'Bash', {'command': 'git commit -m x'}, '2026-11-05T10:00:07.000Z'),
                result('b3', 'y', '2026-11-05T10:00:08.000Z'),
                call('b4', 'Bash', {'command': '/usr/bin/python3 -I t.py'}, '2026-11-05T10:00:09.000Z'),
                result('b4', 'zzzz', '2026-11-05T10:00:10.000Z'),
                call('w1', 'mcp__ccd_turn__wrap_up', {'recap': 'r'}, '2026-11-05T10:00:11.000Z'),
                result('w1', 'ok', '2026-11-05T10:00:12.000Z'),
                result('nope', 'orphan', '2026-11-05T10:00:13.000Z')]
        self.session(rows, project='proj')
        [session] = self.json()['sessions']
        # read is the Read, the cat after cd, the prefixed git log, grep after cd and a prefix, the git log after a
        # longer prefix and the prefixed read; other is the orphan result
        self.assertEqual(session['tool_results'],
                         {'read': 28, 'bash_other': 1, 'run': 4, 'agent': 0, 'edit': 0, 'mcp': 2, 'other': 6})

    def test_json_sweeps_count_inline_runs_of_reads_and_delegated_lookups(self):
        def read(i):
            return asst(f'r{i}', [tool_use(f'r{i}', 'Read', {'file_path': f'/f{i}'})],
                        ts=f'2026-11-05T10:00:{i:02d}.000Z')
        edit_read = asst('e1', [tool_use('e1', 'Edit', {'file_path': '/f2'})], ts='2026-11-05T10:00:20.000Z')
        edit_other = asst('e2', [tool_use('e2', 'Edit', {'file_path': '/other'})], ts='2026-11-05T10:00:40.000Z')
        sweep = asst('a1', [tool_use('a1', 'Agent', {'subagent_type': 'sweeper', 'prompt': 'find x'})],
                     ts='2026-11-05T10:00:50.000Z')
        launched = user([{'type': 'tool_result', 'tool_use_id': 'a1', 'content': 'found'}], ts='2026-11-05T10:00:50.500Z')
        notebook = [asst(f'n{i}', [tool_use(f'n{i}', 'Read', {'file_path': '/n.ipynb'})],
                         ts=f'2026-11-05T10:00:{50 + i:02d}.000Z') for i in range(1, 6)]
        notebook_edit = asst('ne', [tool_use('ne', 'NotebookEdit', {'notebook_path': '/n.ipynb'})],
                             ts='2026-11-05T10:00:56.000Z')
        # five reads then an edit of one of them, five reads then an edit of another file, four reads then a sweeper,
        # five reads of a notebook then an edit of it
        self.session([read(i) for i in range(1, 6)] + [edit_read] + [read(i) for i in range(21, 26)] + [edit_other]
                     + [read(i) for i in range(41, 45)] + [sweep, launched] + notebook + [notebook_edit], project='proj')
        [session] = self.json()['sessions']
        self.assertEqual(session['sweeps'], {'inline': 1, 'delegated': 1})

    def test_json_a_sweeper_that_never_launched_is_no_delegated_sweep(self):
        rows = self.agent('sw1', error="Agent type 'sweeper' not found. Available agents: general-purpose",
                          subagent_type='sweeper', description='Sweep')
        rows += self.agent('sw2', subagent_type='sweeper', description='Sweep')
        self.session(rows, project='proj')
        [session] = self.json()['sessions']
        self.assertEqual(session['sweeps'], {'inline': 0, 'delegated': 1})

    def test_json_context_takes_a_messages_largest_iteration(self):
        # an advisor consult mid-message: two executor samplings over the same prefix, each under the cap
        self.session([asst('m1', iterations=[executor(200_000, 1), ADVISOR, executor(250_000, 1)])], project='proj')
        context = self.json()['sessions'][0]['context']
        self.assertEqual((context['tokens'], context['max_tokens'], context['over_cap']), (250_000, 250_000, False))

    def copied_transcript(self):
        """A transcript and its copy, each with five reads and their results, then 52 calls past the cap; the copy goes
        on with one call of its own."""
        rows = []
        for i in range(1, 6):
            rows += [asst(f'r{i}', [tool_use(f'r{i}', 'Read', {'file_path': f'/f{i}'})], ts=f'2026-11-05T10:00:{i:02d}.000Z'),
                     user([{'type': 'tool_result', 'tool_use_id': f'r{i}', 'content': 'abcd'}],
                          ts=f'2026-11-05T10:00:{i:02d}.500Z')]
        rows += [asst(f'p{i}', u=OVER_CAP, ts=f'2026-11-05T11:{i // 60:02d}:{i % 60:02d}.000Z') for i in range(52)]
        self.session(rows, sid='a-orig')
        self.session(rows + [asst('fork', ts='2026-11-05T12:00:00.000Z')], sid='b-copy')

    def test_json_a_copied_transcript_counts_its_tool_results_and_sweeps_once(self):
        self.copied_transcript()
        [week] = self.json()['totals']['context']
        self.assertEqual((week['calls'], week['tool_results']['read'], week['sweeps']['inline']), (58, 20, 1))

    def test_health_counts_a_copied_transcript_once(self):
        self.copied_transcript()
        out = self.run_report('--health', '--now', '2026-11-06T00:00:00Z')
        self.assertEqual(out.splitlines()[-2:], ['inline sweeps: 1', 'over the cap without a handoff: 1 transcripts'])

    def test_totals_context_rows_sum_units_by_week(self):
        small = {'input_tokens': 100, 'output_tokens': 1}
        self.session([asst('a', u=small, ts='2026-11-05T10:00:01.000Z'),
                      asst('b', u=OVER_CAP, ts='2026-11-05T10:00:02.000Z')], sid='w1')
        self.session([asst('c', u=small, ts='2026-11-09T10:00:01.000Z')], sid='w2')
        rows = self.json('--now', '2026-11-10T00:00:00Z')['totals']['context']  # the default NOW, 2026-11-07, would drop w2
        self.assertEqual([(r['week'], r['calls'], r['tokens'], r['max_tokens'], r['units_over_cap']) for r in rows],
                         [('2026-11-01', 2, 300_101, 300_001, 1), ('2026-11-08', 1, 100, 100, 0)])
        self.assertEqual(rows[0]['sweeps'], {'inline': 0, 'delegated': 0})

    def test_html_shows_the_context_totals(self):
        self.session([asst('a', u={'input_tokens': 100, 'output_tokens': 1}, ts='2026-10-30T10:00:01.000Z')], sid='w1')
        self.session([asst('b', u=OVER_CAP, ts='2026-11-05T10:00:02.000Z')], sid='w2')
        html = self.html()
        for fact, value in (('context calls', 2), ('context tokens', 300_101), ('context max tokens', 300_001),
                            ('units over the cap', 1), ('tool-result characters: read', 0), ('inline sweeps', 0)):
            self.assertIn(f'<tr><td>{fact}</td><td>{value}</td></tr>', html)

    def test_health_information_lines_count_sweeps_and_transcripts_over_the_cap(self):
        reads = [asst(f'r{i}', [tool_use(f'r{i}', 'Read', {'file_path': f'/f{i}'})], ts=f'2026-11-05T10:00:{i:02d}.000Z')
                 for i in range(1, 6)]
        past = [asst(f'p{i}', u=OVER_CAP, ts=f'2026-11-05T11:{i // 60:02d}:{i % 60:02d}.000Z') for i in range(52)]
        self.session(reads + past, sid='busy')
        out = self.run_report('--health', '--now', '2026-11-06T00:00:00Z')
        self.assertEqual(out.splitlines()[-2:], ['inline sweeps: 1', 'over the cap without a handoff: 1 transcripts'])

    def test_health_and_brief_default_to_the_last_seven_days(self):
        self.records([run_record(branch='old-branch', date='2026-10-20T12:00:00Z',
                                 simplify={'ran': 'no', 'applied': 0, 'skipped': 0}),
                      run_record(branch='new-branch', date='2026-11-05T12:00:00Z',
                                 simplify={'ran': 'no', 'applied': 0, 'skipped': 0})])
        now = '2026-11-06T00:00:00Z'
        self.assertNotIn('old-branch', self.run_report('--health', '--now', now))
        self.assertIn('new-branch', self.run_report('--health', '--now', now))
        self.assertIn('old-branch', self.run_report('--health', '--now', now, '--since', '2026-10-01'))
        self.assertIn('old-branch', json.dumps(self.json('--now', now)))
        self.assertIn('Trial: runs 2/5', self.run_report('--brief', '--now', now))  # the trial line still counts every record

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
            {'name': 'brainstorming', 'time': brainstorming['timestamp'], 'outcome': '/r/docs/superpowers/specs/x.md',
             'step': None},
            {'name': 'writing-plans', 'time': writing_plans['timestamp'], 'outcome': None, 'step': None}])
        self.assertEqual(full['skills'], [
            {'name': 'subagent-driven-development', 'time': sdd['timestamp'], 'outcome': True, 'step': None},
            {'name': 'test-driven-development', 'time': tdd['timestamp'], 'outcome': [{'task': 1, 'result': 'passed'}],
             'step': None},
            {'name': 'simplify', 'time': simplify['timestamp'], 'outcome': {'applied': 2, 'skipped': 1}, 'step': None}])
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

    def cache_fixture(self):
        """A covered run with a reviewed final review, and a second session with an advisor iteration, a hook decision
        and a typed trigger."""
        rows = self.reviewed('fr', 'Findings: critical=0 important=0 minor=0 unneeded=0',
                             sub_model='claude-fable-5-1', sub_effort='high', subagent_type='final-reviewer',
                             description='Final review')
        self.schema2_run([SIMPLIFY_STEP, {'step': 'final_review', 'counts': {}, 'findings': []}], rows)
        self.session([asst('adv', iterations=[executor(), ADVISOR], ts=self.ts()),
                      hook_row('guard-main-commit.py', decision('ask', 'commit on main'), ts=self.ts()),
                      user('grill me', ts=self.ts())], project='proj', sid='sess-2')

    def outputs(self, *cache):
        data = self.json(*cache)
        data.pop('generated')
        return data, self.run_report('--health', *cache), self.run_report('--brief', *cache)

    def test_the_cache_never_changes_the_report(self):
        self.cache_fixture()
        plain = self.outputs('--no-cache')
        self.assertFalse(os.path.exists(os.path.join(self.root, 'cache')))
        self.assertEqual(self.outputs(), plain)  # cold
        self.assertEqual(self.outputs(), plain)  # warm
        folder = os.path.join(self.root, 'cache')
        self.assertEqual(os.stat(folder).st_mode & 0o777, 0o700)
        self.assertTrue(os.listdir(os.path.join(folder, 'transcripts')))

    def test_a_grown_transcript_is_read_again(self):
        self.cache_fixture()
        self.json()
        self.write_lines(os.path.join(self.projects, 'proj', 'sess-2.jsonl'),
                         [asst('late', model='claude-sonnet-5-5', ts=self.ts())])
        session = next(s for s in self.json()['sessions'] if s['file'].endswith('sess-2.jsonl'))
        self.assertIn('claude-sonnet-5-5', session['models'])

    def test_a_corrupt_or_old_cache_entry_is_read_again(self):
        self.cache_fixture()
        plain = self.outputs('--no-cache')
        self.outputs()
        entries = glob.glob(os.path.join(self.root, 'cache', 'transcripts', '*.json'))
        for i, entry in enumerate(entries):
            with open(entry) as f:
                stored = json.load(f)
            with open(entry, 'w') as f:
                f.write('not json' if i % 2 else json.dumps(dict(stored, version=0, value=None)))
        self.assertEqual(self.outputs(), plain)
        for entry in entries:
            with open(entry) as f:
                self.assertNotEqual(json.load(f)['version'], 0)

    def test_an_unusable_cache_folder_changes_nothing(self):
        self.cache_fixture()
        blocked = os.path.join(self.root, 'a file')
        with open(blocked, 'w') as f:
            f.write('x')
        self.assertEqual(self.outputs('--cache', blocked), self.outputs('--no-cache'))

    def test_a_deeply_nested_row_reads_the_same_from_the_cache(self):
        self.cache_fixture()
        # Rows nested near the recursion limit: some parse, yet sit too deep once wrapped in a cache entry.
        rows = [json.dumps(asst(f'deep{d}', ts=self.ts(), u=dict(usage(), x='DEEP')))
                .replace('"DEEP"', '[' * d + ']' * d) for d in range(950, 1000)]
        self.session(rows, project='proj', sid='sess-2')
        plain = self.outputs('--no-cache')
        self.assertEqual(self.outputs(), plain)  # cold
        self.assertEqual(self.outputs(), plain)  # warm

    def partition_fixture(self):
        """One transcript with a run window between rows outside it, and one with no run; each holds an advisor
        iteration, a typed trigger and a refusal, one refusal inside a subagent transcript."""
        refusal = 'No simplify since the last final review in this session\'s transcript.'
        early = [self.startup(), asst('early', model='claude-haiku-4-5', iterations=[executor(), ADVISOR], ts=self.ts())]
        early += self.agent('ref1', error=refusal, subagent_type='final-reviewer', description='Final review')
        self.clock = 30
        inside = [asst('in', iterations=[executor(), ADVISOR], ts=self.ts()), user('grill me', ts=self.ts())]
        inside.append(self.record_write('c0ffee01', started='2026-11-05T10:00:20Z'))
        after = [asst('after', model='claude-sonnet-5-5', ts=self.ts())]
        self.session(early + inside + after, project='proj')
        self.subagent('implementer', [asst('nested', ts=self.ts())] + self.agent('ref2', error=refusal,
                      subagent_type='final-reviewer', description='Final review'), project='proj', aid='i1', tid='ti1')
        self.session([asst('other', iterations=[executor(), ADVISOR], ts=self.ts()), user('grill me', ts=self.ts())],
                     project='proj', sid='sess-2')
        self.records([run_record(head='c0ffee01', schema=2, started='2026-11-05T10:00:20Z', steps=[])])
        return self.json()

    def units(self, data):
        return [u for u in data['runs'] + data['sessions'] if u['advisor'] is not None]

    def test_rows_outside_run_windows_form_the_transcripts_session(self):
        data = self.partition_fixture()
        run = data['runs'][0]
        covered = next(s for s in data['sessions'] if s['file'] == run['window']['transcript'])
        self.assertEqual(set(run['models']), {'claude-opus-5-5'})  # the advisor's executor row and the record write
        self.assertTrue({'claude-haiku-4-5', 'claude-sonnet-5-5'} <= set(covered['models']))
        self.assertEqual([t['skill'] for t in run['typed']], ['grill-me'])
        self.assertEqual(covered['typed'], [])
        self.assertEqual(run['refusals'], {})
        self.assertEqual(covered['refusals'], {'no_simplify': 2})  # one in the main transcript, one in its subagent

    def test_a_session_holding_a_run_keeps_its_whole_transcripts_facts(self):
        data = self.partition_fixture()
        run = data['runs'][0]
        covered = next(s for s in data['sessions'] if s['file'] == run['window']['transcript'])
        self.assertLess(covered['start'], run['window']['start'])
        self.assertGreater(covered['end'], run['window']['end'])
        self.assertFalse({'start', 'end', 'project', 'commands'} & set(run))

    def test_commands_typed_inside_a_run_window_stay_on_its_session(self):
        rows = [self.startup(), user('<command-name>/demo-skill</command-name>', ts=self.ts()),
                self.record_write('c0ffee01', started='2026-11-05T10:00:00Z'), asst('after', ts=self.ts())]
        self.session(rows, project='proj')
        self.records([run_record(head='c0ffee01', schema=2, started='2026-11-05T10:00:00Z', steps=[])])
        data = self.json()
        self.assertEqual(data['sessions'][0]['commands'], [{'name': 'demo-skill', 'time': rows[1]['timestamp']}])
        self.assertLess(data['runs'][0]['window']['start'], rows[1]['timestamp'])

    def test_the_advisor_totals_are_the_sums_over_the_units(self):
        data = self.partition_fixture()
        totals, units = data['totals']['advisor'], self.units(data)
        self.assertEqual(totals['calls'], sum(u['advisor']['calls'] for u in units))
        self.assertEqual(totals['calls'], 3)
        self.assertEqual(totals['cost'], sum(u['advisor']['cost'] for u in units))
        self.assertEqual(totals['missing'], sum(u['notes']['missing'] for u in units))
        self.assertEqual([w['advisor'] for w in totals['weeks']],
                         [sum(x['advisor'] for u in units for x in u['advisor']['weeks'] if x['week'] == w['week'])
                          for w in totals['weeks']])

    def test_the_rule_totals_are_the_sums_over_the_units(self):
        data = self.partition_fixture()
        board, units = data['totals']['scoreboard'], self.units(data)
        self.assertEqual(board['dispatch_guard'], {'no_simplify': 2})
        self.assertEqual(board['startup'], {'with': sum(s['startup'] for s in data['sessions']),
                                            'without': sum(not s['startup'] for s in data['sessions'])})
        self.assertEqual(data['totals']['skills']['grill-me']['missing'],
                         sum(not t['met'] for u in units for t in u['typed'] if t['skill'] == 'grill-me'))
        self.assertEqual(data['totals']['skills']['grill-me']['missing'], 2)

    def test_every_warning_belongs_to_one_unit(self):
        data = self.partition_fixture()
        owned = [w for u in data['runs'] + data['sessions'] for w in u['warns']]
        self.assertEqual(sorted(owned), sorted(data['totals']['health']['warns']))
        unavailable = sum(r['git_evidence'] is False for r in data['runs'])
        self.assertIn(f'Git evidence unavailable for {unavailable} runs', data['totals']['health']['info'])

    def test_overlapping_windows_and_shared_messages_count_once(self):
        rows = [self.startup(), asst('dup', ts=self.ts()), self.record_write('aaaa1111', started='2026-11-05T10:00:00Z')]
        self.session(rows, project='proj')
        self.session([asst('dup', ts=self.ts())], project='proj', sid='sess-2')
        self.records([run_record(head='aaaa1111', schema=2, started='2026-11-05T10:00:00Z', steps=[]),
                      run_record(head='aaaa1111', branch='twin', schema=2, started='2026-11-05T10:00:00Z', steps=[])])
        data = self.json()
        first, twin = data['runs']
        self.assertEqual(first['models']['claude-opus-5-5']['output'], 20)  # the shared message and the record write
        self.assertEqual(twin['models'], {})
        other = next(s for s in data['sessions'] if s['file'].endswith('sess-2.jsonl'))
        self.assertEqual(other['models'], {})  # its copy of the shared message counted in the run
        self.assertEqual(sum(m['output'] for u in self.units(data) for m in u['models'].values()), 20)

    def test_a_forked_sessions_copied_events_count_once(self):
        # A fork copies its parent's rows, timestamps included; the parent's session is read first and keeps them.
        shared = [self.startup(), self.loaded('systematic-debugging'), user('grill me', ts=self.ts()),
                  user('<command-name>/demo-skill</command-name>', ts=self.ts()), asst('shared', ts=self.ts())]
        self.session(shared, project='proj')
        self.session(shared + [self.loaded('grill-me'), asst('own', ts=self.ts())], project='proj', sid='sess-2')
        plain = self.outputs('--no-cache')
        data = plain[0]
        parent, fork = (next(s for s in data['sessions'] if s['file'].endswith(name))
                        for name in ('sess-1.jsonl', 'sess-2.jsonl'))
        self.assertEqual([s['name'] for s in parent['skills']], ['systematic-debugging'])
        self.assertEqual([s['name'] for s in fork['skills']], ['grill-me'])
        # The parent's copy of the trigger is met by the fork's later load: the row was typed once.
        self.assertEqual([(t['skill'], t['met']) for t in parent['typed']], [('grill-me', True)])
        self.assertEqual(fork['typed'], [])
        self.assertEqual([c['name'] for c in parent['commands']], ['demo-skill'])
        self.assertEqual(fork['commands'], [])
        self.assertEqual({name: (row['runs'], row['missing']) for name, row in data['totals']['skills'].items()},
                         {'systematic-debugging': (1, 0), 'grill-me': (1, 0)})
        self.assertEqual(self.outputs(), plain)

    def test_a_forked_sessions_copied_events_count_in_the_parents_run(self):
        rows = [self.startup(), self.loaded('systematic-debugging'), user('grill me', ts=self.ts()),
                user('<command-name>/demo-skill</command-name>', ts=self.ts()),
                self.record_write('c0ffee01', started='2026-11-05T10:00:00Z')]
        self.session(rows, project='proj')
        self.session(rows + [asst('own', ts=self.ts())], project='proj', sid='sess-2')
        self.records([run_record(head='c0ffee01', schema=2, started='2026-11-05T10:00:00Z', steps=[])])
        data = self.json()
        run = data['runs'][0]
        parent, fork = (next(s for s in data['sessions'] if s['file'].endswith(name))
                        for name in ('sess-1.jsonl', 'sess-2.jsonl'))
        self.assertTrue(run['window']['transcript'].endswith('sess-1.jsonl'))
        self.assertEqual([s['name'] for s in run['skills']], ['systematic-debugging'])
        self.assertEqual([t['skill'] for t in run['typed']], ['grill-me'])
        self.assertEqual([c['name'] for c in parent['commands']], ['demo-skill'])  # the transcript's, as ever
        self.assertEqual((fork['skills'], fork['typed'], fork['commands']), ([], [], []))

    def test_a_fork_sorting_before_its_parent_keeps_the_copied_events(self):
        shared = [self.startup(), self.loaded('systematic-debugging'), user('grill me', ts=self.ts()),
                  user('<command-name>/demo-skill</command-name>', ts=self.ts()), asst('shared', ts=self.ts())]
        self.session(shared, project='proj')
        self.session(shared + [asst('own', ts=self.ts())], project='proj', sid='sess-0')
        data = self.json()
        fork, parent = (next(s for s in data['sessions'] if s['file'].endswith(name))
                        for name in ('sess-0.jsonl', 'sess-1.jsonl'))
        self.assertEqual([s['name'] for s in fork['skills']], ['systematic-debugging'])
        self.assertEqual([t['skill'] for t in fork['typed']], ['grill-me'])
        self.assertEqual([c['name'] for c in fork['commands']], ['demo-skill'])
        self.assertEqual((parent['skills'], parent['typed'], parent['commands']), ([], [], []))

    def test_a_skipped_transcripts_typed_trigger_counts_in_its_fork(self):
        # The parent has no assistant row, and its trigger is older than this week, so it is no unit and warns nothing.
        trigger = user('grill me', ts='2026-10-30T09:00:00.000Z')
        self.session([trigger], project='proj')
        self.session([trigger, self.loaded('grill-me'), asst('own', ts=self.ts())], project='proj', sid='sess-2')
        data = self.json()
        self.assertEqual([os.path.basename(s['file']) for s in data['sessions']], ['sess-2.jsonl'])
        self.assertEqual(data['sessions'][0]['typed'],
                         [{'skill': 'grill-me', 'time': trigger['timestamp'], 'met': True}])
        self.assertEqual(data['totals']['skills']['grill-me']['missing'], 0)

    def test_a_message_split_across_a_window_end_counts_once_from_its_fullest_row(self):
        rows = [self.startup(), asst('M', u=usage(None, 10, 5), ts=self.ts()),
                self.record_write('aaaa1111', started='2026-11-05T10:00:00Z'), asst('M', u=usage(None, 10, 50), ts=self.ts())]
        self.session(rows, project='proj')
        self.records([run_record(head='aaaa1111', schema=2, started='2026-11-05T10:00:00Z', steps=[])])
        data = self.json()
        run, session = data['runs'][0], data['sessions'][0]
        self.assertEqual(run['models']['claude-opus-5-5']['output'], 10)  # the record write only
        self.assertEqual(session['models']['claude-opus-5-5']['output'], 50)  # the message's fullest row, after the end

    def split_across_a_cut(self, day, cut, *args):
        """Messages M, O (rows in the run window) and N, P (rows before it) each split with a row past the cut; O's and
        P's fullest rows are inside the range, M's and N's past the cut."""
        def at(clock, mid, output):
            return asst(mid, u=usage(None, 10, output), ts=f'{day}T00:00:{clock:02d}.000Z')
        inside = [at(1, 'N', 5), at(1, 'P', 50), at(3, 'M', 5), at(3, 'O', 50)]
        past = [asst(mid, u=usage(None, 10, output), ts=cut) for mid, output in (('M', 50), ('N', 50), ('O', 5), ('P', 5))]
        rows = inside + [dict(self.record_write('aaaa1111', started=f'{day}T00:00:02Z'), timestamp=f'{day}T00:00:05.000Z')]
        self.session(past + rows if cut < day else rows + past, project='proj')
        self.records([run_record(head='aaaa1111', date=f'{day}T00:00:05Z', schema=2, started=f'{day}T00:00:02Z',
                                 steps=[])])
        data = self.json(*args)
        run, session = data['runs'][0], data['sessions'][0]
        # Each message counts once, from its fullest row, in the unit holding it; M and N count nowhere.
        self.assertEqual(run['models']['claude-opus-5-5']['output'], 60)  # O's fullest row and the record write
        self.assertEqual(session['models']['claude-opus-5-5']['output'], 50)  # P's fullest row
        self.assertEqual(sum(m['output'] for u in (run, session) for m in u['models'].values()), 110)

    def test_a_message_split_across_a_past_now_counts_from_its_fullest_row_or_nowhere(self):
        self.split_across_a_cut('2026-11-05', '2026-11-05T00:00:07.000Z', '--now', '2026-11-05T00:00:06Z')

    def test_a_message_split_across_since_counts_from_its_fullest_row_or_nowhere(self):
        self.split_across_a_cut('2026-11-05', '2026-11-04T23:59:59.500Z', '--since', '2026-11-05')

    def test_rows_in_the_window_of_a_run_before_since_belong_to_the_session(self):
        rows = [self.startup(), asst('in', model='claude-sonnet-5-5', ts=self.ts()), user('grill me', ts=self.ts()),
                self.record_write('aaaa1111', started='2026-11-05T10:00:00Z')]
        self.session(rows, project='proj')
        self.records([run_record(head='aaaa1111', date='2026-11-04T12:00:00Z', schema=2,
                                 started='2026-11-05T10:00:00Z', steps=[])])
        data = self.json('--since', '2026-11-05')
        self.assertEqual(data['runs'], [])
        session = data['sessions'][0]
        self.assertIn('claude-sonnet-5-5', session['models'])
        self.assertEqual([t['skill'] for t in session['typed']], ['grill-me'])

    def changed(self, cursor):
        return self.json('--changed-since', cursor)

    def test_units_carry_the_dashboards_keys(self):
        self.partition_fixture()
        undated = run_record(head='nowhere', branch='undated')
        del undated['date']
        self.records([undated])
        data = self.json()
        run = data['runs'][0]
        rec = run['record']
        self.assertEqual(run['key'], key(f"{rec['date']}|{rec['project']}|{rec['branch']}"))
        self.assertEqual([r['key'] for r in data['runs'] if r['record']['branch'] == 'undated'],
                         [key('|/p|undated')])
        self.assertTrue(all(s['key'] == key(s['file']) for s in data['sessions']))

    def test_plain_json_leaves_no_state_and_changed_since_bootstraps_from_nothing(self):
        full = self.partition_fixture()
        self.json()
        self.assertFalse(os.path.exists(os.path.join(self.root, 'cache', 'state.json')))
        first = self.changed('0')
        self.assertRegex(first['cursor'], r'^[0-9a-f]{16}:1$')
        self.assertEqual(first['removed'], {'runs': [], 'sessions': []})
        for data in (first, full):
            data.pop('generated')
        self.assertEqual({k: v for k, v in first.items() if k not in ('cursor', 'removed')}, full)
        second = self.changed(first['cursor'])
        self.assertEqual((second['runs'], second['sessions']), ([], []))
        self.assertEqual(second['cursor'], first['cursor'][:-1] + '2')

    def test_changed_since_exports_what_changed_and_what_was_removed(self):
        self.partition_fixture()
        cursor = self.changed('0')['cursor']
        other = os.path.join(self.projects, 'proj', 'sess-2.jsonl')
        self.write_lines(other, [asst('more', ts=self.ts())])
        grown = self.changed(cursor)
        self.assertEqual(([r['key'] for r in grown['runs']], [s['file'] for s in grown['sessions']]), ([], [other]))
        os.remove(other)
        gone = self.changed(grown['cursor'])
        self.assertEqual(gone['removed']['sessions'], [key(other)])
        self.assertEqual(self.changed(gone['cursor'])['removed'], {'runs': [], 'sessions': []})

    def test_an_unknown_cursor_or_a_lost_state_exports_everything(self):
        full = self.partition_fixture()
        cursor = self.changed('0')['cursor']
        count = len(full['runs']) + len(full['sessions'])
        for bad in ('x', '0123456789abcdef:1', cursor.split(':')[0] + ':99'):
            out = self.changed(bad)
            self.assertEqual((len(out['runs']) + len(out['sessions']), out['removed']), (count, {'runs': [], 'sessions': []}))
        with open(os.path.join(self.root, 'cache', 'state.json'), 'w') as f:
            f.write('broken')
        out = self.changed(cursor)
        self.assertEqual(len(out['runs']) + len(out['sessions']), count)
        self.assertNotEqual(out['cursor'].split(':')[0], cursor.split(':')[0])

    def test_a_unit_removed_then_restored_unchanged_is_exported_again(self):
        self.partition_fixture()
        cursor = self.changed('0')['cursor']
        other = os.path.join(self.projects, 'proj', 'sess-2.jsonl')
        os.rename(other, other + '.away')
        gone = self.changed(cursor)
        self.assertEqual(gone['removed']['sessions'], [key(other)])
        os.rename(other + '.away', other)
        back = self.changed(gone['cursor'])
        self.assertEqual(([r['key'] for r in back['runs']], [s['file'] for s in back['sessions']]), ([], [other]))
        self.assertEqual(back['removed'], {'runs': [], 'sessions': []})

    def test_a_malformed_state_gives_a_new_id_and_a_full_export(self):
        full = self.partition_fixture()
        count = len(full['runs']) + len(full['sessions'])
        unit = {'kind': 'run', 'hash': 'h', 'changed': 1, 'seen': 1}
        for units, sequence in (({}, -1), ({'k': 'x'}, 1), ({'k': dict(unit, seen=True)}, 1),
                                ({'k': dict(unit, kind=None)}, 1), ({'k': dict(unit, changed=2)}, 1)):
            with open(os.path.join(self.root, 'cache', 'state.json'), 'w') as f:
                json.dump({'version': 1, 'id': 'abc', 'sequence': sequence, 'units': units}, f)
            out = self.changed('abc:0')
            self.assertEqual((len(out['runs']) + len(out['sessions']), out['cursor'].split(':')[0] != 'abc'),
                             (count, True), (units, sequence))

    def test_units_sharing_a_key_change_together(self):
        self.partition_fixture()
        self.records([run_record(head='nowhere')])  # shares the fixture run's date, project and branch
        cursor = self.changed('0')['cursor']
        self.records([run_record(head='nowhere3', branch='other')])
        out = self.changed(cursor)
        self.assertEqual([r['record']['branch'] for r in out['runs']], ['other'])
        self.write_lines(os.path.join(self.projects, 'proj', 'sess-1.jsonl'),
                         [{'type': 'custom-title', 'customTitle': 'Renamed', 'sessionId': 's'}])
        out = self.changed(out['cursor'])
        self.assertEqual([r['record']['branch'] for r in out['runs']], ['b', 'b'])

    def test_changed_since_prunes_only_its_own_entries_of_deleted_transcripts(self):
        self.session([asst('a', ts=self.ts())], project='proj')
        self.session([asst('b', ts=self.ts())], project='proj', sid='sess-2')
        folder, sibling = os.path.join(self.root, 'cache [1]'), os.path.join(self.root, 'cache 1', 'transcripts')
        os.makedirs(sibling)
        stranger = os.path.join(sibling, 'someone-elses.json')
        with open(stranger, 'w') as f:
            json.dump({'path': os.path.join(self.root, 'nowhere.jsonl')}, f)
        cursor = self.json('--changed-since', '0', '--cache', folder)['cursor']
        gone = os.path.join(self.projects, 'proj', 'sess-2.jsonl')
        os.remove(gone)
        self.json('--changed-since', cursor, '--cache', folder)
        paths = set()
        for entry in glob.glob(os.path.join(glob.escape(folder), 'transcripts', '*.json')):
            with open(entry) as f:
                paths.add(json.load(f)['path'])
        self.assertNotIn(gone, paths)
        self.assertIn(os.path.join(self.projects, 'proj', 'sess-1.jsonl'), paths)
        self.assertTrue(os.path.exists(stranger))

    def test_changed_since_refuses_since_no_cache_and_other_modes(self):
        for args in (('--json', '--changed-since', '0', '--since', '2026-11-01'),
                     ('--json', '--changed-since', '0', '--no-cache'), ('--health', '--changed-since', '0')):
            p = self.report_process(*args)
            self.assertEqual((p.returncode, p.stdout), (2, ''))
            self.assertIn('--changed-since', p.stderr)
            self.assertNotIn('unrecognized', p.stderr)  # the refusal is the option's own, not argparse's


if __name__ == '__main__':
    unittest.main()
