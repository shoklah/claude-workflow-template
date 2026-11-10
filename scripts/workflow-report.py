#!/usr/bin/python3 -I
"""Read-only report over Claude Code transcripts and the workflow run log.

Usage: workflow-report.py [--since YYYY-MM-DD] [--projects DIR] [--log FILE] [--agents DIR] [--export-stamp FILE]
                          [--cache DIR] [--no-cache] [--brief | --health | --schema | --json | --html [FILE]]
                          [--changed-since CURSOR] [--now ISO]

  --projects  transcript tree (default ~/.claude/projects)
  --log       run log, one JSON run record per line (default ~/.claude/workflow-log.jsonl)
  --agents    installed agent files, whose model and effort pins --health checks (default ~/.claude/agents)
  --export-stamp  the public template's last export, {"repo", "sha", "date"}, which --brief compares with
              origin/main (default ~/.claude/workflow-export.json)
  --cache     the parse cache and the export state (default ~/.claude/workflow-report-cache); files are written
              atomically and hold personal data
  --no-cache  read and write no cache; the output is the same
  --since     ignore transcript rows and run records before this date (UTC)
  --brief     at most 3 lines: this week's advisor share of Fable use, the trial's progress, then this week's
              first WARN line with a count of the rest, or else the consultations with no note, then any commits on
              main since the last template export
  --health    every WARN line for the runs since --since, from the run log, the runs' transcripts and git, for
              this week's sessions with no run record, for this week's plan reviews with no plan-simplifier
              since the previous plan review, for this week's typed triggers whose skill never loaded, and for
              this week's sessions outside every run with no startup guidelines, or "Health: no problems"; for
              schema 2 runs it also checks the steps against the launched agents, their count lines, the agents'
              pinned model, effort and skills, and the skills the run's path requires, for schema 3 plan runs a
              required offer with no entry, and for schema 4 records each step's agent id against the launched
              dispatches, and for schema 5 records each finding's category and validity, each review step's
              strengths and their number against its report's count line; then information lines: the "fixed,
              not proportionate" counts, archives missing or with another Started: time, and the runs whose git
              evidence is unavailable
  --schema    print the run record schema, with the finding category list under "category_list", and exit
  --json      print one JSON object, and nothing else: "version" (1; adding keys keeps it, renaming or removing one
              bumps it), "generated", "since", "now", "categories" (the finding category list, with its version,
              tie-break rule, validity definitions and areas), "stage_table" (the main session's recommended model and
              effort, by path and stage), "runs" (each record in range as written, its window and what the window
              shows: dispatches, skills, refusals, models, tokens and cost, advisor, its spend by phase, its WARN and
              information lines, later fix commits), "sessions" (each main transcript's rows in range outside its run
              windows, with their models, advisor and spend by phase, and its start, end, project and slash commands,
              by name and time, never their arguments) and "totals" (scoreboard, weeks, run_time, skills, models,
              advisor, offers, health); each WARN also as an object with its kind, run or session, step, skill and
              agent, each session's title, each step's agent id, time and end with the dispatch and skill load tied
              to it, and each finding's category and validity and each review step's strengths. Every transcript row
              belongs to one run or to its transcript's session, so every total is a sum over the runs and sessions
              exported. It holds personal data: print it, never write it into a repo
  --changed-since CURSOR  with --json: print only the runs and sessions whose entry changed since the export that
              gave CURSOR, the keys removed since, and a new cursor; 0 or an unknown cursor exports everything; refused
              with --since or --no-cache
  --html      write the same data as one self-contained HTML page, with no script and nothing loaded from outside,
              to FILE (default ~/.claude/workflow-dashboard.html), and print "Wrote FILE". It holds personal data:
              never write it into a repo
  --now       ISO time to use as "now": transcript rows and run records after it are ignored (default: the current
              time)

Advisor use is read from message.usage.iterations entries of type advisor_message, once per message id.
Weeks run from Sunday 07:00 UTC. Costs are list-price estimates at each model's list price.
"""
import argparse
import bisect
import datetime
import fcntl
import functools
import glob
import hashlib
import heapq
import html
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile

OUTCOMES = ('fixed', 'applied', 'declined', 'deferred', 'ruled', 'open', 'to_owner')
OFFER_OUTCOMES = ('accepted', 'skipped', 'not_offered', 'updated')
REQUIRED_OFFERS = ('grill-me',)
STEP_KINDS = ('plan_simplify', 'plan_review', 'task_review', 'fix_check', 'simplify', 'final_review', 'final_fix_check')

# The finding category list: its version, its tie-break rule, the validity definitions and a list per area. An id is
# permanent: a new version adds ids, retires one (kept and marked) or rewords text, and never reuses, renames or removes
# one; any change bumps the version. The reviewer agents carry their area's copy: keep them the same.
CATEGORIES = {
    'version': 1,
    'rule': 'Name the problem, not its fix. A wrong exit code is `error_handling` even when the fix adds a test; a '
            'finding is `tests` only when the code is right and its tests are missing or wrong. When two categories '
            'still fit, pick the more specific one.',
    'validity': {
        'valid': 'The problem the finding names is really there in the code or the plan, whether or not the brief '
                 'covers it; a real problem outside the brief is valid and declined as out of scope. For simplify, '
                 'the cleanup really applies.',
        'invalid': "The problem isn't there, or the reviewer misread the brief or the code.",
        'unclear': "The session can't tell without more work than the finding is worth.",
    },
    'plan': [
        {'id': 'scope', 'label': 'Scope', 'means': "The plan's work differs from the spec's: work missing, work the "
         "spec doesn't ask for (including new code where existing code would do), or a step that contradicts the "
         "spec."},
        {'id': 'task_split', 'label': 'Task split and order', 'means': "Tasks too big or too small, in the wrong "
         "order, depending on each other when they shouldn't, or grouped into a wave that breaks its rules."},
        {'id': 'spec_gap', 'label': 'Spec gap', 'means': 'The spec itself leaves out or gets wrong something the '
         'plan needs, so the plan has to guess.'},
        {'id': 'risk_missed', 'label': 'Risk missed', 'means': "A failure mode, edge case or risk area the plan "
         "doesn't handle or test."},
        {'id': 'test_plan', 'label': 'Test plan', 'means': 'Planned tests that are missing, wrong, test the wrong '
         'thing, or outweigh the code they test.'},
        {'id': 'code_facts', 'label': 'Code facts', 'means': "The plan's claims about the existing code are wrong: "
         "files, functions, signatures, commands or behaviour."},
    ],
    'implementation': [
        {'id': 'correctness', 'label': 'Correctness', 'means': 'The code does the wrong thing for some input, state '
         'or sequence.'},
        {'id': 'error_handling', 'label': 'Error handling', 'means': "A failure isn't caught, reported or recovered "
         "from as it should be: exit codes, bad input, timeouts."},
        {'id': 'data_safety', 'label': 'Data safety', 'means': 'Data can be lost, corrupted, overwritten or written '
         'in the wrong place.'},
        {'id': 'security_privacy', 'label': 'Security and privacy', 'means': 'Secrets, personal data or permissions '
         'are exposed, or untrusted input is trusted.'},
        {'id': 'reuse', 'label': 'Reuse and duplication', 'means': 'The change duplicates, bypasses or departs from '
         'an existing helper or pattern.'},
        {'id': 'conventions', 'label': 'Conventions', 'means': "The change contradicts a `## Code conventions` entry or "
         "the codebase's style, or leaves a decision it relies on unrecorded there."},
        {'id': 'simplicity', 'label': 'Simplicity', 'means': "More structure, options or code than the requirement "
         "needs, or something the spec doesn't require."},
        {'id': 'tests', 'label': 'Tests', 'means': 'Tests missing, weak, wrong, or heavier than the code they test.'},
        {'id': 'naming', 'label': 'Naming and clarity', 'means': 'Names, comments or structure that make the code '
         'hard to read.'},
        {'id': 'performance', 'label': 'Performance', 'means': 'Needless work, slowness or resource use.'},
        {'id': 'docs', 'label': 'Docs', 'means': 'README, help text, CLAUDE.md or other docs missing, wrong or '
         'stale.'},
    ],
}
# Area, the step kinds whose findings it labels, the CATEGORIES list it uses.
AREAS = (('plan', ('plan_simplify', 'plan_review'), 'plan'),
         ('implementation', ('task_review', 'final_review'), 'implementation'),
         ('cleanup', ('simplify',), 'implementation'))
STRENGTH_STEPS = ('plan_simplify', 'plan_review', 'task_review', 'final_review')
VALIDITY = tuple(CATEGORIES['validity'])


def area_ids(kind):
    """The category ids of the list the step kind's area uses, or an empty set for a kind with no area."""
    return {c['id'] for _, kinds, key in AREAS if kind in kinds for c in CATEGORIES[key]}


def category_list():
    """The finding category list as --schema prints it under "category_list" and --json as "categories"."""
    return {'version': CATEGORIES['version'], 'rule': CATEGORIES['rule'], 'validity': CATEGORIES['validity'],
            'areas': [{'area': area, 'steps': list(kinds), 'list': CATEGORIES[key]} for area, kinds, key in AREAS]}

SCHEMA = {
    'schema': 'int, record format: 5; a record without it is 1',
    'date': 'ISO 8601 time the run finished',
    'started': 'ISO 8601 time the session named the run\'s path and stage, in its first message',
    'project': 'absolute path of the project',
    'branch': 'branch the run worked on',
    'base': 'commit the branch started from',
    'head': 'last commit of the run',
    'path': '"small", "full" or "plan"; a plan record leaves out tasks, fix_rounds, failing_test, fix_growth and '
            'simplify, and base and head are optional on it',
    'plan': 'path of the plan file, on plan records and on runs that build from a plan',
    'archive': 'path of the run\'s ledger in the archive, or absent',
    'risk_area': 'bool',
    'stage': 'Fable stage: "1", "2", "3" or "out"',
    'advisor': 'bool, advisor on during the run',
    'fable_start': 'int, weekly Fable percentage at start',
    'fable_end': 'int, weekly Fable percentage at end',
    'all_start': 'int, weekly all-model percentage at start',
    'all_end': 'int, weekly all-model percentage at end',
    'tasks': 'int',
    'fix_rounds': 'int',
    'stops': 'int, stops for the owner',
    'final_findings': '{"critical": N, "important": N, "minor": N, "unneeded": N} from the final review',
    'simplify': '{"ran": "agents"|"inline"|"no", "applied": N, "skipped": N}: how simplify ran ("inline" only under '
                'about 100 changed lines) and its fixes',
    'failing_test': 'list of {"task": N, "result": "passed"|"failed"|"no_tests"|"couldnt_run"|"exempt"}',
    'fix_growth': '{"task_added": N, "task_removed": N, "fix_added": N, "fix_removed": N, "unplanned_files": [paths]}: '
                  'lines from the tasks\' own commits and from fix-round commits, and new files the plan did not name',
    'steps': 'list of the steps that ran, in order: {"step": ' + '|'.join(f'"{k}"' for k in STEP_KINDS)
             + ', "task": N, "wave": "A" in place of "task" on the wave check (a task_review) and on the wave\'s '
             'fix_check steps, "round": N (0 is the first review, fix-checks are 1 to 3), "agent", "model", "effort" '
             '(as dispatched; agent "simplify" for the skill), "agent_id": the agentId the Agent tool returned for the '
             'step\'s dispatch, which names its subagent transcript (every kind but simplify; schema 4), "counts": '
             '{keys of the step\'s count line: reviews critical, important, minor, plus unneeded for the final review; '
             'plan_simplify cuts, flags; plan_review findings; fix-checks fixed, not_proportionate, open; simplify '
             'applied, skipped}, "findings" (every step but fix-checks): [{"id": "F7", "severity" (or "kind": '
             '"cut"|"flag" for plan_simplify), "summary", '
             '"outcome": ' + '|'.join(f'"{o}"' for o in OUTCOMES)
             + ', "reason" (required for declined and ruled), "closed_round": N, "category": an id from its step\'s '
             'area list in "category_list", the reviewer\'s, "validity": "valid"|"invalid"|"unclear", the '
             'session\'s, by the definitions in "category_list" (schema 5), "verdicts": the fix-checker verdicts in '
             'order}], '
             '"strengths" (plan_simplify, plan_review, task_review and final_review; schema 5): [{"category", '
             '"summary"}], [] for none; "counts" never holds "strengths"}',
    'waves': 'list of {"wave": "A", "tasks": [N, ...], "conflicts": N}, one per wave in order; absent on a run with no '
             'waves. tasks still counts plan tasks, fix_rounds includes the waves\' rounds and stops counts a wave stop',
    'offers': 'list of {"skill", "outcome": ' + '|'.join(f'"{o}"' for o in OFFER_OUTCOMES)
              + ', "reason" (required for not_offered)}, one per offer answered before the record is written; plan '
              'records carry ' + ' and '.join(REQUIRED_OFFERS) + '. Schema 3; absent on older records',
    'categories': 'int, the version of the finding category list the labels use, as "category_list" below prints it '
                  '(schema 5)',
}

# List prices, dollars per million tokens: input, output, cache read, 5-minute cache write, 1-hour cache write.
# From https://platform.claude.com/docs/en/about-claude/pricing, read on 2026-11-08.
PRICES = {
    'claude-fable-5-1': (10.0, 50.0, 0.25, 12.5, 20.0),
    'claude-fable-5': (10.0, 50.0, 1.0, 12.5, 20.0),
    'claude-opus-5-5': (4.0, 20.0, 0.2, 5.0, 8.0),
    'claude-opus-5': (5.0, 25.0, 0.5, 6.25, 10.0),
    'claude-opus-4-8': (5.0, 25.0, 0.5, 6.25, 10.0),
    'claude-sonnet-5-5': (2.0, 10.0, 0.1, 2.5, 4.0),
    'claude-sonnet-5': (2.0, 10.0, 0.2, 2.5, 4.0),
    'claude-sonnet-4-6': (3.0, 15.0, 0.3, 3.75, 6.0),
    'claude-haiku-5-5': (0.1, 0.5, 0.01, 0.125, 0.2),
    'claude-haiku-4-5': (1.0, 5.0, 0.1, 1.25, 2.0),
}
DATE_SUFFIX = re.compile(r'-\d{8}$')
ADVISOR_MODEL = 'claude-fable-5-1'  # for advisor iterations that name no model
# Report label, usage field.
TOKENS = (('input', 'input_tokens'), ('output', 'output_tokens'), ('cache_read', 'cache_read_input_tokens'),
          ('cache_write', 'cache_creation_input_tokens'))
# The phases --json splits a run's or session's spend into, in the order it lists them.
PHASES = ('design', 'plan_writing', 'plan_simplify', 'plan_review', 'implement', 'task_review', 'fix_check',
          'simplify', 'final_review', 'final_fix_check', 'coordination', 'unattributed')
# The main session's phase from the last of these skills it loaded, and before any of them from the run's path.
SKILL_PHASES = {'brainstorming': 'design', 'writing-plans': 'plan_writing',
                'subagent-driven-development': 'coordination', 'executing-plans': 'implement',
                'test-driven-development': 'implement'}
PATH_PHASES = {'plan': 'design', 'full': 'coordination', 'small': 'implement'}

STOP_SHARE = 25.0
TRIAL_RUNS = 5
TRIAL_WEEKS = 3
FIX_WINDOW_DAYS = 14
NEVER_CONSULT = ('task-reviewer', 'final-reviewer', 'plan-reviewer', 'plan-simplifier', 'fix-checker')
FINDINGS = ('critical', 'important', 'minor', 'unneeded')
VERDICTS = ('fixed', 'not_proportionate', 'open')
# Each dispatched step kind's report: its name in WARN lines, its count line and that line's keys.
COUNT_LINES = {'plan_simplify': ('plan simplify', 'Findings', ('cuts', 'flags')),
               'plan_review': ('plan review', 'Findings', ('findings',)),
               'task_review': ('review', 'Findings', FINDINGS), 'fix_check': ('fix-check', 'Verdicts', VERDICTS),
               'final_review': ('review', 'Findings', FINDINGS), 'final_fix_check': ('fix-check', 'Verdicts', VERDICTS)}
# What a step's findings add up to: the count each finding adds to, and the counts they make up. Fix-checks carry no
# findings.
ADDS_UP = {'plan_simplify': (lambda f: {'cut': 'cuts', 'flag': 'flags'}.get(f['kind']), ('cuts', 'flags')),
           'plan_review': (lambda f: 'findings', ('findings',)),
           'task_review': (lambda f: f['severity'], FINDINGS),
           'simplify': (lambda f: 'applied' if f['outcome'] == 'applied' else 'skipped', ('applied', 'skipped')),
           'final_review': (lambda f: f['severity'], FINDINGS)}
TEST_RESULTS = ('passed', 'failed', 'no_tests', 'couldnt_run', 'exempt')
SIMPLIFY_RAN = ('agents', 'inline', 'no')
NOTE_RE = re.compile(r'^Advisor: .+ \| changed: (yes|no) \| .*$', re.M)
SCRIPT_RE = re.compile(r'([^/\s"\']+\.(?:py|sh))')
WRAP_UP = 'mcp__ccd_turn__wrap_up'
GIT_TIMEOUT = 30

# Copied from hooks/guard-agent-dispatch.py, since scripts can't import each other: keep them the same.
UNNAMED_TYPES = {None, '', 'general-purpose'}
# The openings of its refusal reasons, which an Agent call's error result holds.
GUARD_REASONS = {'no_model': "This dispatch has no model and no named agent",
                 'no_simplify': "No simplify since the last final review",
                 'no_plan_simplify': "No plan-simplifier since the last plan review"}
SIMPLIFY_COMMAND = re.compile(r'\s*(<command-message>.*?</command-message>\s*)?<command-name>/simplify</command-name>', re.S)
# A general-purpose or untyped dispatch is a final review when its description says so, and is not a fix wave or a
# fix-check of one.
FINAL_REVIEW_DESCRIPTION = re.compile(r'final[ -]review', re.I)
NOT_FINAL_REVIEW = re.compile(r'fix|check', re.I)
# The same for plan reviews and plan simplifies, which NOT_FINAL_REVIEW also excludes: a check that only mentions
# plan-simplifier is not a plan simplify.
PLAN_REVIEW_DESCRIPTION = re.compile(r'plan[ -](re-?)?review', re.I)
PLAN_SIMPLIFY_DESCRIPTION = re.compile(r'plan[ -]simplif', re.I)
# Health check: a general-purpose dispatch with one of these descriptions did a named agent's job.
TASK_REVIEW = re.compile(r're-?review|review task|task \d+ review|(spec|quality|code quality) review', re.I)
NAMED_ROLE = re.compile(r'implement|plan review|fix-?check|final[ -]review|' + TASK_REVIEW.pattern, re.I)
WORKFLOW_TYPES = ('implementer', 'implementer-copy', 'implementer-escalated', 'final-reviewer')
SEVERITY = re.compile(r'[#*_\s\d.)-]*(critical|important|minor)\b', re.I)
NOT_PROPORTIONATE = 'fixed, not proportionate'
CONVENTIONS = '## Code conventions'
FIX_ROUND_SUBJECT = 'fix: address review findings'
FIX_GROWTH_SLACK = 20  # lines; a recorded fix_added is off when it misses git's by more than this and by over 50%
NOT_FOUND = re.compile(r"Agent type '([^']*)' not found")
STARTUP_TAG = b'<karpathy-guidelines-session-start>'
# The session-start hook's using-superpowers block: the skill is in context without a Skill call.
SUPERPOWERS_TAG = b'<using-superpowers-session-start>'
RUN_LOG = 'workflow-log.jsonl'
# Only lines matching this can hold a dispatch, a simplify call, a record write, the startup block, an error, a skill
# load (the hook's using-superpowers block included, which it prints alone when the karpathy file is unreadable), a
# typed trigger, a spec or plan write or a session title. A typed command shows as <command-name>/name</command-name>.
HEALTH_NEEDLES = (b'"Agent"', b'simplify', b'workflow-log.jsonl', b'karpathy-guidelines-session-start',
                  b'using-superpowers-session-start',
                  b'"is_error":true', b'"is_error": true', b'Base directory for this skill', b'rill me',
                  b'command-name', b'docs/superpowers/', b'"custom-title"')
SKILL_LOADED = 'Base directory for this skill: '
SPECS_AND_PLANS = {'spec': 'docs/superpowers/specs/', 'plan': 'docs/superpowers/plans/'}


def command_re(name):
    """A typed /name command, in SIMPLIFY_COMMAND's shape; name is a pattern, spliced in as it is."""
    return re.compile(SIMPLIFY_COMMAND.pattern.replace('/simplify', '/' + name), re.S)


# What a typed row can hold that calls for a skill, and that skill. A command pattern is tried with match, a phrase
# with search.
TYPED_TRIGGERS = (
    (re.compile(r'\bgrill me\b', re.I), 'grill-me'),
    (command_re('grill-me'), 'grill-me'),
)
# A typed slash command, tried with match: its name only, never its arguments.
COMMAND = command_re(r'(?P<name>[^<]+)')
# The skills a schema 2 record's run window must load, by its path.
REQUIRED_SKILLS = (('plan', 'brainstorming'), ('plan', 'writing-plans'), ('full', 'subagent-driven-development'))
# What shows a skill's outcome: a spec or plan written, the record's simplify counts or failing-test results, or a run
# record written.
SKILL_OUTCOMES = {'brainstorming': 'spec', 'writing-plans': 'plan', 'simplify': 'simplify',
                  'test-driven-development': 'failing_test', 'subagent-driven-development': 'record'}
# The stage table's model and effort for a run's main session, by the record's path and stage; None is the owner's
# choice. A difference is shown, never warned.
RECOMMENDED = {'full': dict.fromkeys(('1', '2', '3', 'out'), 'opus/medium'),
               'plan': {'1': 'fable', '2': 'fable', '3': 'opus', 'out': 'opus'},
               'small': {'1': None, '2': 'opus', '3': 'opus', 'out': 'opus'}}
JSON_VERSION = 1
CACHE_VERSION = 3
STATE_VERSION = 1
CACHE_DIR = None  # the parse cache's folder; main sets it from --cache, and leaves it None under --no-cache


def parse_time(value):
    """Aware UTC datetime from an ISO 8601 string or YYYY-MM-DD, or None."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace('Z', '+00:00')
    try:
        t = datetime.datetime.fromisoformat(text)
    except ValueError:
        try:
            t = datetime.datetime.strptime(text[:19], '%Y-%m-%dT%H:%M:%S')
        except ValueError:
            return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    return t.astimezone(datetime.timezone.utc)


def stamp(t):
    """t in the transcripts' timestamp form, YYYY-MM-DDTHH:MM:SS.mmmZ in UTC, so it compares as a string with theirs."""
    t = t.astimezone(datetime.timezone.utc)
    return t.strftime('%Y-%m-%dT%H:%M:%S.') + f'{t.microsecond // 1000:03d}Z'


def week_start(t):
    """Start of the week containing t; weeks run from Sunday 07:00 UTC."""
    shifted = t - datetime.timedelta(hours=7)
    days_since_sunday = (shifted.weekday() + 1) % 7
    day = shifted.date() - datetime.timedelta(days=days_since_sunday)
    return datetime.datetime(day.year, day.month, day.day, 7, tzinfo=datetime.timezone.utc)


def is_num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def num(value):
    return value if is_num(value) else 0


def field(obj, key, kind, default):
    """obj[key] when it has the expected type, else default. A bool is never an int, as in num."""
    value = obj.get(key)
    return value if isinstance(value, kind) and not (kind is int and isinstance(value, bool)) else default


def kind_costs(u, model):
    """The list price of each token kind of usage u on model, keyed as TOKENS labels them, or None for a model PRICES
    lacks. Cache writes are priced by lifetime when u['cache_creation'] splits them, else all at the 5-minute rate."""
    prices = PRICES.get(DATE_SUFFIX.sub('', model))
    if prices is None:
        return None
    p_input, p_output, p_read, p_5m, p_1h = prices
    split = u.get('cache_creation')
    if isinstance(split, dict):
        write_5m, write_1h = num(split.get('ephemeral_5m_input_tokens')), num(split.get('ephemeral_1h_input_tokens'))
    else:
        write_5m, write_1h = num(u.get('cache_creation_input_tokens')), 0
    return {'input': num(u.get('input_tokens')) * p_input / 1e6, 'output': num(u.get('output_tokens')) * p_output / 1e6,
            'cache_read': num(u.get('cache_read_input_tokens')) * p_read / 1e6,
            'cache_write': (write_5m * p_5m + write_1h * p_1h) / 1e6}


def cost(u, model):
    """The list price of usage u on model, the sum of its kind_costs, or None for a model PRICES lacks."""
    kinds = kind_costs(u, model)
    return None if kinds is None else sum(kinds.values())


def read_jsonl(path, stats):
    """Yield the JSON objects in a file, counting lines that don't parse as objects."""
    try:
        f = open(path, 'rb')
    except OSError:
        return
    with f:
        for raw in f:
            if raw.isspace():
                continue
            try:
                row = json.loads(raw)
            except (ValueError, RecursionError):
                row = None
            if not isinstance(row, dict):
                stats['skipped'] += 1
                continue
            yield row


def usage_iterations(msg):
    """A message's usage dict and the dict items of its iterations."""
    usage = field(msg, 'usage', dict, {})
    return usage, [i for i in usage.get('iterations') or [] if isinstance(i, dict)]


class Transcripts:
    def __init__(self, since, now, keep=None, shared=None, phase_of=None):
        """A row counts when it is in since..now and keep is None or keep(its timestamp) is true. With shared, the
        message ids and advisor failures already counted are shared's, so each counts once across the instances. With
        phase_of(path, timestamp), the phase of a message counted from a row of the transcript at path, each message
        also counts in its phase."""
        self.since = since
        self.now = now
        self.keep = keep
        self.phase_of = phase_of
        self.phases = {}
        self.advisor_ids = shared.advisor_ids if shared else set()
        self.spent_ids = shared.spent_ids if shared else set()
        self.tokens = {label: 0 for label, _ in TOKENS}
        self.advisor_cost = 0.0
        self.models = {}
        self.efforts = {}
        self.by_role = {}
        self.failed_ids = shared.failed_ids if shared else set()
        self.failed = {}
        self.notes = 0
        self.changed = 0
        self.missing = 0
        self.week_advisor = {}
        self.week_fable = {}
        self.hooks = {}
        self.tool_ids = set()
        self.main_calls = 0
        self.typed = 0
        self.ask_user = 0
        self.wrap_up_ask = 0
        self.assistant_rows = 0  # the assistant rows counted, before the dedup by message id
        self.last = None  # the greatest timestamp of the rows read, the since/now filter aside

    def read_file(self, path, role, stats):
        if self.since is not None:
            try:
                if os.path.getmtime(path) < self.since.timestamp():
                    return
            except OSError:
                return
        main = role is None
        file_calls = 0
        file_notes = set()
        # A message split across rows sharing its id counts once, in the unit whose window and since..now range hold
        # its row with the most output tokens among all the file's rows, and nowhere when that row is out of range.
        file_spend = {}
        parsed = file_usage(path)
        stats['skipped'] += parsed['skipped']
        has_origin = parsed['has_origin']
        if parsed['last'] is not None and (self.last is None or parsed['last'] > self.last):
            self.last = parsed['last']
        typed_human = typed_plain = 0
        for row in parsed['rows']:
            t = parse_time(row.get('timestamp'))
            in_range = t is None or not (self.since is not None and t < self.since or self.now is not None and t > self.now)
            kind = row.get('type')
            kept = in_range and (self.keep is None or self.keep(row['timestamp']))
            phase = self.phase_of(path, row['timestamp']) if kept and kind == 'assistant' and self.phase_of else None
            if kind == 'assistant':
                self.pick_spend(row, t, kept, file_spend, phase)
            if not kept:
                continue
            if kind == 'assistant':
                self.assistant_rows += 1
                file_calls += self.assistant(row, t, role, main, file_notes, phase)
            elif kind == 'attachment':
                self.hook(row.get('attachment'))
            elif kind == 'user' and main:
                if row['human']:
                    typed_human += 1
                else:
                    typed_plain += 1
        for mid, (_, model, own, t, kept, phase) in file_spend.items():
            if kept and mid not in self.spent_ids:
                self.spent_ids.add(mid)
                self.spend(model, own, week_start(t) if t is not None else None, phase)
        # Transcripts since Claude Code 2.1.187 mark typed rows origin.kind "human"; rows without it there are
        # injected (reminders, task notifications, resumes). Older transcripts have no origin at all.
        self.typed += typed_human if has_origin else typed_plain
        self.notes += len(file_notes)
        self.changed += sum(1 for _, _, changed in file_notes if changed == 'yes')
        self.missing += max(0, file_calls - len(file_notes))

    def pick_spend(self, row, t, kept, file_spend, phase):
        """Count an assistant row's own iterations now, in phase, when it has no message id and kept is true, else keep
        it in file_spend when it is its message's row with the most output tokens so far."""
        msg = row.get('message')
        if not isinstance(msg, dict):
            return
        mid = msg.get('id')
        model = field(msg, 'model', str, '')
        usage, iterations = usage_iterations(msg)
        # The executor's own sampling; advisor and fallback iterations are counted apart.
        own = [i for i in iterations if i.get('type') == 'message'] if iterations else [usage]
        if not mid:
            if kept:
                self.spend(model, own, week_start(t) if t is not None else None, phase)
            return
        output = sum(num(i.get('output_tokens')) for i in own)
        if mid not in file_spend or output > file_spend[mid][0]:
            file_spend[mid] = (output, model, own, t, kept, phase)

    def assistant(self, row, t, role, main, file_notes, phase):
        msg = row.get('message')
        if not isinstance(msg, dict):
            return 0
        mid = msg.get('id')
        model = field(msg, 'model', str, '')
        on_fable = model.startswith('claude-fable')
        week = week_start(t) if t is not None else None
        content = field(msg, 'content', list, [])
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get('type')
            if btype == 'text' and isinstance(block.get('text'), str):
                for m in NOTE_RE.finditer(block['text']):
                    file_notes.add((mid, m.group(0), m.group(1)))
            elif btype == 'advisor_tool_result':
                self.advisor_result(block)
            elif btype == 'tool_use' and main:
                self.main_tool_use(block)
        _, iterations = usage_iterations(msg)
        effort = row.get('effort')
        if isinstance(effort, str):
            efforts = self.efforts.setdefault(model, {})
            efforts[effort] = efforts.get(effort, 0) + 1
        if not mid or mid in self.advisor_ids:
            return 0
        advisor = [i for i in iterations if i.get('type') == 'advisor_message']
        if not advisor:
            return 0
        self.advisor_ids.add(mid)
        label = (role or 'main') + (' on Fable' if on_fable else '')
        flagged = on_fable or role in NEVER_CONSULT
        entry = self.by_role.setdefault(label, [0, flagged])
        for i in advisor:
            entry[0] += 1
            for token, name in TOKENS:
                self.tokens[token] += num(i.get(name))
            spent = cost(i, field(i, 'model', str, '') or ADVISOR_MODEL) or 0.0
            self.advisor_cost += spent
            if phase is not None:
                phase_advisor = self.phase(phase)['advisor']
                phase_advisor['calls'] += 1
                phase_advisor['cost'] += spent
            if week is not None:
                self.week_advisor[week] = self.week_advisor.get(week, 0.0) + spent
        return len(advisor)

    def spend(self, model, own, week, phase=None):
        """Count the executor's own iterations own on model, in phase when it is not None, and on Fable toward week's
        Fable use."""
        if model not in self.models:
            self.models[model] = dict({label: 0 for label, _ in TOKENS}, cost=cost({}, model))
        entry, part = self.models[model], None
        if phase is not None:
            part = self.phase(phase)['models'].get(model)
            if part is None:
                part = self.phases[phase]['models'][model] = dict(
                    {label: 0 for label, _ in TOKENS}, cost=None if entry['cost'] is None else 0.0, cost_by_kind=kind_costs({}, model))
        entries = [entry] if part is None else [entry, part]
        for i in own:
            for token, name in TOKENS:
                n = num(i.get(name))
                for e in entries:
                    e[token] += n
        if entry['cost'] is None:
            return
        spent = 0.0
        for i in own:
            kinds = kind_costs(i, model)
            spent += sum(kinds.values())
            if part is not None:
                for kind, dollars in kinds.items():
                    part['cost_by_kind'][kind] += dollars
        for e in entries:
            e['cost'] += spent
        if week is not None and model.startswith('claude-fable'):
            self.week_fable[week] = self.week_fable.get(week, 0.0) + spent

    def phase(self, name):
        """The models and advisor counted in phase name so far."""
        return self.phases.setdefault(name, {'models': {}, 'advisor': {'calls': 0, 'cost': 0.0}})

    def phase_list(self):
        """The phases value of --json: one entry per phase that counted a message, in PHASES order."""
        return [dict(name=name, **self.phases[name]) for name in PHASES if name in self.phases]

    def advisor_result(self, block):
        tid = block.get('tool_use_id')
        if tid and tid in self.failed_ids:
            return
        contents = block.get('content')
        for c in contents if isinstance(contents, list) else [contents]:
            if isinstance(c, dict) and c.get('error_code'):
                if tid:
                    self.failed_ids.add(tid)
                code = str(c['error_code'])
                self.failed[code] = self.failed.get(code, 0) + 1

    def main_tool_use(self, block):
        tid = block.get('id')
        if tid:
            if tid in self.tool_ids:
                return
            self.tool_ids.add(tid)
        self.main_calls += 1
        name = block.get('name')
        inp = field(block, 'input', dict, {})
        if name == 'AskUserQuestion':
            self.ask_user += 1
        elif name == WRAP_UP and isinstance(inp.get('ask'), str) and inp['ask'].strip():
            self.wrap_up_ask += 1

    @staticmethod
    def is_typed(row):
        if row.get('isMeta'):
            return False
        msg = row.get('message')
        content = msg.get('content') if isinstance(msg, dict) else None
        if isinstance(content, str):
            return bool(content.strip())
        if isinstance(content, list) and content:
            return all(isinstance(b, dict) and b.get('type') == 'text' for b in content)
        return False

    @staticmethod
    def typed_by_owner(row):
        """Whether a typed row is the owner's: one with an origin only when its kind is "human", one without unless it
        is a compaction summary, which quotes typed text that was not typed again."""
        origin = row.get('origin')
        return Transcripts.is_typed(row) and (origin.get('kind') == 'human' if isinstance(origin, dict)
                                              else not row.get('isCompactSummary'))

    def hook(self, att):
        if not isinstance(att, dict) or att.get('type') != 'hook_success':
            return
        try:
            out = json.loads(att.get('stdout') or '')
        except (ValueError, TypeError, RecursionError):
            return
        spec = out.get('hookSpecificOutput') if isinstance(out, dict) else None
        if not isinstance(spec, dict):
            return
        decision = spec.get('permissionDecision')
        if decision not in ('ask', 'deny'):
            return
        command = field(att, 'command', str, '')
        found = SCRIPT_RE.findall(command)
        name = found[-1] if found else (command or str(att.get('hookName') or 'unknown'))
        reason = spec.get('permissionDecisionReason')
        key = (name, decision, reason if isinstance(reason, str) else '')
        self.hooks[key] = self.hooks.get(key, 0) + 1


def usage_rows(path):
    """What Transcripts.read_file reads of the transcript at path, in one pass: {'rows', 'has_origin', 'last',
    'first', 'cwd', 'skipped'}. A row keeps only the fields read_file reads, a text block only its notes, and a row it
    ignores is dropped; has_origin is whether any row has an origin, last the greatest row timestamp, first the least,
    cwd the cwd of the first row with a string one, and skipped the lines that are not objects."""
    stats = {'skipped': 0}
    rows, has_origin, last, first, cwd = [], False, None, None, None
    for row in read_jsonl(path, stats):
        has_origin = has_origin or isinstance(row.get('origin'), dict)
        timestamp = row.get('timestamp')
        if isinstance(timestamp, str):
            last, first = max(last or timestamp, timestamp), min(first or timestamp, timestamp)
        if cwd is None and isinstance(row.get('cwd'), str):
            cwd = row['cwd']
        kind, msg, att = row.get('type'), row.get('message'), row.get('attachment')
        if kind == 'assistant' and isinstance(msg, dict):
            content = []
            for block in field(msg, 'content', list, []):
                btype = block.get('type') if isinstance(block, dict) else None
                if btype == 'text' and isinstance(block.get('text'), str):
                    notes = [m.group(0) for m in NOTE_RE.finditer(block['text'])]
                    if notes:
                        content.append({'type': btype, 'text': '\n'.join(notes)})
                elif btype == 'advisor_tool_result':
                    items = block.get('content')
                    content.append({'type': btype, 'tool_use_id': block.get('tool_use_id'),
                                    'content': [{'error_code': c['error_code']}
                                                for c in (items if isinstance(items, list) else [items])
                                                if isinstance(c, dict) and c.get('error_code')]})
                elif btype == 'tool_use':
                    name, inp = block.get('name'), field(block, 'input', dict, {})
                    ask = inp.get('ask')
                    content.append({'type': btype, 'id': block.get('id'), 'name': name,
                                    'input': {'ask': ask} if name == WRAP_UP and isinstance(ask, str) else {}})
            rows.append({'type': kind, 'timestamp': timestamp, 'effort': row.get('effort'),
                         'message': {'id': msg.get('id'), 'model': msg.get('model'), 'usage': msg.get('usage'),
                                     'content': content}})
        elif (kind == 'attachment' and isinstance(att, dict) and att.get('type') == 'hook_success'
              and 'permissionDecision' in field(att, 'stdout', str, '')):
            rows.append({'type': kind, 'timestamp': timestamp,
                         'attachment': {key: att.get(key) for key in ('type', 'stdout', 'command', 'hookName')}})
        elif kind == 'user' and Transcripts.typed_by_owner(row):
            rows.append({'type': kind, 'timestamp': timestamp, 'human': isinstance(row.get('origin'), dict)})
    return {'rows': rows, 'has_origin': has_origin, 'last': last, 'first': first, 'cwd': cwd,
            'skipped': stats['skipped']}


@functools.lru_cache(maxsize=None)
def file_usage(path):
    """usage_rows(path) through the parse cache."""
    return cached(path, 'usage', lambda: usage_rows(path))


def usage_of(main_paths, sub_paths, since, now, stats, keep=None, shared=None, phase_of=None):
    """The Transcripts of main_paths, read with no role, and of the subagent transcripts sub_paths, read with the role
    their .meta.json names; keep, shared and phase_of as Transcripts takes them."""
    tr = Transcripts(since, now, keep, shared, phase_of)
    for path in main_paths:
        tr.read_file(path, None, stats)
    for path in sub_paths:
        tr.read_file(path, field(subagent_meta(path), 'agentType', str, '') or 'subagent', stats)
    return tr


def in_window(timestamp, start, end):
    """Whether a transcript timestamp is in the window (start, end], a start of None being open, compared as strings."""
    return (start is None or timestamp > start) and timestamp <= end


def window_owner(windows, timestamp):
    """The owner of the first of windows, (start, end, owner) sorted by end then by record index, whose (start, end]
    holds timestamp; None when none does or timestamp is not a string."""
    if not isinstance(timestamp, str):
        return None
    return next((owner for start, end, owner in windows if in_window(timestamp, start, end)), None)


def event_key(event):
    """What identifies an event across transcripts: its time, kind and value, a dispatch by its tool_use id. A forked
    session copies its parent's rows, timestamps included, so the copies share a key."""
    timestamp, kind, value = event
    return timestamp, kind, value[0] if kind == 'agent' else value


def unit_notes(tr):
    """The advisor notes of one Transcripts: how many, how many changed something, and the consultations with none."""
    return {'notes': tr.notes, 'changed': tr.changed, 'missing': tr.missing}


def unit_advisor(tr):
    """The advisor facts advisor_facts reads, from one Transcripts: calls, cost, tokens, calls by role, failed calls
    by error code and the advisor and Fable dollars by week."""
    return {'calls': sum(n for n, _ in tr.by_role.values()), 'cost': tr.advisor_cost, 'tokens': dict(tr.tokens),
            'by_role': {label: {'calls': calls, 'flagged': flagged}
                        for label, (calls, flagged) in sorted(tr.by_role.items())},
            'failed': dict(sorted(tr.failed.items())),
            'weeks': [{'week': week.date().isoformat(), 'advisor': tr.week_advisor.get(week, 0.0),
                       'fable': tr.week_fable.get(week, 0.0)}
                      for week in sorted(set(tr.week_advisor) | set(tr.week_fable))]}


def sum_advisor(advisors):
    """unit_advisor's facts summed over advisors, each in its shape."""
    total = {'calls': 0, 'cost': 0.0, 'tokens': {label: 0 for label, _ in TOKENS}, 'by_role': {}, 'failed': {}}
    weeks = {}
    for adv in advisors:
        total['calls'] += adv['calls']
        total['cost'] += adv['cost']
        for label, n in adv['tokens'].items():
            total['tokens'][label] += n
        for label, role in adv['by_role'].items():
            total['by_role'].setdefault(label, {'calls': 0, 'flagged': role['flagged']})['calls'] += role['calls']
        for code, n in adv['failed'].items():
            total['failed'][code] = total['failed'].get(code, 0) + n
        for w in adv['weeks']:
            row = weeks.setdefault(w['week'], {'week': w['week'], 'advisor': 0.0, 'fable': 0.0})
            row['advisor'] += w['advisor']
            row['fable'] += w['fable']
    return dict(total, by_role=dict(sorted(total['by_role'].items())), failed=dict(sorted(total['failed'].items())),
                weeks=[weeks[week] for week in sorted(weeks)])


def read_transcripts(root, since, now, stats):
    # Agent subagents sit in <session>/subagents, workflow subagents deeper, as subagent_transcripts finds them.
    pattern = os.path.join(glob.escape(root), '*', '*', 'subagents', '**', '*.jsonl')
    return usage_of(main_transcripts(root), sorted(glob.glob(pattern, recursive=True)), since, now, stats)


def read_json_dict(path):
    """The JSON object in the file at path, or {} when it is missing, unreadable, nested too deep or not an object."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError, RecursionError):  # open() raises ValueError for a path with a NUL in it.
        return {}
    return data if isinstance(data, dict) else {}


def write_atomic(path, text):
    """Write text to path through a temp file in its folder, so a reader sees the old file or the new one, never part of
    one. Raises OSError on failure, and creates no folder."""
    fd, temp = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
        os.replace(temp, path)
    except OSError:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def cached(path, part, compute):
    """compute(), kept in the parse cache under the file at path's size and time, so a later call while the file is
    unchanged reads it back. With no cache folder, or a file it can't stat, it only computes; a cache it can't write
    changes nothing. The stat comes first, so a file that grows while compute reads it is read again next time."""
    if CACHE_DIR is None:
        return compute()
    try:
        st = os.stat(path)
    except OSError:
        return compute()
    key = {'path': path, 'size': st.st_size, 'mtime_ns': st.st_mtime_ns, 'version': CACHE_VERSION}
    folder = os.path.join(CACHE_DIR, 'transcripts')
    entry = os.path.join(folder, f'{hashlib.sha256(os.fsencode(path)).hexdigest()[:16]}-{part}.json')
    stored = read_json_dict(entry)
    if 'value' in stored and all(stored.get(k) == v for k, v in key.items()):
        return stored['value']
    value = compute()
    try:
        os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
        os.makedirs(folder, mode=0o700, exist_ok=True)
        write_atomic(entry, json.dumps(dict(key, value=value)))
    except (OSError, RecursionError):  # A value nested too deep to write is computed again next time.
        pass
    return value


def subagent_meta(path):
    """The .meta.json beside a subagent transcript, or {}."""
    return read_json_dict(path[:-len('.jsonl')] + '.meta.json')


def read_category(obj, ids):
    """obj's category when it is a string among ids, else None."""
    value = field(obj, 'category', str, None)
    return value if value in ids else None


def read_steps(rec):
    """The record's steps, cleaned: a step that isn't a dict of a known kind is dropped, other bad values read as
    absent."""
    steps = []
    for step in field(rec, 'steps', list, []):
        if not isinstance(step, dict) or step.get('step') not in STEP_KINDS:
            continue
        ids = area_ids(step['step'])
        strengths = field(step, 'strengths', list, None) if step['step'] in STRENGTH_STEPS else None
        if strengths is not None:
            strengths = [{'category': read_category(s, ids), 'summary': field(s, 'summary', str, None)}
                         for s in strengths if isinstance(s, dict)]
        findings = [{'id': field(f, 'id', str, None), 'severity': field(f, 'severity', str, None),
                     'category': read_category(f, ids),
                     'validity': f.get('validity') if f.get('validity') in VALIDITY else None,
                     'kind': field(f, 'kind', str, None), 'summary': field(f, 'summary', str, None),
                     'outcome': f.get('outcome') if f.get('outcome') in OUTCOMES else None,
                     'reason': field(f, 'reason', str, None), 'closed_round': field(f, 'closed_round', int, None),
                     'verdicts': [v for v in field(f, 'verdicts', list, []) if isinstance(v, str)]}
                    for f in field(step, 'findings', list, []) if isinstance(f, dict)]
        steps.append({'step': step['step'], 'task': field(step, 'task', int, None),
                      'wave': field(step, 'wave', str, None),
                      'round': field(step, 'round', int, None), 'agent': field(step, 'agent', str, None),
                      'model': field(step, 'model', str, None), 'effort': field(step, 'effort', str, None),
                      'agent_id': field(step, 'agent_id', str, None),
                      'counts': {k: num(v) for k, v in field(step, 'counts', dict, {}).items()},
                      'strengths': strengths, 'findings': findings})
    return steps


def read_waves(rec):
    """The record's waves, cleaned: an entry that isn't a dict with a string wave is dropped, other bad values read as
    absent."""
    return [{'wave': w['wave'],
             'tasks': [t for t in field(w, 'tasks', list, []) if isinstance(t, int) and not isinstance(t, bool)],
             'conflicts': num(w.get('conflicts'))}
            for w in field(rec, 'waves', list, []) if isinstance(w, dict) and isinstance(w.get('wave'), str)]


def read_offers(rec):
    """The record's offers, cleaned: an entry that isn't a dict with a string skill and a known outcome is dropped, a
    bad reason reads as absent."""
    return [{'skill': o['skill'], 'outcome': o['outcome'], 'reason': field(o, 'reason', str, None)}
            for o in field(rec, 'offers', list, [])
            if isinstance(o, dict) and isinstance(o.get('skill'), str) and o.get('outcome') in OFFER_OUTCOMES]


def read_runs(log, stats):
    runs = []
    for rec in read_jsonl(log, stats):
        raw = dict(rec)
        t = parse_time(rec.get('date'))
        # Clean each record once, so the report lines below read plain fields.
        rec['_raw'] = raw
        rec['_time'] = t
        rec['_started'] = parse_time(rec.get('started'))
        rec['schema'] = field(rec, 'schema', int, 1)
        rec['steps'] = read_steps(rec)
        rec['waves'] = read_waves(rec)
        rec['offers'] = read_offers(rec)
        rec['categories'] = field(rec, 'categories', int, None)
        rec['advisor'] = rec.get('advisor') is True
        findings = field(rec, 'final_findings', dict, {})
        rec['final_findings'] = {k: num(findings.get(k)) for k in FINDINGS}
        rec['failing_test'] = [item for item in field(rec, 'failing_test', list, [])
                               if isinstance(item, dict) and item.get('result') in TEST_RESULTS]
        # A record without "ran" (written before it existed) is unknown, never "no".
        simplify = field(rec, 'simplify', dict, {})
        rec['simplify'] = {'ran': simplify.get('ran') if simplify.get('ran') in SIMPLIFY_RAN else None,
                           'applied': num(simplify.get('applied')), 'skipped': num(simplify.get('skipped'))}
        rec['fix_growth'] = field(rec, 'fix_growth', dict, None)
        runs.append(rec)
    return runs


def git(project, *args):
    p = subprocess.run(['git', '-C', project, *args], capture_output=True, text=True, timeout=GIT_TIMEOUT)
    if p.returncode != 0:
        raise OSError(p.stderr.strip())
    return p.stdout


def later_fixes(run):
    """Commits whose subject starts with "fix" (any case) touching the run's files within 14 days after it."""
    project, base, head, t = run.get('project'), run.get('base'), run.get('head'), run.get('_time')
    if not all(isinstance(v, str) and v for v in (project, base, head)) or t is None or not os.path.isdir(project):
        return []
    try:
        files = [f for f in git(project, 'diff', '--name-only', '-z', base, head).split('\0') if f]
        if not files:
            return []
        until = t + datetime.timedelta(days=FIX_WINDOW_DAYS)
        out = git(project, 'log', '--all', '-i', '--grep=^fix', '--since=' + t.isoformat(),
                  '--until=' + until.isoformat(), '--format=%h%x09%cI%x09%s', '--', *[':(literal)' + f for f in files])
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.split('\t', 2) for line in out.splitlines() if line.count('\t') >= 2]


def git_range(project, base, head, command, *args):
    """Output of git command base..head *args in the project, or None when the folder, the range or git is unusable."""
    if (not all(isinstance(v, str) and v for v in (project, base, head)) or not os.path.isdir(project)
            or base.startswith('-') or head.startswith('-')):
        return None
    try:
        return git(project, command, f'{base}..{head}', *args)
    except (OSError, subprocess.SubprocessError):
        return None


def conventions_added(project, base, head):
    """Non-blank lines added under ## Code conventions in the project's root CLAUDE.md over base..head, or None."""
    # Whole-file context, so the heading above each added line is in the diff.
    out = git_range(project, base, head, 'diff', '--no-color', '--no-ext-diff', '--unified=1000000', '--', 'CLAUDE.md')
    if out is None:
        return None
    heading, added, body = None, 0, False
    for line in out.splitlines():
        body = body or line.startswith('@@')
        if not body or line[:1] not in ('+', ' '):
            continue
        text = line[1:].rstrip()
        if re.match(r'#{1,2} ', text):
            heading = text
        elif heading == CONVENTIONS and line[0] == '+' and text:
            added += 1
    return added


def recompute_fix_growth(project, base, head):
    """Lines added and removed over base..head by fix-round commits and by the other commits, or None."""
    out = git_range(project, base, head, 'log', '--no-color', '--numstat', '--format=%x00%s')
    if out is None:
        return None
    growth = dict.fromkeys(('task_added', 'task_removed', 'fix_added', 'fix_removed'), 0)
    kind = 'task'
    for line in out.splitlines():
        if line.startswith('\0'):
            kind = 'fix' if line[1:].startswith(FIX_ROUND_SUBJECT) else 'task'
        elif line.count('\t') >= 2:
            added, removed, _ = line.split('\t', 2)
            growth[kind + '_added'] += int(added) if added.isdigit() else 0
            growth[kind + '_removed'] += int(removed) if removed.isdigit() else 0
    return growth


def is_final_review(tool_input):
    subagent_type = tool_input.get('subagent_type')
    if subagent_type == 'final-reviewer':
        return True
    description = tool_input.get('description')
    return (subagent_type in UNNAMED_TYPES and isinstance(description, str)
            and bool(FINAL_REVIEW_DESCRIPTION.search(description)) and not NOT_FINAL_REVIEW.search(description))


def launches_final_review(kind, value, launched):
    """Whether a transcript event of kind and value launched a final review: the event that ends a simplify span."""
    return kind == 'agent' and value[0] in launched and is_final_review(value[1])


def is_plan_review(tool_input):
    subagent_type = tool_input.get('subagent_type')
    if subagent_type == 'plan-reviewer':
        return True
    description = tool_input.get('description')
    return (subagent_type in UNNAMED_TYPES and isinstance(description, str)
            and bool(PLAN_REVIEW_DESCRIPTION.search(description)) and not NOT_FINAL_REVIEW.search(description))


def is_plan_simplify(tool_input):
    subagent_type = tool_input.get('subagent_type')
    if subagent_type == 'plan-simplifier':
        return True
    description = tool_input.get('description')
    return (subagent_type in UNNAMED_TYPES and bool(tool_input.get('model')) and isinstance(description, str)
            and bool(PLAN_SIMPLIFY_DESCRIPTION.search(description)) and not NOT_FINAL_REVIEW.search(description))


def row_calls_simplify(row):
    content = row.get('message', {}).get('content')
    if row.get('type') == 'user':
        if isinstance(content, str):
            return bool(SIMPLIFY_COMMAND.match(content))
        return any(block.get('type') == 'text' and SIMPLIFY_COMMAND.match(block.get('text', '')) for block in content or [])
    if row.get('type') == 'assistant':
        return any(block.get('type') == 'tool_use' and block.get('name') == 'Skill'
                   and block.get('input', {}).get('skill') == 'simplify' for block in content or [])
    return False


def user_texts(content):
    """The texts of a user row's content: the string itself, or its text blocks when none is a tool_result."""
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list) or any(block.get('type') == 'tool_result' for block in content):
        return []
    return [block['text'] for block in content if block.get('type') == 'text' and isinstance(block.get('text'), str)]


def launched_ids(row, ids):
    """(id, agent id) for the ids, among ids, that this row reports as launched: their tool_result is not an error.
    The agent id is the row's toolUseResult agentId, or None.

    Only is_error decides this, never the result's text, which may be a review that says "not found" or "denied".
    """
    content = row.get('message', {}).get('content')
    if row.get('type') != 'user' or not isinstance(content, list):
        return []
    agent_id = field(row, 'toolUseResult', dict, {}).get('agentId')
    return [(block['tool_use_id'], agent_id if isinstance(agent_id, str) else None) for block in content
            if block.get('type') == 'tool_result' and block.get('tool_use_id') in ids and block.get('is_error') is not True]


def result_text(block):
    content = block.get('content')
    if isinstance(content, list):
        return ' '.join(c.get('text', '') for c in content if isinstance(c, dict) and isinstance(c.get('text'), str))
    return content if isinstance(content, str) else ''


@functools.lru_cache(maxsize=None)
def transcript_events(path):
    """scan_events(path) through the parse cache, with its events as tuples again."""
    events, launched, startup = cached(path, 'events', lambda: scan_events(path))
    return [(t, k, tuple(v) if k == 'agent' else v) for t, k, v in events], launched, startup


def scan_events(path):
    """Read a transcript once and return (events, {launched id: its agent id or None}, has the startup block).

    Events are (timestamp, kind, value) in file order: ('simplify', None), ('agent', (id, input)), ('not_found',
    agent type), ('refused', GUARD_REASONS key), ('write', Bash command naming the run log), ('skill', name) for a
    skill's load, the row that follows a Skill call, a typed command or an agent's start with its preloaded skills,
    or the session-start hook's using-superpowers block (SUPERPOWERS_TAG in a SessionStart hook row, so once per
    start, clear or compaction),
    ('typed', skill) for a typed row matching a TYPED_TRIGGERS pattern, ('command', name) for a typed row's slash
    command, its first text that COMMAND matches, ('wrote', path) for a Write or Edit of a spec or plan, and
    ('title', text) for a custom-title row. A row with no timestamp takes the previous row's.
    Only lines holding one of HEALTH_NEEDLES are parsed, plus the result lines of dispatches still waiting for one.
    """
    events, launched, startup, timestamp = [], {}, False, ''
    pending = {}  # tool_use id -> its bytes, for Agent dispatches with no result yet
    try:
        f = open(path, 'rb')
    except OSError:
        return events, launched, startup
    with f:
        for line in f:
            if not any(n in line for n in HEALTH_NEEDLES) and not any(needle in line for needle in pending.values()):
                continue
            try:
                row = json.loads(line)
                if isinstance(row.get('timestamp'), str):
                    timestamp = row['timestamp']
                kind, content = row.get('type'), row.get('message', {}).get('content')
                # Prompts and briefs can quote the tag; only the hook's attachment row shows the block was injected.
                startup = startup or (kind == 'attachment' and STARTUP_TAG in line)
                if kind == 'attachment' and SUPERPOWERS_TAG in line:
                    # A SessionStart hook row carries the block; a queued prompt or another hook can only quote it.
                    hook = field(field(row, 'attachment', dict, {}), 'hookName', str, '')
                    if hook.startswith('SessionStart'):
                        events.append((timestamp, 'skill', 'using-superpowers'))
                if kind == 'custom-title' and isinstance(row.get('customTitle'), str):
                    events.append((timestamp, 'title', row['customTitle']))
                if row_calls_simplify(row):
                    events.append((timestamp, 'simplify', None))
                # Only the load's own row counts, never a tool result quoting it or an attachment re-listing it.
                # An agent's preloaded skill puts a command-message block before the skill's text.
                texts = user_texts(content) if kind == 'user' else []
                skill_text = next((text for text in texts if text.startswith(SKILL_LOADED)), None)
                if skill_text is not None:
                    folder = skill_text[len(SKILL_LOADED):].split('\n', 1)[0].strip().rstrip('/')
                    events.append((timestamp, 'skill', os.path.basename(folder)))
                if texts and Transcripts.typed_by_owner(row):
                    found = [skill for pattern, skill in TYPED_TRIGGERS for text in texts
                             if (pattern.match if '<command-name>' in pattern.pattern else pattern.search)(text)]
                    events.extend((timestamp, 'typed', skill) for skill in dict.fromkeys(found))
                    command = next((m.group('name') for m in map(COMMAND.match, texts) if m), None)
                    if command is not None:
                        events.append((timestamp, 'command', command))
                if kind == 'assistant':
                    for block in content or []:
                        inp = block.get('input')
                        if block.get('type') != 'tool_use' or not isinstance(inp, dict):
                            continue
                        if block.get('name') == 'Agent' and isinstance(block.get('id'), str):
                            pending[block['id']] = block['id'].encode()
                            kept = {k: field(inp, k, str, None) for k in ('subagent_type', 'model', 'effort',
                                                                           'description')}
                            events.append((timestamp, 'agent', (block['id'], kept)))
                        elif block.get('name') == 'Bash' and RUN_LOG in str(inp.get('command')):
                            events.append((timestamp, 'write', inp['command']))
                        elif (block.get('name') in ('Write', 'Edit') and isinstance(inp.get('file_path'), str)
                              and any(folder in inp['file_path'] for folder in SPECS_AND_PLANS.values())):
                            events.append((timestamp, 'wrote', inp['file_path']))
                elif kind == 'user' and isinstance(content, list):
                    launched.update(launched_ids(row, pending))
                    for block in content:
                        if block.get('type') != 'tool_result':
                            continue
                        # Only an Agent call's own error says its agent type was not found or the guard refused it.
                        dispatch = pending.pop(block.get('tool_use_id'), None)
                        errored = dispatch is not None and block.get('is_error') is True
                        text = result_text(block) if errored else ''
                        found = NOT_FOUND.search(text)
                        if found:
                            events.append((timestamp, 'not_found', found.group(1)))
                        events.extend((timestamp, 'refused', key) for key, reason in GUARD_REASONS.items()
                                      if reason in text)
            except (ValueError, AttributeError, TypeError, RecursionError):  # A torn or odd line says nothing.
                continue
    return events, launched, startup


def transcript_title(path):
    """The transcript's last custom title, or None when it has none."""
    return next((value for _, kind, value in reversed(transcript_events(path)[0]) if kind == 'title'), None)


def main_transcripts(projects_root):
    return sorted(glob.glob(os.path.join(glob.escape(projects_root), '*', '*.jsonl')))


def in_range(timestamp, since, now):
    """Whether a transcript timestamp is in since..now; a row without one counts."""
    t = parse_time(timestamp)
    return t is None or (since is None or t >= since) and t <= now


def refusal_counts(events):
    """How many dispatches the dispatch guard refused among events, by GUARD_REASONS key."""
    counts = {}
    for _, kind, key in events:
        if kind == 'refused':
            counts[key] = counts.get(key, 0) + 1
    return counts


def guard_refusals(projects_root, since, now):
    """How many dispatches the dispatch guard refused in the main transcripts, by GUARD_REASONS key, in since..now."""
    return refusal_counts([e for path in main_transcripts(projects_root) for e in transcript_events(path)[0]
                           if in_range(e[0], since, now)])


def run_spans(projects_root, records, now):
    """Map each record's index in records, the whole run log, to (transcript_path, start, end), as timestamps.

    end is the main-transcript Bash row that wrote the run's record. For a record with a "started" time, it is the
    first whose "started" value is the record's, as written, in JSON, Python or shell-escaped quotes, and start is that
    time. Otherwise it is the first whose "head" value is the run's head, since the next run's record names that head
    again as its base, perhaps in another session, and start is the previous record-writing row in that transcript, one
    whose "head" value is any record's head, or None for the transcript's start. So a run a report leaves out still
    ends where its record was written, and a command that mentions the log without a record's "head" or "started"
    value ends nothing. A run whose record write isn't found has no span, and a span ends at now at the latest, since
    rows after now are ignored.
    """
    heads = [(i, re.compile(r'"head"\s*:\s*"' + re.escape(r['head'])))
             for i, r in enumerate(records) if isinstance(r.get('head'), str) and r['head']]
    starts = [(i, re.compile(r'"started"\s*:\s*"' + re.escape(r['started']) + '"'), stamp(r['_started']))
              for i, r in enumerate(records) if r['_started'] is not None]
    by_start = {i for i, _, _ in starts}
    spans, last = {}, stamp(now)
    if not heads and not starts:
        return spans
    for path in main_transcripts(projects_root):
        start = None
        for timestamp, kind, command in transcript_events(path)[0]:
            if kind != 'write':
                continue
            # A record built in a Python dict or an echo "..." string quotes its values with ' or \".
            command = command.replace('\\"', '"').replace("'", '"')
            for i, written, started in starts:
                if written.search(command):
                    spans.setdefault(i, (path, started, min(timestamp, last)))
            owners = [i for i, written in heads if written.search(command)]
            for i in owners:
                if i not in by_start:
                    spans.setdefault(i, (path, start, min(timestamp, last)))
            if owners:
                start = timestamp
    return spans


def subagent_transcripts(path):
    """The subagent transcripts of the main transcript path, workflow subagents included."""
    folder = glob.escape(path[:-len('.jsonl')])
    return sorted(glob.glob(os.path.join(folder, 'subagents', '**', '*.jsonl'), recursive=True))


@functools.lru_cache(maxsize=None)
def span_events(path, start, end):
    """(events, {launched id: its agent id or None}) of the rows timed in (start, end] of the main transcript path and
    its subagents."""
    launched, per_file = {}, []
    for p in [path] + subagent_transcripts(path):
        events, file_launched, _ = transcript_events(p)
        launched.update(file_launched)
        per_file.append([e for e in events if in_window(e[0], start, end)])
    # Rows from several transcripts merge by timestamp, as in the dispatch hook.
    return list(heapq.merge(*per_file, key=lambda e: e[0])), launched


@functools.lru_cache(maxsize=None)
def review_report(subagent_path):
    """scan_report(subagent_path) through the parse cache."""
    return cached(subagent_path, 'report', lambda: scan_report(subagent_path))


def scan_report(subagent_path):
    """The input.message of the transcript's last SubagentHandback tool_use, or failing that its last assistant text."""
    handback, text = None, ''
    for row in read_jsonl(subagent_path, {'skipped': 0}):
        msg = row.get('message')
        if row.get('type') != 'assistant' or not isinstance(msg, dict):
            continue
        for block in field(msg, 'content', list, []):
            if not isinstance(block, dict):
                continue
            message = field(block, 'input', dict, {}).get('message')
            if block.get('type') == 'tool_use' and block.get('name') == 'SubagentHandback' and isinstance(message, str):
                handback = message
            elif block.get('type') == 'text' and isinstance(block.get('text'), str):
                text = block['text']
    return text if handback is None else handback


@functools.lru_cache(maxsize=None)
def span_reports(path, start, end):
    """(dispatch input, subagent transcript path, report, launch time, agent id) for each dispatch launched in the span,
    in order; the path and report are None when its subagent transcript isn't there. The agent id is the launch's own,
    else the subagent transcript's agent-<id>.jsonl name, else None."""
    events, launched = span_events(path, start, end)
    files = {}
    for p in subagent_transcripts(path):
        tid = subagent_meta(p).get('toolUseId')
        if isinstance(tid, str):
            files[tid] = p
    reports = []
    for time, kind, value in events:
        if kind == 'agent' and value[0] in launched:
            sub = files.get(value[0])
            name = os.path.basename(sub) if sub else ''
            agent_id = launched[value[0]] or (name[len('agent-'):-len('.jsonl')] if name.startswith('agent-') else None)
            reports.append((value[1], sub, review_report(sub) if sub else None, time, agent_id))
    return reports


@functools.lru_cache(maxsize=None)
def span_dispatches(path, start, end):
    """{'inp', 'sub', 'report', 'time', 'id', 'kind'} for each dispatch launched in the span, in order: its input,
    subagent transcript path, report, launch time and agent id as span_reports gives them, and its step kind."""
    dispatches, after_final = [], False
    for inp, sub, report, time, agent_id in span_reports(path, start, end):
        kind = step_kind(inp, after_final)
        after_final = after_final or kind == 'final_review'
        dispatches.append({'inp': inp, 'sub': sub, 'report': report, 'time': time, 'id': agent_id, 'kind': kind})
    return dispatches


@functools.lru_cache(maxsize=None)
def sub_usage(sub):
    """The Transcripts of one subagent transcript, read once."""
    return usage_of([], [sub], None, None, {'skipped': 0})


def is_review(tool_input):
    """A task or final review, whose report ends with a Findings: line."""
    subagent_type, description = tool_input.get('subagent_type'), tool_input.get('description')
    return (subagent_type == 'task-reviewer' or is_final_review(tool_input)
            or subagent_type in UNNAMED_TYPES and isinstance(description, str) and bool(TASK_REVIEW.search(description)))


def step_kind(tool_input, after_final):
    """The step kind of a launched dispatch, or None; a fix-check is a final_fix_check once a final review launched."""
    if is_final_review(tool_input):
        return 'final_review'
    if is_plan_review(tool_input):
        return 'plan_review'
    if is_plan_simplify(tool_input):
        return 'plan_simplify'
    if is_review(tool_input):
        return 'task_review'
    if tool_input.get('subagent_type') == 'fix-checker':
        return 'final_fix_check' if after_final else 'fix_check'
    return None


def step_ties(run, dispatches):
    """Each step's tie by agent id, in record order: ('tied', dispatch index) when no other step carries its agent_id
    and exactly one dispatch of the step's kind has that id; else why it has none: ('missing', None) with no agent_id,
    ('duplicate', None) when another step carries it, ('unknown', None) when no dispatch has it, and ('mismatch', the
    index of a dispatch with the id, of another kind where there is one)."""
    ids = [s['agent_id'] for s in run['steps']]
    ties = []
    for step in run['steps']:
        agent_id = step['agent_id']
        with_id = [] if agent_id is None else [j for j, d in enumerate(dispatches) if d['id'] == agent_id]
        of_kind = [j for j in with_id if dispatches[j]['kind'] == step['step']]
        if agent_id is None:
            ties.append(('missing', None))
        elif ids.count(agent_id) > 1:
            ties.append(('duplicate', None))
        elif len(of_kind) == 1:
            ties.append(('tied', of_kind[0]))
        elif not with_id:
            ties.append(('unknown', None))
        else:
            ties.append(('mismatch', next((j for j in with_id if j not in of_kind), with_id[0])))
    return ties


def ties_by_id(run, dispatches):
    """Step index to dispatch index, for each step step_ties ties."""
    return {i: j for i, (reason, j) in enumerate(step_ties(run, dispatches)) if reason == 'tied'}


def dispatch_steps(run, dispatches):
    """The step each dispatch's warnings name, by dispatch index: the record step ties_by_id ties it to, else its kind
    alone, else None for a dispatch of no step kind."""
    tied = {j: run['steps'][i] for i, j in ties_by_id(run, dispatches).items()}
    return [tied.get(j) or ({'step': d['kind']} if d['kind'] else None) for j, d in enumerate(dispatches)]


def tie_steps(run, dispatches):
    """ties_by_id, plus each step with no agent_id whose kind has exactly one step in the record and one dispatch in
    dispatches. A step is tied only when its dispatch is certain, never by order; simplify steps have no dispatch."""
    ties = ties_by_id(run, dispatches)
    kinds = [s['step'] for s in run['steps']]
    for i, step in enumerate(run['steps']):
        same = [j for j, d in enumerate(dispatches) if d['kind'] == step['step']]
        if step['agent_id'] is None and kinds.count(step['step']) == 1 and len(same) == 1:
            ties[i] = same[0]
    return ties


def count_line(report, name):
    """The key=N pairs on the report's last name: line, or None when it has none."""
    lines = re.findall(r'^[`*\s]*' + re.escape(name) + r':(.*)$', report, re.M)
    if not lines:
        return None
    return {k: int(v) for k, v in re.findall(r'(\w+)=(\d+)', lines[-1])}


def findings_line(report):
    """The counts on the report's last Findings: line, or None when it has none."""
    counts = count_line(report, 'Findings')
    return None if counts is None else {k: counts.get(k, 0) for k in FINDINGS}


def findings_text(counts):
    return ' '.join(f'{k}={v}' for k, v in counts.items())


def pairs(counts):
    """A dict as findings_text gives it, "none" when empty, or None for null."""
    return None if counts is None else findings_text(counts) or 'none'


def asks_for_conventions(report):
    """Whether the report has an Important finding that mentions ## Code conventions: on a line starting with
    Important, or under an Important heading up to the next severity or heading."""
    severity = None
    for line in report.splitlines():
        m = SEVERITY.match(line)
        if m or line.startswith('#'):
            severity = m.group(1).lower() if m else None
        if severity == 'important' and CONVENTIONS in line:
            return True
    return False


def count_not_proportionate(path, start, end):
    """"fixed, not proportionate" verdicts in the span's fix-checker reports."""
    return sum(report.lower().count(NOT_PROPORTIONATE) for inp, _, report, _, _ in span_reports(path, start, end)
               if report is not None and inp.get('subagent_type') == 'fix-checker')


# Copied from tests/test_workflow_files.py, since scripts can't import tests: keep them the same.
def frontmatter(text):
    """Reads the simple YAML used here: `key: value` lines and `- item` lists under a key with no value."""
    lines = text.splitlines()
    if not lines or lines[0] != '---':
        raise ValueError('no frontmatter')
    end = lines.index('---', 1)
    fields, key = {}, None
    for line in lines[1:end]:
        stripped = line.strip()
        if stripped.startswith('- ') and key is not None:
            fields[key].append(stripped[2:].strip())
        elif ':' in line and not line.startswith(' '):
            key, value = line.split(':', 1)
            key, value = key.strip(), value.strip()
            fields[key] = value if value else []
        elif stripped:
            raise ValueError('unexpected frontmatter line: ' + line)
    return fields, '\n'.join(lines[end + 1:])


def read_pins(agents_dir):
    """Each agent file's model, effort and skills, by agent name; {} for a missing folder. A file whose frontmatter
    doesn't read is left out."""
    pins = {}
    for path in sorted(glob.glob(os.path.join(glob.escape(agents_dir), '*.md'))):
        try:
            with open(path) as f:
                fields = frontmatter(f.read())[0]
        except (OSError, ValueError):
            continue
        pins[os.path.basename(path)[:-len('.md')]] = {
            'model': field(fields, 'model', str, None), 'effort': field(fields, 'effort', str, None),
            'skills': field(fields, 'skills', list, [])}
    return pins


def ran_on(tr):
    """The model and effort a dispatch's Transcripts ran on: the model with the most output tokens, and its most
    frequent effort; None for either when the transcript doesn't show it."""
    if not tr.models:
        return None, None
    model = max(tr.models, key=lambda m: tr.models[m]['output'])
    efforts = tr.efforts.get(model, {})
    return model, max(efforts, key=efforts.get) if efforts else None


def on_model(model, alias):
    """Whether the model ID is the one alias names, as an alias such as "opus" or as a model ID."""
    return model.startswith(alias) or model.startswith('claude-' + alias)


def off_request(model, effort, want_model, want_effort):
    """Whether the model and effort a transcript shows differ from the ones wanted; an unknown side never differs."""
    return bool(model and want_model and not on_model(model, want_model)
                or effort and want_effort and effort != want_effort)


def missing_required(run, events):
    """The skills REQUIRED_SKILLS asks of the run's path that no event loaded."""
    loaded = {value for _, kind, value in events if kind == 'skill'}
    return [skill for path, skill in REQUIRED_SKILLS if run.get('path') == path and skill not in loaded]


def agent_skills(sub):
    """The skills a subagent transcript loaded, in order."""
    return [value for _, kind, value in transcript_events(sub)[0] if kind == 'skill']


def unloaded_skills(pin, sub):
    """The skills an agent file lists that its subagent transcript never loaded, by the part of the name after any
    plugin prefix."""
    own = set(agent_skills(sub))
    return [skill for skill in pin['skills'] if skill.split(':')[-1] not in own]


def missing_skills(run, events, dispatches, pins):
    """(skill, dispatch) for each skill REQUIRED_SKILLS asks of the run that no event loaded, with dispatch None, then
    for each skill a launched agent's pin lists that its subagent transcript never loaded."""
    missing = [(skill, None) for skill in missing_required(run, events)]
    for d in dispatches:
        pin = pins.get(d['inp'].get('subagent_type'))
        if pin is not None and d['sub'] is not None:
            missing += [(skill, d) for skill in unloaded_skills(pin, d['sub'])]
    return missing


def problem(kind, text, step=None, skill=None, agent=None):
    """A WARN before its prefix: its sentence, its kind and the step, skill and agent it names. step is a record step,
    or {'step': kind} for a step kind alone, and keeps only its step, task, round and wave."""
    if step is not None:
        step = {k: step.get(k) for k in ('step', 'task', 'round', 'wave')}
    return {'kind': kind, 'text': text, 'step': step, 'skill': skill, 'agent': agent}


def run_ref(r):
    """The date, project and branch that name a run record."""
    return {key: field(r, key, str, None) for key in ('date', 'project', 'branch')}


def warning(prob, run=None, session=None, label=None):
    """A problem as a WARN: its full line, after the run's label when it has one, with the run or the session
    transcript path it belongs to."""
    return dict(prob, text=f"WARN: {label}: {prob['text']}" if label else f"WARN: {prob['text']}", run=run,
                session=session)


def texts(warnings):
    return [w['text'] for w in warnings]


def check_steps(run, dispatches, events, pins):
    """Problems in a schema 2 record's steps against its window's events and its span_dispatches."""
    steps = {kind: [s for s in run['steps'] if s['step'] == kind] for kind in STEP_KINDS}
    launched = {kind: [d for d in dispatches if d['kind'] == kind] for kind in STEP_KINDS}
    launched['simplify'] = [e for e in events if e[1] == 'simplify']
    problems = [problem('step_count_differs',
                        f'record has {len(steps[kind])} {kind} steps, transcript launched {len(launched[kind])}',
                        step={'step': kind})
                for kind in STEP_KINDS if len(steps[kind]) != len(launched[kind])]
    nth = [sum(s['step'] == step['step'] for s in run['steps'][:i + 1]) for i, step in enumerate(run['steps'])]
    tied = ties_by_id(run, dispatches)
    if run['schema'] >= 4:
        # Each step but simplify that step_ties leaves untied gets one WARN saying why; a shared id gets one for all the
        # steps carrying it.
        shared = set()
        for i, (step, (reason, j)) in enumerate(zip(run['steps'], step_ties(run, dispatches))):
            kind, agent_id = step['step'], step['agent_id']
            if kind == 'simplify' or reason == 'tied' or agent_id in shared:
                continue
            if reason == 'missing':
                problems.append(problem('step_agent_missing', f'{kind} step {nth[i]} has no agent id', step=step))
            elif reason == 'duplicate':
                shared.add(agent_id)
                sharing = ' and '.join(f"{s['step']} step {nth[k]}" for k, s in enumerate(run['steps'])
                                       if s['agent_id'] == agent_id)
                problems.append(problem('step_agent_duplicate', f'{sharing} share agent id {agent_id}', step=step))
            elif reason == 'unknown':
                problems.append(problem('step_agent_unknown',
                                        f"{kind} step {nth[i]}'s agent id {agent_id} matches no launched dispatch",
                                        step=step))
            else:
                problems.append(problem('step_agent_mismatch', f"{kind} step {nth[i]}'s agent id {agent_id} belongs "
                                        f"to a {dispatches[j]['kind'] or 'non-step'} dispatch", step=step,
                                        agent=dispatches[j]['inp'].get('subagent_type')))
    # A schema 4 step is checked against the dispatch ties_by_id gives it, an older one, the n-th step of a kind,
    # against the n-th launched dispatch of that kind.
    for kind, (_, name, keys) in COUNT_LINES.items():
        if run['schema'] >= 4:
            compared = [(nth[i], step, dispatches[tied[i]]) for i, step in enumerate(run['steps'])
                        if i in tied and step['step'] == kind]
        else:
            compared = [(n, step, d) for n, (step, d) in enumerate(zip(steps[kind], launched[kind]), 1)]
        for n, step, d in compared:
            line = count_line(d['report'], name) if d['report'] is not None else None
            if line is not None and any(step['counts'].get(k, 0) != line.get(k, 0) for k in keys):
                problems.append(problem('step_counts_differ',
                                        f"{kind} step {n} counts {findings_text(step['counts'])} differ from its "
                                        f"report's {findings_text(line)}", step=step))
            if (run['schema'] >= 5 and line is not None and 'strengths' in line and step['strengths'] is not None
                    and len(step['strengths']) != line['strengths']):
                problems.append(problem('step_counts_differ', f"{kind} step {n} has {len(step['strengths'])} "
                                        f"strengths, its report's count line says {line['strengths']}", step=step))
    for kind, (count_of, keys) in ADDS_UP.items():
        for n, step in enumerate(steps[kind], 1):
            tally = dict.fromkeys(keys, 0)
            for key in map(count_of, step['findings']):
                tally[key] = tally.get(key, 0) + 1
            if tally != {k: step['counts'].get(k, 0) for k in keys}:
                problems.append(problem('findings_dont_add_up', f"{kind} step {n} findings don't add up to its counts",
                                        step=step))
    if run['schema'] >= 5:
        for i, step in enumerate(run['steps']):
            kind = step['step']
            unlabelled = [f['id'] or '?' for f in step['findings'] if f['category'] is None or f['validity'] is None]
            if unlabelled:
                problems.append(problem('findings_unlabelled', f"{kind} step {nth[i]} findings {', '.join(unlabelled)} "
                                        f"have no category or validity", step=step))
            strengths = step['strengths']
            if kind in STRENGTH_STEPS and (strengths is None or len(strengths) > 3 or any(
                    s['category'] is None or s['summary'] is None for s in strengths)):
                problems.append(problem('strengths_unrecorded', f'{kind} step {nth[i]} strengths are missing, more '
                                        f'than three, or lack a category or summary', step=step))
    verdicts = dict.fromkeys(VERDICTS, 0)
    for step in run['steps']:
        for finding in step['findings']:
            for verdict in finding['verdicts']:
                verdicts[verdict] = verdicts.get(verdict, 0) + 1
    checks = steps['fix_check'] + steps['final_fix_check']
    if verdicts != {k: sum(s['counts'].get(k, 0) for s in checks) for k in VERDICTS}:
        problems.append(problem('verdicts_dont_add_up',
                                "fix-check verdicts in findings don't add up to the fix-check steps' counts"))
    missing = missing_skills(run, events, dispatches, pins)
    problems += [problem('required_skill_missing', f'required skill {skill} did not run', skill=skill)
                 for skill, d in missing if d is None]
    stage = run.get('stage')
    for d, step in zip(dispatches, dispatch_steps(run, dispatches)):
        inp, sub = d['inp'], d['sub']
        agent = inp.get('subagent_type')
        pin = pins.get(agent)
        if pin is None or sub is None:
            continue
        model, effort = ran_on(sub_usage(sub))
        # The overrides the stage allows: an explicit model in stage 3 or Out, effort xhigh for final-reviewer in a
        # risk area, and Opus at xhigh for final-reviewer in Out. A model or effort the transcript doesn't show passes.
        final = agent == 'final-reviewer'
        model_ok = (not model or not pin['model'] or on_model(model, pin['model'])
                    or stage in ('3', 'out') and bool(inp.get('model')))
        effort_ok = (not effort or not pin['effort'] or effort == pin['effort']
                     or final and run.get('risk_area') is True and inp.get('effort') == effort == 'xhigh')
        out_ok = final and stage == 'out' and (model or '').startswith('claude-opus') and effort == 'xhigh'
        if not (model_ok and effort_ok or out_ok):
            problems.append(problem('agent_off_pin', f"{agent} ran on {model}/{effort}, pinned "
                                    f"{pin['model']}/{pin['effort']}: {inp.get('description')}",
                                    step=step, agent=agent))
        problems += [problem('agent_skill_missing', f"{agent} ran without its skill {skill}: {inp.get('description')}",
                             step=step, skill=skill, agent=agent)
                     for skill, owner in missing if owner is d]
    if run.get('risk_area') is True and run.get('path') == 'small':
        problems.append(problem('risk_area_small_path', 'risk area on the Small path'))
    checked = {s['wave'] for s in steps['task_review']}
    problems += [problem('wave_unchecked', f"wave {w['wave']} has no wave check step",
                         step={'step': 'task_review', 'wave': w['wave']})
                 for w in run['waves'] if w['wave'] not in checked]
    if run['schema'] >= 3 and run.get('path') == 'plan':
        offered = {o['skill'] for o in run['offers']}
        problems += [problem('offer_not_recorded', f'no {skill} offer recorded', skill=skill)
                     for skill in REQUIRED_OFFERS if skill not in offered]
    return problems


def check_run(path, start, end, run, pins):
    """Problems shown by the rows timed in (start, end] of the main transcript path and its subagent transcripts, and
    for a schema 2 run its steps against them and against the agents' pins."""
    events, launched = span_events(path, start, end)
    agents = [value[1] for _, kind, value in events if kind == 'agent' and value[0] in launched]
    schema2 = run['schema'] >= 2
    dispatches = span_dispatches(path, start, end)
    ran = run['simplify']['ran']
    problems, since, simplified = [], False, False
    for _, kind, value in events:
        if kind == 'simplify':
            since = simplified = True
        elif launches_final_review(kind, value, launched):
            # When the record says simplify did not run, its own warning covers these.
            if not since and ran != 'no':
                problems.append(problem('final_review_without_simplify',
                                        f"final review without simplify: {value[1].get('description')}",
                                        step={'step': 'final_review'}))
            since = False
    # A schema 2 record's simplify steps are checked against the simplify calls in check_steps instead.
    recorded = None if schema2 else ran
    if recorded == 'no' and simplified:
        problems.append(problem('simplify_record_differs',
                                'record says simplify did not run but the transcript has a simplify call',
                                step={'step': 'simplify'}))
    elif recorded in ('agents', 'inline') and not simplified:
        problems.append(problem('simplify_record_differs',
                                f'record says simplify ran ({recorded}) but the transcript has no simplify call',
                                step={'step': 'simplify'}))
    problems += [problem('agent_not_found', f"agent type '{value}' not found", agent=value)
                 for _, kind, value in events if kind == 'not_found']
    for d, step in zip(dispatches, dispatch_steps(run, dispatches)):
        if d['report'] is None:
            continue
        if schema2 and d['kind'] in COUNT_LINES:
            label, name, _ = COUNT_LINES[d['kind']]
        elif is_review(d['inp']):
            label, name = 'review', 'Findings'
        else:
            continue
        if count_line(d['report'], name) is None:
            problems.append(problem('count_line_missing',
                                    f"{label} report has no {name}: line: {d['inp'].get('description')}",
                                    step=step, agent=d['inp'].get('subagent_type')))
    finals = [findings_line(d['report']) for d in dispatches if d['report'] is not None and d['kind'] == 'final_review']
    if finals and finals[-1] is not None and finals[-1] != run['final_findings']:
        problems.append(problem('final_findings_differ',
                                f"record's final_findings {findings_text(run['final_findings'])} differ from the "
                                f"final review's {findings_text(finals[-1])}", step={'step': 'final_review'}))
    unnamed = [inp for inp in agents if inp.get('subagent_type') in UNNAMED_TYPES]
    problems += [problem('named_role_by_general_purpose', f"named role by general-purpose: {inp.get('description')}",
                         agent=inp.get('subagent_type')) for inp in unnamed
                 if isinstance(inp.get('description'), str) and NAMED_ROLE.search(inp['description'])]
    problems += [problem('model_less_dispatch',
                         f"model-less general-purpose dispatch launched: {inp.get('description')}",
                         agent=inp.get('subagent_type')) for inp in unnamed if not inp.get('model')]
    if not transcript_events(path)[2]:
        problems.append(problem('startup_missing', 'startup guidelines missing from the session'))
    if schema2:
        problems += check_steps(run, dispatches, events, pins)
    return problems


def run_date(run):
    return run['_time'].date().isoformat() if run['_time'] is not None else 'no date'


def check_git(run, final_report):
    """Problems git shows in the run, and whether git was usable for every check that needed it: True, False, or None
    when no check needed git."""
    project, base, head = run.get('project'), run.get('base'), run.get('head')
    problems, results = [], []
    if run['fix_growth'] is not None:
        growth = recompute_fix_growth(project, base, head)
        results.append(growth is not None)
        if growth is not None:
            recorded = num(run['fix_growth'].get('fix_added'))
            off = abs(recorded - growth['fix_added'])
            if off > growth['fix_added'] / 2 and off > FIX_GROWTH_SLACK:
                problems.append(problem('fix_added_differs',
                                        f"record's fix_added {recorded} differs from git's {growth['fix_added']}"))
    if asks_for_conventions(final_report):
        added = conventions_added(project, base, head)
        results.append(added is not None)
        if added == 0:
            problems.append(problem('conventions_not_added',
                                    f'final review asked for a {CONVENTIONS} entry but the branch added none',
                                    step={'step': 'final_review'}))
    return problems, all(results) if results else None


def is_workflow_agent(tool_input):
    """An implementer or a final review, named or general-purpose: the session that dispatches one writes a record."""
    description = tool_input.get('description')
    return (tool_input.get('subagent_type') in WORKFLOW_TYPES or is_final_review(tool_input)
            or tool_input.get('subagent_type') in UNNAMED_TYPES and isinstance(description, str)
            and 'implement' in description.lower())


def unrecorded_sessions(projects_root, spans, now):
    """(transcript path, problem) for each main transcript that launched a workflow agent this week, up to now,
    outside every run's span."""
    week = week_start(now)
    covered = {}
    for path, start, end in spans.values():
        covered.setdefault(path, []).append((start, end))
    out = []
    for path in main_transcripts(projects_root):
        events, launched, _ = transcript_events(path)
        own = covered.get(path, [])
        for timestamp, kind, value in events:
            t = parse_time(timestamp)
            if (kind == 'agent' and value[0] in launched and is_workflow_agent(value[1]) and t is not None
                    and week <= t <= now
                    and not any(in_window(timestamp, start, end) for start, end in own)):
                out.append((path, problem('unrecorded_session',
                                          f'session {os.path.basename(path)} ran workflow agents but wrote no run '
                                          'record')))
                break
    return out


def unsimplified_plan_reviews(projects_root, now):
    """(transcript path, problem) for each plan review a main transcript launched this week, up to now, with no
    launched plan simplify since the previous launched plan review."""
    week = week_start(now)
    out = []
    for path in main_transcripts(projects_root):
        events, launched, _ = transcript_events(path)
        simplified = False
        for timestamp, kind, value in events:
            t = parse_time(timestamp)
            if kind != 'agent' or value[0] not in launched or t is not None and t > now:
                continue
            simplified = simplified or is_plan_simplify(value[1])
            if is_plan_review(value[1]):
                if not simplified and t is not None and t >= week:
                    out.append((path, problem('plan_review_without_simplify',
                                              f"session {os.path.basename(path)}: plan review without "
                                              f"plan-simplifier: {value[1].get('description')}",
                                              step={'step': 'plan_review'})))
                simplified = False
    return out


def typed_triggers(events):
    """(timestamp, skill, met) for each typed trigger in a main transcript's events, met when a later event loads its
    skill."""
    return [(timestamp, skill, any(k == 'skill' and v == skill for _, k, v in events[i + 1:]))
            for i, (timestamp, kind, skill) in enumerate(events) if kind == 'typed']


def shared_triggers(events_by_path):
    """Each typed trigger in the main transcripts' events, given in path order, by its time and skill: [the first
    transcript holding it, whether any transcript holding it loads the skill later]. A forked session copies its
    parent's rows, so the copies are one trigger, typed once."""
    triggers = {}
    for path, events in events_by_path.items():
        for timestamp, skill, met in typed_triggers(events):
            entry = triggers.setdefault((timestamp, skill), [path, False])
            entry[1] = entry[1] or met
    return triggers


def session_warnings(projects_root, spans, now):
    """(transcript path, problem) for each typed trigger the main transcripts hold this week, up to now, that no later
    load of its skill meets, on the first transcript holding it, and for each main transcript written this week with
    no run's span and no startup block."""
    week = week_start(now)
    covered = {path for path, _, _ in spans.values()}
    read = {path: transcript_events(path) for path in main_transcripts(projects_root)}
    triggers = shared_triggers({path: [e for e in events if in_range(e[0], None, now)]
                                for path, (events, _, _) in read.items()})
    out = []
    for path, (_, _, startup) in read.items():
        name = os.path.basename(path)
        for (timestamp, skill), (first, met) in triggers.items():
            if first != path or met:
                continue
            t = parse_time(timestamp)
            if t is not None and t >= week:
                out.append((path, problem('typed_skill_not_loaded',
                                          f"session {name}: typed '{skill}' but {skill} never loaded", skill=skill)))
        # A session with a run gets the per-run line instead. Its rows say whether it ran this week, since a chat-only
        # session has no events; the file's time only screens, as the app rewrites old transcripts' metadata rows.
        try:
            touched = os.path.getmtime(path) >= week.timestamp()
        except OSError:
            touched = False
        if path not in covered and not startup and touched and (file_usage(path)['last'] or '') >= stamp(week):
            out.append((path, problem('startup_missing', f'session {name}: startup guidelines missing')))
    return out


def archive_line(run):
    """The information line on a schema 2 run's archive, or None when it's there with the run's Started: time."""
    try:
        with open(os.path.expanduser(run['archive']), errors='replace') as f:
            started = [line[len('Started:'):] for line in f if line.startswith('Started:')]
    except (OSError, ValueError):  # open() raises ValueError for a path with a NUL in it.
        return 'archive missing'
    return None if parse_time(started[0] if started else None) == run['_started'] else 'archive Started: differs'


def health(projects_root, runs, now, records, pins, spans):
    """Warnings for the problems the transcripts and git show in each run, for this week's sessions with no run
    record and for this week's plan reviews with no plan simplify, then the information lines, then each run's own
    warnings, information lines and git evidence (check_git's usable) in runs' order, then the session warnings by
    transcript path; records is the whole run log, pins the agent files' read_pins and spans the log's run_spans."""
    span_of = {id(r): spans.get(i) for i, r in enumerate(records)}
    warns, info, unavailable, per_run = [], [], 0, []
    for r in runs:
        label = f"{r.get('branch')} ({run_date(r)})"
        span = span_of.get(id(r))
        problems = check_run(*span, r, pins) if span else [problem('no_transcript', 'no session transcript found')]
        finals = [d['report'] for d in span_dispatches(*span)
                  if d['report'] is not None and d['kind'] == 'final_review'] if span else []
        git_problems, usable = check_git(r, finals[-1] if finals else '')
        unavailable += usable is False
        own_warns = [warning(p, run=run_ref(r), label=label) for p in problems + git_problems]
        own_info = []
        count = count_not_proportionate(*span) if span else 0
        if count:
            own_info.append(f'{label}: {NOT_PROPORTIONATE}: {count}')
        archive = archive_line(r) if r['schema'] >= 2 and isinstance(r.get('archive'), str) and r['archive'] else None
        if archive:
            own_info.append(f'{label}: {archive}')
        warns += own_warns
        info += own_info
        per_run.append((own_warns, own_info, usable))
    if unavailable:
        info.append(f'Git evidence unavailable for {unavailable} runs')
    sessions = [warning(p, session=path) for path, p in
                unrecorded_sessions(projects_root, spans, now) + unsimplified_plan_reviews(projects_root, now)
                + session_warnings(projects_root, spans, now)]
    per_session = {}
    for w in sessions:
        per_session.setdefault(w['session'], []).append(w)
    return warns + sessions, info, per_run, per_session


def run_warnings(projects_root, runs, now, records, pins, spans):
    """Warnings for these runs (simplify recorded as not run, then the health warnings), the information lines, each
    run's own warnings (its simplify warning first), information lines and git evidence, and the session warnings by
    transcript path."""
    warns, info, per_run, per_session = health(projects_root, runs, now, records, pins, spans)
    simplify = [[warning(problem('simplify_not_run', f"simplify did not run on {r.get('branch')} ({run_date(r)})",
                                 step={'step': 'simplify'}), run=run_ref(r))] if r['simplify']['ran'] == 'no' else []
                for r in runs]
    return (sum(simplify, []) + warns, info,
            [(own_simplify + own_warns, own_info, usable)
             for own_simplify, (own_warns, own_info, usable) in zip(simplify, per_run)],
            per_session)


def share_pct(advisor, fable):
    total = advisor + fable
    return 100.0 * advisor / total if total > 0 else 0.0


def share_text(advisor, fable):
    pct = share_pct(advisor, fable)
    text = f'{pct:.0f}% (${advisor:.2f} of ${advisor + fable:.2f})'
    return text + (' STOP' if pct > STOP_SHARE else '')


def trial(runs, now):
    trial_runs = sum(1 for r in runs if r.get('path') == 'full' and r['advisor'])
    starts = [r['_time'] for r in runs if r['advisor'] and r['_time'] is not None]
    weeks = int((now - min(starts)).total_seconds() // (7 * 86400)) if starts else 0
    weeks = max(weeks, 0)
    text = f'runs {trial_runs}/{TRIAL_RUNS}, weeks {weeks}/{TRIAL_WEEKS}'
    if trial_runs >= TRIAL_RUNS or weeks >= TRIAL_WEEKS:
        text += ' CHECKPOINT'
    return text


def group_totals(group):
    """The group of runs' count, and its summed tasks, fix rounds, stops, final findings and failing-test results."""
    tot = {'runs': len(group), 'tasks': 0, 'fix_rounds': 0, 'stops': 0}
    tot.update((k, 0) for k in FINDINGS + TEST_RESULTS)
    for r in group:
        for k in ('tasks', 'fix_rounds', 'stops'):
            tot[k] += num(r.get(k))
        for k in FINDINGS:
            tot[k] += r['final_findings'][k]
        for item in r['failing_test']:
            tot[item['result']] += 1
    return tot


def group_line(name, group):
    tot = group_totals(group)
    keys = ('runs', 'tasks') + FINDINGS + ('fix_rounds', 'stops') + TEST_RESULTS
    return f'  {name} ' + ' '.join(f'{k}={tot[k]}' for k in keys)


def run_line(r):
    growth = r['fix_growth']
    found = findings_text(r['final_findings'])
    return (f"    {r.get('date')} {r.get('project')} {r.get('branch')} path={r.get('path')} {found} "
            f"fix_rounds={num(r.get('fix_rounds'))} stops={num(r.get('stops'))} simplify {r['simplify']['ran'] or 'unknown'}"
            + (f" fix +{num(growth.get('fix_added'))}/task +{num(growth.get('task_added'))}" if growth else ''))


def rate(count, calls):
    return f'{count * 1000.0 / calls:.1f}' if calls else '0.0'


def advisor_facts(adv, notes, runs, now):
    """Everything the full report says about the advisor, as data, from unit_advisor's facts adv and the notes counts
    {'notes', 'changed', 'missing'}."""
    weeks = []
    for w in adv['weeks']:
        share = share_pct(w['advisor'], w['fable'])
        weeks.append(dict(w, share=share, stop=share > STOP_SHARE))
    return {'calls': adv['calls'], 'tokens': adv['tokens'], 'cost': adv['cost'], 'by_role': adv['by_role'],
            'failed': adv['failed'], 'notes': notes['notes'], 'changed': notes['changed'], 'missing': notes['missing'],
            'weeks': weeks, 'trial': trial(runs, now)}


def full_report(tr, runs, stats, since, now, projects_root):
    out = [f"Workflow report{' since ' + since.date().isoformat() if since else ''}"]
    advisor = advisor_facts(unit_advisor(tr), unit_notes(tr), runs, now)
    tk = advisor['tokens']
    out.append(f"Advisor calls: calls={advisor['calls']} input={tk['input']} output={tk['output']} "
               f"cache_read={tk['cache_read']} cache_write={tk['cache_write']} cost=${advisor['cost']:.2f}")
    out.append('Advisor calls by role:')
    for label, role in advisor['by_role'].items():
        out.append(f"  {label}: calls={role['calls']}" + (' FLAG should not consult' if role['flagged'] else ''))
    out.append(f"Advisor failed calls: {pairs(advisor['failed'])}")
    out.append(f"Advisor notes: notes={advisor['notes']} changed={advisor['changed']} missing={advisor['missing']}")
    out.append('Weeks from Sunday 07:00 UTC, advisor share of Fable use:')
    for week in advisor['weeks']:
        out.append(f"  {week['week']}: " + share_text(week['advisor'], week['fable']))
    out.append(f"Trial: {advisor['trial']}")
    out.append('Runs, advisor on and off:')
    for name, flag in (('advisor=on', True), ('advisor=off', False)):
        group = [r for r in runs if r['advisor'] == flag]
        out.append(group_line(name, group))
        out.extend(run_line(r) for r in group)
    out.append(f'Later fix commits within {FIX_WINDOW_DAYS} days on the same files (candidates to confirm):')
    for r in runs:
        for sha, date, subject in later_fixes(r):
            out.append(f"  {r.get('project')} {r.get('branch')}: {sha} {date} {subject}")
    out.append('Hook decisions:')
    hooks = dict(tr.hooks)
    for reason, count in guard_refusals(projects_root, since, now).items():
        key = ('guard-agent-dispatch.py', 'deny', reason)
        hooks[key] = hooks.get(key, 0) + count
    for (name, decision, reason), count in sorted(hooks.items()):
        out.append(f'  {name} {decision} x{count}: {reason}')
    calls = tr.main_calls
    out.append(f'Questions: main_calls={calls} typed={tr.typed} ({rate(tr.typed, calls)} per 1000) '
               f'ask_user_question={tr.ask_user} ({rate(tr.ask_user, calls)} per 1000) '
               f'wrap_up_ask={tr.wrap_up_ask} ({rate(tr.wrap_up_ask, calls)} per 1000)')
    out.append(f"Malformed lines: skipped={stats['skipped']}")
    return '\n'.join(out)


def warnings(projects_root, runs, now, records, pins, spans):
    """This week's WARN lines."""
    week = week_start(now)
    return texts(run_warnings(projects_root, [r for r in runs if r['_time'] is not None and r['_time'] >= week], now,
                              records, pins, spans)[0])


def template_drift(stamp_path):
    """The note that origin/main has commits since the export stamp's sha, or None when it has none or the stamp, its
    repo or git is unusable."""
    stamp = read_json_dict(stamp_path)
    out = git_range(field(stamp, 'repo', str, None), field(stamp, 'sha', str, None), 'origin/main', 'rev-list',
                    '--count')
    if not out or int(out) == 0:
        return None
    return (f"Template: {int(out)} commits on main since the last export ({field(stamp, 'date', str, '?')}), "
            'run /workflow-export')


def brief_report(tr, runs, now, records, projects_root, pins, spans, stamp_path):
    week = week_start(now)
    warns = warnings(projects_root, runs, now, records, pins, spans)
    if warns:
        last = warns[0] + (f' (+{len(warns) - 1} more, run --health)' if len(warns) > 1 else '')
    else:
        last = f'Consultations with no note: {tr.missing}'
    drift = template_drift(stamp_path)
    if drift is not None:
        last += '; ' + drift
    return '\n'.join([
        "Advisor share of this week's Fable use: " + share_text(tr.week_advisor.get(week, 0.0),
                                                               tr.week_fable.get(week, 0.0)),
        f'Trial: {trial(runs, now)}',
        last,
    ])


def spent(models):
    """The summed tokens of a Transcripts' models, and their cost; a model PRICES lacks adds tokens only."""
    return ({label: sum(m[label] for m in models.values()) for label, _ in TOKENS},
            sum(m['cost'] for m in models.values() if m['cost'] is not None))


def skill_entries(events, main_events, run):
    """The skill loads in events, each with its outcome from SKILL_OUTCOMES: the path of a later spec or plan write in
    main_events, the record run's simplify counts or failing-test results, or whether events hold a record write; or
    "ran". run is None for a session with no record."""
    entries = []
    for timestamp, kind, name in events:
        if kind != 'skill':
            continue
        source = SKILL_OUTCOMES.get(name)
        if source in ('spec', 'plan'):
            folder = SPECS_AND_PLANS[source]
            outcome = next((value for t, k, value in main_events if k == 'wrote' and t > timestamp and folder in value),
                           None)
        elif source == 'simplify':
            outcome = {k: run['simplify'][k] for k in ('applied', 'skipped')} if run else None
        elif source == 'failing_test':
            outcome = run['failing_test'] if run else None
        elif source == 'record':
            outcome = run is not None and any(k == 'write' for _, k, _ in events)
        else:
            outcome = 'ran'
        entries.append({'name': name, 'time': timestamp, 'outcome': outcome})
    return entries


def unit_phases(path, start, end, unit_path, run_ends=()):
    """The phase function of a run or session whose main transcript is path, over the window (start, end]: a run's,
    on path unit_path, or a session's, with unit_path None and run_ends the ends of its transcript's run windows.

    The main session's phase changes at each of its own events in the window: a skill load named in SKILL_PHASES, a
    simplify call, which starts the simplify span, and a launched final review or a run window's end, which ends it.
    A subagent transcript takes its dispatch's step kind, else implement for an implementer, else the main session's
    phase at its launch; one that no dispatch in the window launched is unattributed.
    """
    events, launched, _ = transcript_events(path)
    first = PATH_PHASES.get(unit_path, 'unattributed')
    times, phases, loaded, in_simplify = [], [], first, False
    # A run's end sorts after the events at its time, so a simplify call or skill load at that time is the run's.
    marks = sorted([e for e in events if in_window(e[0], start, end)]
                   + [(t, 'run_end', None) for t in run_ends if in_window(t, start, end)], key=lambda e: e[0])
    for timestamp, kind, value in marks:
        if kind == 'skill' and value in SKILL_PHASES:
            loaded = SKILL_PHASES[value]
        elif kind == 'simplify':
            in_simplify = True
        elif kind == 'run_end' or launches_final_review(kind, value, launched):
            in_simplify = False
        else:
            continue
        times.append(timestamp)
        phases.append('simplify' if in_simplify else loaded)

    def main_phase(timestamp):
        if not isinstance(timestamp, str):
            return 'unattributed'
        i = bisect.bisect_right(times, timestamp)
        return phases[i - 1] if i else first

    subs = None

    def phase_of(transcript, timestamp):
        nonlocal subs
        if transcript == path:
            return main_phase(timestamp)
        if subs is None:  # read the dispatches only for a unit that counts a subagent row
            subs = {}
            for d in span_dispatches(path, start, end):
                if d['sub']:
                    # Final reviews already have a kind, so is_workflow_agent picks out the implementers here.
                    subs[d['sub']] = d['kind'] or ('implement' if is_workflow_agent(d['inp'])
                                                   else main_phase(d['time']))
        return subs.get(transcript, 'unattributed')
    return phase_of


def run_entry(r, span, warns, info, pins, seen, keep, events, typed, git_evidence):
    """A run's record and what its window shows; the window's facts are None when it has no window. keep says whether
    a row's timestamp is the run's, its window being the first to end among those holding it; seen is the Transcripts
    whose message ids every unit shares, events the window's events the run owns, typed the run's typed triggers and
    git_evidence check_git's usable."""
    known = r['_time'] is not None and r['_started'] is not None
    entry = {'record': r['_raw'], 'schema': r['schema'], 'steps': [dict(s, time=None, end=None) for s in r['steps']],
             'final_findings': r['final_findings'],
             'simplify': r['simplify'], 'waves': r['waves'], 'offers': r['offers'], 'categories': r['categories'],
             'fable_points': run_points(r, 'fable_start', 'fable_end'),
             'all_points': run_points(r, 'all_start', 'all_end'),
             'minutes': (r['_time'] - r['_started']).total_seconds() / 60 if known else None,
             'window': None if span is None else dict(zip(('transcript', 'start', 'end'), span)),
             'warns': texts(warns), 'warnings': warns, 'info': info,
             'later_fixes': [dict(zip(('sha', 'date', 'subject'), fix)) for fix in later_fixes(r)],
             'typed': typed, 'git_evidence': git_evidence,
             'key': unit_key('|'.join(field(r, k, str, '') for k in ('date', 'project', 'branch')))}
    entry.update(dict.fromkeys(('dispatches', 'skills', 'missing_skills', 'refusals', 'models', 'phases', 'efforts',
                                'advisor', 'notes', 'main', 'title')))
    if span is None:
        return entry
    path, start, end = span
    # The main transcript is read first, so its model and effort are known before the subagents are added.
    tr = Transcripts(None, None, keep=keep, shared=seen,
                     phase_of=unit_phases(path, start, end, field(r, 'path', str, None)))
    tr.read_file(path, None, {'skipped': 0})
    model, effort = ran_on(tr)
    for sub in subagent_transcripts(path):
        tr.read_file(sub, field(subagent_meta(sub), 'agentType', str, '') or 'subagent', {'skipped': 0})
    span_ds = span_dispatches(*span)
    # The required-skill checks run only on schema 2 records.
    missing = [skill for skill, _ in missing_skills(r, events, span_ds, pins)] if r['schema'] >= 2 else None
    dispatches = []
    for d in span_ds:
        inp, sub, report, kind = d['inp'], d['sub'], d['report'], d['kind']
        name = COUNT_LINES[kind][1] if kind in COUNT_LINES else None
        sub_tr = sub_usage(sub) if sub else None
        sub_model, sub_effort = ran_on(sub_tr) if sub_tr else (None, None)
        dispatches.append({'kind': kind, 'id': d['id'], 'time': d['time'], 'end': sub_tr.last if sub_tr else None,
                           'step': None, 'agent': inp.get('subagent_type'), 'description': inp.get('description'),
                           'requested_model': inp.get('model'), 'requested_effort': inp.get('effort'),
                           'model': sub_model, 'effort': sub_effort, 'skills': agent_skills(sub) if sub else None,
                           'counts': count_line(report, name) if name and report is not None else None,
                           'models': sub_tr.models if sub_tr else None})
    for i, j in tie_steps(r, span_ds).items():
        entry['steps'][i].update(time=dispatches[j]['time'], end=dispatches[j]['end'])
        dispatches[j]['step'] = i
    # The simplify step takes its time from the window's simplify call when each is the only one.
    simplify_steps = [i for i, s in enumerate(r['steps']) if s['step'] == 'simplify']
    simplify_calls = [e[0] for e in events if e[1] == 'simplify']
    simplify_step = simplify_steps[0] if len(simplify_steps) == len(simplify_calls) == 1 else None
    if simplify_step is not None:
        entry['steps'][simplify_step]['time'] = simplify_calls[0]
    skills = [dict(s, step=simplify_step if s['name'] == 'simplify' else None)
              for s in skill_entries(events, transcript_events(path)[0], r)]
    entry.update(dispatches=dispatches, skills=skills,
                 missing_skills=missing, refusals=refusal_counts(events), models=tr.models, phases=tr.phase_list(),
                 efforts=tr.efforts, advisor=unit_advisor(tr), notes=unit_notes(tr),
                 main={'model': model, 'effort': effort}, title=transcript_title(path))
    return entry


def scoreboard(runs, entries, sessions, advisor):
    """One row per step kind and per rule, from the runs, their entries, the session entries and the advisor_facts. A
    schema 1 record adds its final review and simplify, without outcomes."""
    board = {kind: {'ran': 0, 'found': 0, 'counts': {}, 'outcomes': {}, 'tokens': {label: 0 for label, _ in TOKENS},
                    'cost': 0.0} for kind in STEP_KINDS}
    # simplify's agents are no step kind, so no dispatch tells its spend.
    board['simplify'].update(tokens=None, cost=None)

    def add(kind, counts):
        row = board[kind]
        row['ran'] += 1
        row['found'] += any(counts.values())
        for k, v in counts.items():
            row['counts'][k] = row['counts'].get(k, 0) + v

    for r, entry in zip(runs, entries):
        for step in r['steps']:
            add(step['step'], step['counts'])
            outcomes = board[step['step']]['outcomes']
            for finding in step['findings']:
                if finding['outcome']:
                    outcomes[finding['outcome']] = outcomes.get(finding['outcome'], 0) + 1
        if r['schema'] < 2:
            add('final_review', r['final_findings'])
            if r['simplify']['ran'] in ('agents', 'inline'):
                add('simplify', {k: r['simplify'][k] for k in ('applied', 'skipped')})
        for dispatch in entry['dispatches'] or []:
            if dispatch['kind'] in board and dispatch['models']:
                tokens, dollars = spent(dispatch['models'])
                row = board[dispatch['kind']]
                row['cost'] += dollars
                for label, n in tokens.items():
                    row['tokens'][label] += n
    tot = group_totals(runs)
    guard = {}
    for unit in entries + sessions:
        for key, n in (unit['refusals'] or {}).items():
            guard[key] = guard.get(key, 0) + n
    board.update(
        failing_test={k: tot[k] for k in TEST_RESULTS},
        dispatch_guard=guard,
        advisor={'calls': advisor['calls'], 'notes': advisor['notes'], 'changed': advisor['changed'],
                 'cost': advisor['cost'],
                 'share': share_pct(advisor['cost'], sum(week['fable'] for week in advisor['weeks']))},
        startup={'with': sum(s['startup'] for s in sessions), 'without': sum(not s['startup'] for s in sessions)},
        fix_round_cap={'runs': sum(1 for r in runs if any(step['round'] == 3 for step in r['steps'])),
                       'ruled': sum(1 for r in runs for step in r['steps'] for finding in step['findings']
                                    if finding['outcome'] == 'ruled')},
        stop_rule={'stops': tot['stops']})
    return board


def run_points(r, start, end):
    """A run's end - start percentage points, or None when the record lacks either number or the run spans a weekly
    usage reset: its started and date in different weeks, or, lacking either time, points that went down."""
    if not (is_num(r.get(start)) and is_num(r.get(end))):
        return None
    if r['_started'] is not None and r['_time'] is not None:
        crossed = week_start(r['_started']) != week_start(r['_time'])
    else:
        crossed = r[end] < r[start]
    return None if crossed else r[end] - r[start]


def points(entries, key):
    """The summed known points under key of the run entries, or None when none is known."""
    known = [e[key] for e in entries if e[key] is not None]
    return sum(known) if known else None


def path_counts(records):
    """The records' count by path."""
    paths = {}
    for r in records:
        path = field(r, 'path', str, 'unknown')
        paths[path] = paths.get(path, 0) + 1
    return paths


def windows_cost(entries):
    """The summed priced cost of the entries' run windows, or None when none has a window."""
    priced = [spent(entry['models'])[1] for entry in entries if entry['models'] is not None]
    return sum(priced) if priced else None


def median_or_none(values):
    """The median of the values that are not None, or None when there are none."""
    known = [v for v in values if v is not None]
    return statistics.median(known) if known else None


def week_rows(runs, entries):
    """One row per week, from Sunday 07:00 UTC, of the runs with a date."""
    weeks = {}
    for r, entry in zip(runs, entries):
        if r['_time'] is not None:
            weeks.setdefault(week_start(r['_time']), []).append((r, entry))
    rows = []
    for week, group in sorted(weeks.items()):
        tot = group_totals([r for r, _ in group])
        rows.append({'week': week.date().isoformat(), 'runs': path_counts([r for r, _ in group]),
                     'tasks': tot['tasks'],
                     'findings': {k: tot[k] for k in FINDINGS}, 'fix_rounds': tot['fix_rounds'], 'stops': tot['stops'],
                     'fable_points': points([entry for _, entry in group], 'fable_points'),
                     'all_points': points([entry for _, entry in group], 'all_points'),
                     'cost': windows_cost([entry for _, entry in group]),
                     'minutes_per_run': median_or_none(entry['minutes'] for _, entry in group),
                     'later_fixes': sum(len(entry['later_fixes']) for _, entry in group)})
    return rows


def run_time(runs, entries):
    """Full runs with waves and without: how many, how many have a run time, and their median minutes."""
    groups = {'with_waves': [], 'without_waves': []}
    for r, entry in zip(runs, entries):
        if r.get('path') == 'full':
            groups['with_waves' if r['waves'] else 'without_waves'].append(entry['minutes'])
    return {name: {'runs': len(minutes), 'timed': sum(m is not None for m in minutes),
                   'median_minutes': median_or_none(minutes)} for name, minutes in groups.items()}


def offer_totals(runs):
    """Per offered skill, by name: its outcomes, and offered as accepted plus skipped."""
    totals = {}
    for r in runs:
        for o in r['offers']:
            row = totals.setdefault(o['skill'], dict.fromkeys(('offered',) + OFFER_OUTCOMES, 0))
            row[o['outcome']] += 1
            row['offered'] += o['outcome'] in ('accepted', 'skipped')
    return dict(sorted(totals.items()))


def skill_totals(units):
    """Per skill name: the runs and sessions that loaded it, its last load, its loads by outcome (the SKILL_OUTCOMES
    source, "ran", or "none" when it produced nothing), and the times it was required and missing: a run's missing
    skill or a unit's typed trigger never met."""
    totals = {}

    def row(name):
        return totals.setdefault(name, {'runs': 0, 'last': None, 'outcomes': {}, 'missing': 0})

    for unit in units:
        skills = unit['skills'] or []
        for name in dict.fromkeys(s['name'] for s in skills):
            row(name)['runs'] += 1
        for s in skills:
            entry = row(s['name'])
            entry['last'] = max(entry['last'] or '', s['time'])
            produced = any(s['outcome'].values()) if isinstance(s['outcome'], dict) else s['outcome']
            label = SKILL_OUTCOMES.get(s['name'], 'ran') if produced else 'none'
            entry['outcomes'][label] = entry['outcomes'].get(label, 0) + 1
        for name in unit.get('missing_skills') or []:
            row(name)['missing'] += 1
        for t in unit['typed']:
            if not t['met']:
                row(t['skill'])['missing'] += 1
    return totals


def model_totals(runs, entries):
    """Per agent type, the model and effort dispatches asked for and ran on, and how many differed; per main session
    role, by the record's path, the stage table's recommendation and what the main session ran on."""
    agents = {}
    main = {path: {'recommended': recommended, 'actual': {}, 'differs': 0} for path, recommended in RECOMMENDED.items()}
    for r, entry in zip(runs, entries):
        for d in entry['dispatches'] or []:
            row = agents.setdefault(d['agent'] or 'general-purpose', {'requested': {}, 'actual': {}, 'mismatches': 0})
            for key, value in (('requested', f"{d['requested_model'] or '-'}/{d['requested_effort'] or '-'}"),
                               ('actual', f"{d['model'] or '-'}/{d['effort'] or '-'}")):
                row[key][value] = row[key].get(value, 0) + 1
            row['mismatches'] += off_request(d['model'], d['effort'], d['requested_model'], d['requested_effort'])
        row = main.get(field(r, 'path', str, None))
        if row is None or entry['main'] is None:
            continue
        model, effort = entry['main']['model'], entry['main']['effort']
        actual = f"{model or '-'}/{effort or '-'}"
        row['actual'][actual] = row['actual'].get(actual, 0) + 1
        want_model, _, want_effort = (row['recommended'].get(field(r, 'stage', str, None)) or '').partition('/')
        row['differs'] += off_request(model, effort, want_model, want_effort)
    return {'agents': agents, 'main': main}


def report_data(projects_root, records, runs, spans, pins, now, since):
    """The report's facts and totals, as the dict --json prints; records is the whole run log and spans its
    run_spans. Each transcript row belongs to one unit, the run whose window ends first among those holding it, else
    its transcript's session, and each total is a sum over the units."""
    warns, info, per_run, per_session = run_warnings(projects_root, runs, now, records, pins, spans)
    # Oldest first, a record with no time last; sorted keeps log order among equals.
    order = sorted(range(len(runs)), key=lambda i: (runs[i]['_time'] is None, runs[i]['_time'] or now))
    runs, per_run = [runs[i] for i in order], [per_run[i] for i in order]
    index_of = {id(r): i for i, r in enumerate(records)}
    # Only the exported runs' windows, so each row in range belongs to one exported unit.
    exported_runs = {index_of[id(r)] for r in runs}
    windows = {}
    for i, (path, start, end) in sorted(spans.items(), key=lambda item: (item[1][2], item[0])):
        if i in exported_runs:
            windows.setdefault(path, []).append((start, end, i))

    def owned_by(path, owner):
        """Whether a row's timestamp in the transcript path is owner's: a record index, or None for its session."""
        return lambda timestamp: window_owner(windows.get(path, []), timestamp) == owner

    events = {path: [e for e in transcript_events(path)[0] if in_range(e[0], since, now)]
              for path in main_transcripts(projects_root)}
    triggers = shared_triggers(events)
    start, end = stamp(since) if since else None, stamp(now)
    # A forked session copies its parent's rows, so an event two transcripts hold counts once, in the first unit read
    # that holds it, as a message id does: the runs are read first, then the sessions by path.
    owner_of = {}

    def unit_events(span, owner):
        """The events in the window span that are owner's, a record index or None for its session, each unclaimed one
        claimed for the transcript here; an event another transcript's unit claimed first is left out."""
        path, keep = span[0], owned_by(span[0], owner)
        return [e for e in span_events(*span)[0] if keep(e[0]) and owner_of.setdefault(event_key(e), path) == path]

    def held(path, event):
        """Whether a unit of the transcript path claimed event, through unit_events."""
        return owner_of.get(event_key(event)) == path

    def unit_typed(path, owner):
        """The typed triggers of the unit owner in the transcript path, after unit_events claimed its events; met when
        a later event of any transcript holding the trigger loads the skill."""
        keep = owned_by(path, owner)
        return [{'skill': skill, 'time': timestamp, 'met': triggers[(timestamp, skill)][1]}
                for timestamp, skill, _ in typed_triggers(events[path])
                if keep(timestamp) and held(path, (timestamp, 'typed', skill))]

    run_events = {i: unit_events(spans[i], i) for i in (index_of[id(r)] for r in runs) if i in spans}
    # The runs are read first, so a message a run shares with a session counts in the run.
    seen = Transcripts(None, None)
    entries = []
    for r, (own_warns, own_info, git_evidence) in zip(runs, per_run):
        i = index_of[id(r)]
        path = spans[i][0] if i in spans else None
        entries.append(run_entry(r, spans.get(i), own_warns, own_info, pins, seen, owned_by(path, i),
                                 run_events.get(i, []), unit_typed(path, i) if i in spans else [], git_evidence))
    sessions = []
    for path, main_events in events.items():
        keep = owned_by(path, None)
        tr_session = usage_of([path], subagent_transcripts(path), since, now, {'skipped': 0}, keep, seen,
                              unit_phases(path, start, end, None, [w[1] for w in windows.get(path, [])]))
        own = per_session.get(path, [])
        if path not in windows and not tr_session.assistant_rows and not own:
            continue
        rest = unit_events((path, start, end), None)
        whole = file_usage(path)
        sessions.append({'file': path, 'start': whole['first'], 'end': whole['last'], 'project': whole['cwd'],
                         'commands': [{'name': v, 'time': t} for t, k, v in main_events
                                      if k == 'command' and held(path, (t, k, v))],
                         'skills': skill_entries(rest, main_events, None),
                         'typed': unit_typed(path, None),
                         'models': tr_session.models, 'phases': tr_session.phase_list(), 'efforts': tr_session.efforts,
                         'advisor': unit_advisor(tr_session), 'notes': unit_notes(tr_session),
                         'refusals': refusal_counts(rest),
                         'startup': transcript_events(path)[2], 'warns': texts(own), 'warnings': own,
                         'title': transcript_title(path), 'key': unit_key(path)})
    units = entries + sessions
    advisor = advisor_facts(sum_advisor(u['advisor'] for u in units if u['advisor'] is not None),
                            {k: sum(u['notes'][k] for u in units if u['notes'] is not None)
                             for k in ('notes', 'changed', 'missing')}, runs, now)
    return {
        'version': JSON_VERSION,
        'generated': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'since': since.isoformat() if since else None,
        'now': now.isoformat(),
        'categories': category_list(),
        'stage_table': RECOMMENDED,
        'runs': entries,
        'sessions': sessions,
        'totals': {'scoreboard': scoreboard(runs, entries, sessions, advisor),
                   'weeks': week_rows(runs, entries), 'run_time': run_time(runs, entries),
                   'skills': skill_totals(units),
                   'models': model_totals(runs, entries), 'advisor': advisor, 'offers': offer_totals(runs),
                   'health': {'warns': texts(warns), 'warnings': warns, 'info': info}},
    }


def unit_key(text):
    """A unit's stable key: the first 16 hex characters of text's SHA-256."""
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]


def read_state(folder):
    """The export state in folder's state.json, or a new one with a fresh id when it is missing or malformed. A valid
    state's sequence is an int >= 0 and each unit a dict with a string kind and hash and int changed and seen, each at
    most the sequence."""
    state = read_json_dict(os.path.join(folder, 'state.json'))
    sequence, units = field(state, 'sequence', int, -1), field(state, 'units', dict, None)
    if (field(state, 'version', int, None) == STATE_VERSION and isinstance(state.get('id'), str) and sequence >= 0
            and units is not None
            and all(isinstance(u, dict) and isinstance(u.get('kind'), str) and isinstance(u.get('hash'), str)
                    and all(field(u, k, int, None) is not None and u[k] <= sequence for k in ('changed', 'seen'))
                    for u in units.values())):
        return state
    return {'version': STATE_VERSION, 'id': os.urandom(8).hex(), 'sequence': 0, 'units': {}}


def changed_units(data, cursor, folder):
    """data with only the runs and sessions whose key's entries changed since the export that gave cursor, the keys
    removed since and a new cursor, after recording this export in folder's state and pruning the parse cache of
    transcripts that no longer exist, all under state.lock. A unit absent from the last export counts as changed. A
    cursor naming another state, or a sequence it hasn't reached, counts as 0: everything."""
    os.makedirs(folder, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(folder, 'state.lock'), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        state = read_state(folder)
        old, n = state['sequence'], state['sequence'] + 1
        cursor_id, _, seq = cursor.partition(':')
        since = int(seq) if cursor_id == state['id'] and seq.isascii() and seq.isdigit() and int(seq) <= old else 0
        groups = {}
        for kind, name in (('run', 'runs'), ('session', 'sessions')):
            for entry in data[name]:
                groups.setdefault(entry['key'], (kind, []))[1].append(entry)
        units = state['units']
        for key, (kind, entries) in groups.items():
            text = json.dumps(entries, sort_keys=True, separators=(',', ':'))
            digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
            unit = units.get(key, {})
            changed = unit['changed'] if unit.get('hash') == digest and unit['seen'] == old else n
            units[key] = {'kind': kind, 'hash': digest, 'changed': changed, 'seen': n}
        state['sequence'] = n
        write_atomic(os.path.join(folder, 'state.json'), json.dumps(state))
        for entry in glob.glob(os.path.join(glob.escape(folder), 'transcripts', '*.json')):
            path = read_json_dict(entry).get('path')
            if isinstance(path, str) and not os.path.exists(path):
                try:
                    os.remove(entry)
                except OSError:
                    pass
    finally:
        os.close(fd)
    removed = {'run': [], 'session': []}
    if since:
        for key, unit in units.items():
            if key not in groups and unit['kind'] in removed and unit['seen'] >= since:
                removed[unit['kind']].append(key)
    return dict(data, runs=[e for e in data['runs'] if units[e['key']]['changed'] > since],
                sessions=[e for e in data['sessions'] if units[e['key']]['changed'] > since],
                cursor=f"{state['id']}:{n}",
                removed={'runs': sorted(removed['run']), 'sessions': sorted(removed['session'])})


PAGE_STYLE = ('body{font:14px/1.4 system-ui,sans-serif;margin:2em;color:#222;background:#fff}'
              'table{border-collapse:collapse;margin:.5em 0}'
              'th,td{border:1px solid #ccc;padding:2px 6px;text-align:left;vertical-align:top}'
              'tr.off td{background:#fde2e2}summary{cursor:pointer}'
              '@media (prefers-color-scheme: dark){body{color:#ddd;background:#1e1e1e}th,td{border-color:#555}'
              'tr.off td{background:#5a2626}}')


def cell(value):
    """A value as page text, escaped; a value the data holds as null shows as "–"."""
    return '–' if value is None else html.escape(str(value), quote=True)


def usd(value):
    return None if value is None else f'≈${value:.2f}'


def whole(value):
    return None if value is None else f'{value:.0f}'


def table(head, rows, marked=()):
    """An HTML table of escaped cells; the rows whose index is in marked are highlighted."""
    out = ['<table><tr>' + ''.join(f'<th>{cell(h)}</th>' for h in head) + '</tr>']
    for i, row in enumerate(rows):
        out.append(('<tr class="off">' if i in marked else '<tr>') + ''.join(f'<td>{cell(v)}</td>' for v in row)
                   + '</tr>')
    return '\n'.join(out + ['</table>'])


def run_steps(run):
    """A run's waves, steps and findings, each finding with its category, validity and outcome, and its strengths;
    a schema 1 run's totals, marked "before steps"."""
    head = ('step', 'task', 'round', 'agent', 'model', 'effort', 'counts')
    if run['schema'] < 2:
        simplify = dict(run['simplify'], ran=run['simplify']['ran'] or 'unknown')
        return '<p>before steps</p>\n' + table(head, [
            ('final review', None, None, None, None, None, pairs(run['final_findings'])),
            ('simplify', None, None, None, None, None, pairs(simplify))])
    def where(s):
        return 'wave ' + s['wave'] if s['wave'] else s['task']

    steps = table(head, [(s['step'].replace('_', ' '), where(s), s['round'],
                          s['agent'], s['model'], s['effort'], pairs(s['counts'])) for s in run['steps']])
    findings = [(s['step'].replace('_', ' '), f['id'], f['severity'] or f['kind'], f['category'], f['summary'],
                 f['outcome'], f['validity'], f['reason']) for s in run['steps'] for f in s['findings']]
    parts = [steps, table(('step', 'finding', 'severity or kind', 'category', 'summary', 'outcome', 'validity',
                           'reason'), findings)]
    strengths = [(s['step'].replace('_', ' '), where(s), g['category'],
                  g['summary']) for s in run['steps'] for g in s['strengths'] or ()]
    if strengths:
        parts.append(table(('step', 'task', 'category', 'strength'), strengths))
    if run['waves']:
        parts.insert(0, table(('wave', 'tasks', 'conflicts'),
                              [(w['wave'], ' '.join(map(str, w['tasks'])), w['conflicts']) for w in run['waves']]))
    return '\n'.join(parts)


def render_html(data):
    """The dashboard page, built only from report_data's dict: no script, nothing loaded from outside, every value
    escaped."""
    totals, board = data['totals'], data['totals']['scoreboard']
    runs = data['runs']
    records = [run['record'] for run in runs]
    header = (f"<p>Generated {cell(data['generated'])}. Window {cell(data['since'])} to {cell(data['now'])}. "
              f"Runs: {cell(pairs(path_counts(records)))}. "
              f"Points: Fable {cell(points(runs, 'fable_points'))}, "
              f"all models {cell(points(runs, 'all_points'))}. "
              f"Estimated {cell(usd(windows_cost(runs)))}. "
              f"WARN lines: {len(totals['health']['warns'])}.</p>")

    rules = []
    for kind in STEP_KINDS:
        row, out = board[kind], board[kind]['outcomes']
        rules.append((kind.replace('_', ' '), row['ran'], row['found'], pairs(row['counts']),
                      out.get('fixed', 0) + out.get('applied', 0), out.get('declined', 0), out.get('deferred', 0),
                      out.get('open', 0) + out.get('to_owner', 0), pairs(row['tokens']), usd(row['cost'])))
    advisor, rest = board['advisor'], (None,) * 6
    rules += [('failing-test check', sum(board['failing_test'].values()), None, pairs(board['failing_test'])) + rest,
              ('dispatch guard', None, sum(board['dispatch_guard'].values()), pairs(board['dispatch_guard'])) + rest,
              ('advisor', advisor['calls'], None,
               f"notes={advisor['notes']} changed={advisor['changed']} share={whole(advisor['share'])}%")
              + rest[:5] + (usd(advisor['cost']),),
              ('startup guidelines', board['startup']['with'] + board['startup']['without'], None,
               pairs(board['startup'])) + rest,
              ('fix-round cap', None, None, pairs(board['fix_round_cap'])) + rest,
              ('stop rule', None, None, pairs(board['stop_rule'])) + rest]
    rules += [('offer ' + skill, counts['offered'], None, pairs(counts)) + rest
              for skill, counts in totals['offers'].items()]

    weeks = [(w['week'], pairs(w['runs']), w['tasks'], pairs(w['findings']), w['fix_rounds'], w['stops'],
              w['fable_points'], w['all_points'], usd(w['cost']), whole(w['minutes_per_run']), w['later_fixes'])
             for w in totals['weeks']]
    skills = [(name, s['runs'], s['last'], pairs(s['outcomes']), s['missing'])
              for name, s in sorted(totals['skills'].items())]
    agents = sorted(totals['models']['agents'].items())
    sessions = sorted(totals['models']['main'].items())

    adv = totals['advisor']
    facts = [('calls', adv['calls']), ('tokens', pairs(adv['tokens'])), ('cost', usd(adv['cost']))]
    facts += [(f'calls by role: {label}', f"{role['calls']}" + (' FLAG should not consult' if role['flagged'] else ''))
              for label, role in adv['by_role'].items()]
    facts += [('failed calls', pairs(adv['failed'])),
              ('notes', pairs({k: adv[k] for k in ('notes', 'changed', 'missing')})),
              ('trial', adv['trial'])]

    groups = [(f"{run['record'].get('branch')} ({run['record'].get('date')})", run['warns'])
              for run in runs if run['warns']]
    groups += [(os.path.basename(s['file']), s['warns']) for s in data['sessions'] if s['warns']]
    used = {line for _, lines in groups for line in lines}
    groups += [(name, lines) for name, lines in (('other', [w for w in totals['health']['warns'] if w not in used]),
                                                 ('information', totals['health']['info'])) if lines]

    body = [header, '<h2>Rules and steps</h2>',
            table(('rule or step', 'ran', 'found something', 'counts', 'fixed or applied', 'declined', 'deferred',
                   'open or to the owner', 'tokens', 'estimated dollars'), rules),
            '<h2>Whole workflow</h2>',
            table(('week', 'runs', 'tasks', 'findings', 'fix rounds', 'stops', 'Fable points', 'all points',
                   'estimated dollars', 'minutes per run', 'later fix commits'), weeks),
            table(('Full runs', 'runs', 'with a run time', 'median minutes'),
                  [(name.replace('_', ' '), t['runs'], t['timed'], whole(t['median_minutes']))
                   for name, t in totals['run_time'].items()]),
            '<h2>Skills</h2>', table(('skill', 'runs', 'last', 'outcomes', 'required but missing'), skills),
            '<h2>Models</h2>',
            table(('agent', 'requested', 'actual', 'mismatches'),
                  [(name, pairs(a['requested']), pairs(a['actual']), a['mismatches']) for name, a in agents],
                  {i for i, (_, a) in enumerate(agents) if a['mismatches']}),
            table(('main session', 'stage table', 'actual', 'differs'),
                  [(path, ' '.join(f'{stage}={want or "–"}' for stage, want in m['recommended'].items()),
                    pairs(m['actual']), m['differs']) for path, m in sessions],
                  {i for i, (_, m) in enumerate(sessions) if m['differs']}),
            '<h2>Advisor</h2>', table(('fact', 'value'), facts),
            table(('week', 'advisor', 'Fable', 'share', ''),
                  [(w['week'], usd(w['advisor']), usd(w['fable']), f"{whole(w['share'])}%", 'STOP' if w['stop'] else '')
                   for w in adv['weeks']]),
            '<h2>Runs</h2>']
    for run in runs:
        rec = run['record']
        known = (('path', rec.get('path')), ('schema', run['schema']), ('tasks', rec.get('tasks')),
                 ('fix_rounds', rec.get('fix_rounds')), ('stops', rec.get('stops')), ('minutes', whole(run['minutes'])),
                 ('waves', len(run['waves'])),
                 ('offers', ' '.join(f"{o['skill']}:{o['outcome']}" for o in run['offers']) or None),
                 ('cost', usd(windows_cost([run]))), ('warns', len(run['warns'])))
        line = ' '.join([cell(rec.get(k)) for k in ('date', 'project', 'branch')]
                        + [f'{k}={cell(v)}' for k, v in known])
        body.append(f'<details class="run"><summary>{line}</summary>\n{run_steps(run)}\n</details>')
    body.append('<h2>Health</h2>')
    body += [f'<details class="health"><summary>{cell(name)}: {len(lines)}</summary><ul>'
             + ''.join(f'<li>{cell(line)}</li>' for line in lines) + '</ul></details>' for name, lines in groups]
    if not groups:
        body.append('<p>Health: no problems</p>')
    return '\n'.join(['<!DOCTYPE html>', '<html lang="en">', '<head>', '<meta charset="utf-8">',
                      '<title>Workflow dashboard</title>', f'<style>{PAGE_STYLE}</style>', '</head>', '<body>',
                      '<h1>Workflow dashboard</h1>'] + body + ['</body>', '</html>']) + '\n'


def main():
    global CACHE_DIR
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0], formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split('\n', 2)[2])
    ap.add_argument('--since', help='YYYY-MM-DD')
    ap.add_argument('--projects', default=os.path.expanduser('~/.claude/projects'))
    ap.add_argument('--log', default=os.path.expanduser('~/.claude/workflow-log.jsonl'))
    ap.add_argument('--now', help='ISO time to use as now (for tests)')
    ap.add_argument('--agents', default=os.path.expanduser('~/.claude/agents'))
    ap.add_argument('--export-stamp', default=os.path.expanduser('~/.claude/workflow-export.json'))
    ap.add_argument('--cache', default=os.path.expanduser('~/.claude/workflow-report-cache'))
    ap.add_argument('--no-cache', action='store_true')
    ap.add_argument('--changed-since', metavar='CURSOR')
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--brief', action='store_true')
    mode.add_argument('--health', action='store_true')
    mode.add_argument('--schema', action='store_true')
    mode.add_argument('--json', action='store_true')
    mode.add_argument('--html', nargs='?', metavar='FILE',
                      const=os.path.expanduser('~/.claude/workflow-dashboard.html'))
    args = ap.parse_args()
    CACHE_DIR = None if args.no_cache else args.cache
    if args.changed_since is not None:
        if not args.json:
            ap.error('--changed-since needs --json')
        if args.since:
            ap.error('--changed-since cannot be used with --since')
        if args.no_cache:
            ap.error('--changed-since cannot be used with --no-cache')
    if args.schema:
        print(json.dumps(dict(SCHEMA, category_list=category_list()), indent=2))
        return 0
    since = parse_time(args.since)
    if args.since and since is None:
        ap.error('--since must be YYYY-MM-DD')
    now = parse_time(args.now) if args.now else datetime.datetime.now(datetime.timezone.utc)
    if now is None:
        ap.error('--now must be an ISO 8601 time')
    stats = {'skipped': 0}
    records = [r for r in read_runs(args.log, stats) if r['_time'] is None or r['_time'] <= now]
    runs = [r for r in records if since is None or r['_time'] is None or r['_time'] >= since]
    pins = read_pins(args.agents)
    spans = run_spans(args.projects, records, now)
    if args.health:
        warns, info, _, _ = run_warnings(args.projects, runs, now, records, pins, spans)
        print('\n'.join((texts(warns) or ['Health: no problems']) + info))
        return 0
    if args.json or args.html:
        data = report_data(args.projects, records, runs, spans, pins, now, since)
        if args.json:
            try:
                if args.changed_since is not None:
                    data = changed_units(data, args.changed_since, CACHE_DIR)
                print(json.dumps(data, indent=1, sort_keys=True))
                sys.stdout.flush()
            except BrokenPipeError:  # The reader stopped early; point stdout at devnull so the exit flush can't fail.
                os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
            return 0
        with open(args.html, 'w', encoding='utf-8') as f:
            f.write(render_html(data))
        print(f'Wrote {args.html}')
        return 0
    tr = read_transcripts(args.projects, since, now, stats)
    print(brief_report(tr, runs, now, records, args.projects, pins, spans, args.export_stamp) if args.brief
          else full_report(tr, runs, stats, since, now, args.projects))
    return 0


if __name__ == '__main__':
    sys.exit(main())
