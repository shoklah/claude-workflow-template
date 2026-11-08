#!/usr/bin/python3 -I
"""Read-only report over Claude Code transcripts and the workflow run log.

Usage: workflow-report.py [--since YYYY-MM-DD] [--projects DIR] [--log FILE] [--brief | --health | --schema] [--now ISO]

  --projects  transcript tree (default ~/.claude/projects)
  --log       run log, one JSON run record per line (default ~/.claude/workflow-log.jsonl)
  --since     ignore transcript rows and run records before this date (UTC)
  --brief     at most 3 lines: this week's advisor share of Fable use, the trial's progress, then this week's
              first WARN line with a count of the rest, or else the consultations with no note
  --health    every WARN line for the runs since --since, from the run log, the runs' transcripts and git, for
              this week's sessions with no run record, and for this week's plan reviews with no plan-simplifier
              since the previous plan review, or "Health: no problems"; then information lines: the
              "fixed, not proportionate" counts and the runs whose git evidence is unavailable
  --schema    print the run record schema and exit
  --now       ISO time to use as "now", for tests (default: the current time)

Advisor use is read from message.usage.iterations entries of type advisor_message, once per message id.
Weeks run from Sunday 07:00 UTC. Costs are list-price estimates at Fable prices.
"""
import argparse
import datetime
import functools
import glob
import heapq
import json
import os
import re
import subprocess
import sys

SCHEMA = {
    'date': 'ISO 8601 time the run finished',
    'project': 'absolute path of the project',
    'branch': 'branch the run worked on',
    'base': 'commit the branch started from',
    'head': 'last commit of the run',
    'path': '"small" or "full"',
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
}

# Fable list prices, dollars per million tokens.
PRICE_INPUT = 10.0
PRICE_OUTPUT = 50.0
PRICE_CACHE_READ = 0.25
PRICE_CACHE_WRITE = PRICE_INPUT * 1.25
# Report label, usage field, price.
TOKENS = (('input', 'input_tokens', PRICE_INPUT), ('output', 'output_tokens', PRICE_OUTPUT),
          ('cache_read', 'cache_read_input_tokens', PRICE_CACHE_READ),
          ('cache_write', 'cache_creation_input_tokens', PRICE_CACHE_WRITE))

STOP_SHARE = 25.0
TRIAL_RUNS = 5
TRIAL_WEEKS = 3
FIX_WINDOW_DAYS = 14
NEVER_CONSULT = ('task-reviewer', 'final-reviewer', 'plan-reviewer', 'plan-simplifier', 'fix-checker')
FINDINGS = ('critical', 'important', 'minor', 'unneeded')
TEST_RESULTS = ('passed', 'failed', 'no_tests', 'couldnt_run', 'exempt')
SIMPLIFY_RAN = ('agents', 'inline', 'no')
NOTE_RE = re.compile(r'^Advisor: .+ \| changed: (yes|no) \| .*$', re.M)
SCRIPT_RE = re.compile(r'([^/\s"\']+\.(?:py|sh))')
WRAP_UP = 'mcp__ccd_turn__wrap_up'
GIT_TIMEOUT = 30

# Copied from hooks/guard-agent-dispatch.py, since scripts can't import each other: keep them the same.
UNNAMED_TYPES = {None, '', 'general-purpose'}
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
FINDINGS_LINE = re.compile(r'^[`*\s]*Findings:(.*)$', re.M)
SEVERITY = re.compile(r'[#*_\s\d.)-]*(critical|important|minor)\b', re.I)
NOT_PROPORTIONATE = 'fixed, not proportionate'
CONVENTIONS = '## Code conventions'
FIX_ROUND_SUBJECT = 'fix: address review findings'
FIX_GROWTH_SLACK = 20  # lines; a recorded fix_added is off when it misses git's by more than this and by over 50%
NOT_FOUND = re.compile(r"Agent type '([^']*)' not found")
STARTUP_TAG = b'<karpathy-guidelines-session-start>'
RUN_LOG = 'workflow-log.jsonl'
# Only lines matching this can hold a dispatch, a simplify call, a record write, the startup block or an error.
HEALTH_NEEDLES = (b'"Agent"', b'simplify', b'workflow-log.jsonl', b'karpathy-guidelines-session-start',
                  b'"is_error":true', b'"is_error": true')


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


def week_start(t):
    """Start of the week containing t; weeks run from Sunday 07:00 UTC."""
    shifted = t - datetime.timedelta(hours=7)
    days_since_sunday = (shifted.weekday() + 1) % 7
    day = shifted.date() - datetime.timedelta(days=days_since_sunday)
    return datetime.datetime(day.year, day.month, day.day, 7, tzinfo=datetime.timezone.utc)


def num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def field(obj, key, kind, default):
    """obj[key] when it has the expected type, else default."""
    value = obj.get(key)
    return value if isinstance(value, kind) else default


def cost(u):
    return sum(num(u.get(name)) * price for _, name, price in TOKENS) / 1e6


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


class Transcripts:
    def __init__(self, since):
        self.since = since
        self.advisor_ids = set()
        self.fable_ids = set()
        self.tokens = {label: 0 for label, _, _ in TOKENS}
        self.by_role = {}
        self.failed_ids = set()
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
        has_origin = False
        typed_human = typed_plain = 0
        for row in read_jsonl(path, stats):
            if isinstance(row.get('origin'), dict):
                has_origin = True
            t = parse_time(row.get('timestamp'))
            if self.since is not None and t is not None and t < self.since:
                continue
            kind = row.get('type')
            if kind == 'assistant':
                file_calls += self.assistant(row, t, role, main, file_notes)
            elif kind == 'attachment':
                self.hook(row.get('attachment'))
            elif kind == 'user' and main and self.is_typed(row):
                origin = row.get('origin')
                typed_human += isinstance(origin, dict) and origin.get('kind') == 'human'
                typed_plain += not row.get('isCompactSummary')
        # Transcripts since Claude Code 2.1.187 mark typed rows origin.kind "human"; rows without it there are
        # injected (reminders, task notifications, resumes). Older transcripts have no origin at all.
        self.typed += typed_human if has_origin else typed_plain
        self.notes += len(file_notes)
        self.changed += sum(1 for _, _, changed in file_notes if changed == 'yes')
        self.missing += max(0, file_calls - len(file_notes))

    def assistant(self, row, t, role, main, file_notes):
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
        usage = field(msg, 'usage', dict, {})
        iterations = [i for i in usage.get('iterations') or [] if isinstance(i, dict)]
        if on_fable and mid not in self.fable_ids:
            if mid:
                self.fable_ids.add(mid)
            # The executor's own sampling; advisor and fallback iterations are counted apart.
            own = [i for i in iterations if i.get('type') == 'message'] if iterations else [usage]
            spent = sum(cost(i) for i in own)
            if week is not None:
                self.week_fable[week] = self.week_fable.get(week, 0.0) + spent
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
            for token, name, _ in TOKENS:
                self.tokens[token] += num(i.get(name))
            if week is not None:
                self.week_advisor[week] = self.week_advisor.get(week, 0.0) + cost(i)
        return len(advisor)

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


def read_transcripts(root, since, stats):
    tr = Transcripts(since)
    for path in main_transcripts(root):
        tr.read_file(path, None, stats)
    # Agent subagents sit in <session>/subagents, workflow subagents deeper, as subagent_transcripts finds them.
    pattern = os.path.join(glob.escape(root), '*', '*', 'subagents', '**', '*.jsonl')
    for path in sorted(glob.glob(pattern, recursive=True)):
        tr.read_file(path, field(subagent_meta(path), 'agentType', str, '') or 'subagent', stats)
    return tr


def subagent_meta(path):
    """The .meta.json beside a subagent transcript, or {}."""
    try:
        with open(path[:-len('.jsonl')] + '.meta.json') as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def read_runs(log, stats):
    runs = []
    for rec in read_jsonl(log, stats):
        t = parse_time(rec.get('date'))
        # Clean each record once, so the report lines below read plain fields.
        rec['_time'] = t
        rec['advisor'] = rec.get('advisor') is True
        findings = field(rec, 'final_findings', dict, {})
        rec['final_findings'] = {k: num(findings.get(k)) for k in FINDINGS}
        rec['failing_test'] = [item for item in field(rec, 'failing_test', list, [])
                               if isinstance(item, dict) and item.get('result') in TEST_RESULTS]
        # A record without "ran" (written before it existed) is unknown, never "no".
        simplify = field(rec, 'simplify', dict, {})
        rec['simplify'] = dict(simplify, ran=simplify.get('ran') if simplify.get('ran') in SIMPLIFY_RAN else None)
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


def launched_ids(row, ids):
    """The ids, among ids, that this row reports as launched: their tool_result is not an error.

    Only is_error decides this, never the result's text, which may be a review that says "not found" or "denied".
    """
    content = row.get('message', {}).get('content')
    if row.get('type') != 'user' or not isinstance(content, list):
        return []
    return [block['tool_use_id'] for block in content
            if block.get('type') == 'tool_result' and block.get('tool_use_id') in ids and block.get('is_error') is not True]


def result_text(block):
    content = block.get('content')
    if isinstance(content, list):
        return ' '.join(c.get('text', '') for c in content if isinstance(c, dict) and isinstance(c.get('text'), str))
    return content if isinstance(content, str) else ''


@functools.lru_cache(maxsize=None)
def transcript_events(path):
    """Read a transcript once and return (events, launched ids, has the startup block).

    Events are (timestamp, kind, value) in file order: ('simplify', None), ('agent', (id, input)), ('not_found',
    agent type) and ('write', Bash command naming the run log). A row with no timestamp takes the previous row's.
    Only lines holding one of HEALTH_NEEDLES are parsed, plus the result lines of dispatches still waiting for one.
    """
    events, launched, startup, timestamp = [], set(), False, ''
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
                if row_calls_simplify(row):
                    events.append((timestamp, 'simplify', None))
                if kind == 'assistant':
                    for block in content or []:
                        inp = block.get('input')
                        if block.get('type') != 'tool_use' or not isinstance(inp, dict):
                            continue
                        if block.get('name') == 'Agent' and isinstance(block.get('id'), str):
                            pending[block['id']] = block['id'].encode()
                            kept = {k: inp.get(k) for k in ('subagent_type', 'model', 'description')}
                            events.append((timestamp, 'agent', (block['id'], kept)))
                        elif block.get('name') == 'Bash' and RUN_LOG in str(inp.get('command')):
                            events.append((timestamp, 'write', inp['command']))
                elif kind == 'user' and isinstance(content, list):
                    launched.update(launched_ids(row, pending))
                    for block in content:
                        if block.get('type') != 'tool_result':
                            continue
                        # Only an Agent call's own error says its agent type was not found.
                        dispatch = pending.pop(block.get('tool_use_id'), None)
                        errored = dispatch is not None and block.get('is_error') is True
                        found = NOT_FOUND.search(result_text(block)) if errored else None
                        if found:
                            events.append((timestamp, 'not_found', found.group(1)))
            except (ValueError, AttributeError, TypeError, RecursionError):  # A torn or odd line says nothing.
                continue
    return events, launched, startup


def main_transcripts(projects_root):
    return sorted(glob.glob(os.path.join(glob.escape(projects_root), '*', '*.jsonl')))


def run_spans(projects_root, records):
    """Map each record's index in records, the whole run log, to (transcript_path, start, end), as timestamps.

    end is the main-transcript Bash row that wrote the run's record: the first whose "head" value is the run's head,
    since the next run's record names that head again as its base, perhaps in another session. start is the previous
    record-writing row in that transcript, one whose "head" value is any record's head, or None for the transcript's
    start. So a run a report leaves out still ends where its record was written, and a command that mentions the log
    without a record's "head" value ends nothing. A run whose record write isn't found has no span.
    """
    heads = [(i, re.compile(r'"head"\s*:\s*"' + re.escape(r['head'])))
             for i, r in enumerate(records) if isinstance(r.get('head'), str) and r['head']]
    spans = {}
    if not heads:
        return spans
    for path in main_transcripts(projects_root):
        start = None
        for timestamp, kind, command in transcript_events(path)[0]:
            owners = [i for i, written in heads if kind == 'write' and written.search(command)]
            for i in owners:
                spans.setdefault(i, (path, start, timestamp))
            if owners:
                start = timestamp
    return spans


def subagent_transcripts(path):
    """The subagent transcripts of the main transcript path, workflow subagents included."""
    folder = glob.escape(path[:-len('.jsonl')])
    return sorted(glob.glob(os.path.join(folder, 'subagents', '**', '*.jsonl'), recursive=True))


@functools.lru_cache(maxsize=None)
def span_events(path, start, end):
    """(events, launched ids) of the rows timed in (start, end] of the main transcript path and its subagents."""
    launched, per_file = set(), []
    for p in [path] + subagent_transcripts(path):
        events, file_launched, _ = transcript_events(p)
        launched |= file_launched
        per_file.append([e for e in events if (start is None or e[0] > start) and e[0] <= end])
    # Rows from several transcripts merge by timestamp, as in the dispatch hook.
    return list(heapq.merge(*per_file, key=lambda e: e[0])), launched


@functools.lru_cache(maxsize=None)
def review_report(subagent_path):
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
    """(dispatch input, report) for each review or fix-checker launched in the span whose subagent transcript is
    there, in order."""
    events, launched = span_events(path, start, end)
    files = {}
    for p in subagent_transcripts(path):
        tid = subagent_meta(p).get('toolUseId')
        if isinstance(tid, str):
            files[tid] = p
    reports = []
    for _, kind, value in events:
        if (kind == 'agent' and value[0] in launched and value[0] in files
                and (is_review(value[1]) or value[1].get('subagent_type') == 'fix-checker')):
            reports.append((value[1], review_report(files[value[0]])))
    return reports


def is_review(tool_input):
    """A task or final review, whose report ends with a Findings: line."""
    subagent_type, description = tool_input.get('subagent_type'), tool_input.get('description')
    return (subagent_type == 'task-reviewer' or is_final_review(tool_input)
            or subagent_type in UNNAMED_TYPES and isinstance(description, str) and bool(TASK_REVIEW.search(description)))


def findings_line(report):
    """The counts on the report's last Findings: line, or None when it has none."""
    lines = FINDINGS_LINE.findall(report)
    if not lines:
        return None
    counts = {k: int(v) for k, v in re.findall(r'(\w+)=(\d+)', lines[-1])}
    return {k: counts.get(k, 0) for k in FINDINGS}


def findings_text(counts):
    return ' '.join(f'{k}={v}' for k, v in counts.items())


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
    return sum(report.lower().count(NOT_PROPORTIONATE) for inp, report in span_reports(path, start, end)
               if inp.get('subagent_type') == 'fix-checker')


def check_run(path, start, end, run):
    """Problems shown by the rows timed in (start, end] of the main transcript path and its subagent transcripts."""
    events, launched = span_events(path, start, end)
    agents = [value[1] for _, kind, value in events if kind == 'agent' and value[0] in launched]
    ran = run['simplify']['ran']
    problems, since, simplified = [], False, False
    for _, kind, value in events:
        if kind == 'simplify':
            since = simplified = True
        elif kind == 'agent' and value[0] in launched and is_final_review(value[1]):
            # When the record says simplify did not run, its own warning covers these.
            if not since and ran != 'no':
                problems.append(f"final review without simplify: {value[1].get('description')}")
            since = False
    if ran == 'no' and simplified:
        problems.append('record says simplify did not run but the transcript has a simplify call')
    elif ran in ('agents', 'inline') and not simplified:
        problems.append(f'record says simplify ran ({ran}) but the transcript has no simplify call')
    problems += [f"agent type '{value}' not found" for _, kind, value in events if kind == 'not_found']
    reports = span_reports(path, start, end)
    problems += [f"review report has no Findings: line: {inp.get('description')}" for inp, report in reports
                 if is_review(inp) and findings_line(report) is None]
    finals = [findings_line(report) for inp, report in reports if is_final_review(inp)]
    if finals and finals[-1] is not None and finals[-1] != run['final_findings']:
        problems.append(f"record's final_findings {findings_text(run['final_findings'])} differ from the final "
                        f"review's {findings_text(finals[-1])}")
    unnamed = [inp for inp in agents if inp.get('subagent_type') in UNNAMED_TYPES]
    problems += [f"named role by general-purpose: {inp.get('description')}" for inp in unnamed
                 if isinstance(inp.get('description'), str) and NAMED_ROLE.search(inp['description'])]
    problems += [f"model-less general-purpose dispatch launched: {inp.get('description')}" for inp in unnamed
                 if not inp.get('model')]
    if not transcript_events(path)[2]:
        problems.append('startup guidelines missing from the session')
    return problems


def run_date(run):
    return run['_time'].date().isoformat() if run['_time'] is not None else 'no date'


def check_git(run, final_report):
    """Problems git shows in the run, and False when a check needed git and git was unusable."""
    project, base, head = run.get('project'), run.get('base'), run.get('head')
    problems, usable = [], True
    if run['fix_growth'] is not None:
        growth = recompute_fix_growth(project, base, head)
        usable = growth is not None
        if usable:
            recorded = num(run['fix_growth'].get('fix_added'))
            off = abs(recorded - growth['fix_added'])
            if off > growth['fix_added'] / 2 and off > FIX_GROWTH_SLACK:
                problems.append(f"record's fix_added {recorded} differs from git's {growth['fix_added']}")
    if asks_for_conventions(final_report):
        added = conventions_added(project, base, head)
        usable = usable and added is not None
        if added == 0:
            problems.append(f'final review asked for a {CONVENTIONS} entry but the branch added none')
    return problems, usable


def is_workflow_agent(tool_input):
    """An implementer or a final review, named or general-purpose: the session that dispatches one writes a record."""
    description = tool_input.get('description')
    return (tool_input.get('subagent_type') in WORKFLOW_TYPES or is_final_review(tool_input)
            or tool_input.get('subagent_type') in UNNAMED_TYPES and isinstance(description, str)
            and 'implement' in description.lower())


def unrecorded_sessions(projects_root, spans, now):
    """A WARN line for each main transcript that launched a workflow agent this week outside every run's span."""
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
            if (kind == 'agent' and value[0] in launched and is_workflow_agent(value[1]) and t is not None and t >= week
                    and not any((start is None or timestamp > start) and timestamp <= end for start, end in own)):
                out.append(f'WARN: session {os.path.basename(path)} ran workflow agents but wrote no run record')
                break
    return out


def unsimplified_plan_reviews(projects_root, now):
    """A WARN line for each plan review a main transcript launched this week with no launched plan simplify since
    the previous launched plan review."""
    week = week_start(now)
    out = []
    for path in main_transcripts(projects_root):
        events, launched, _ = transcript_events(path)
        simplified = False
        for timestamp, kind, value in events:
            if kind != 'agent' or value[0] not in launched:
                continue
            simplified = simplified or is_plan_simplify(value[1])
            if is_plan_review(value[1]):
                t = parse_time(timestamp)
                if not simplified and t is not None and t >= week:
                    out.append(f"WARN: session {os.path.basename(path)}: plan review without plan-simplifier: "
                               f"{value[1].get('description')}")
                simplified = False
    return out


def health(projects_root, runs, now, records):
    """WARN lines for the problems the transcripts and git show in each run, for this week's sessions with no run
    record and for this week's plan reviews with no plan simplify, then the information lines; records is the whole
    run log."""
    spans = run_spans(projects_root, records)
    span_of = {id(r): spans.get(i) for i, r in enumerate(records)}
    warns, info, unavailable = [], [], 0
    for r in runs:
        label = f"{r.get('branch')} ({run_date(r)})"
        span = span_of.get(id(r))
        problems = check_run(*span, r) if span else ['no session transcript found']
        finals = [report for inp, report in span_reports(*span) if is_final_review(inp)] if span else []
        git_problems, usable = check_git(r, finals[-1] if finals else '')
        unavailable += not usable
        warns.extend(f'WARN: {label}: {problem}' for problem in problems + git_problems)
        count = count_not_proportionate(*span) if span else 0
        if count:
            info.append(f'{label}: {NOT_PROPORTIONATE}: {count}')
    if unavailable:
        info.append(f'Git evidence unavailable for {unavailable} runs')
    return warns + unrecorded_sessions(projects_root, spans, now) + unsimplified_plan_reviews(projects_root, now), info


def run_warnings(projects_root, runs, now, records):
    """WARN lines for these runs (simplify recorded as not run, then the health lines) and the information lines."""
    warns, info = health(projects_root, runs, now, records)
    return [f"WARN: simplify did not run on {r.get('branch')} ({run_date(r)})" for r in runs
            if r['simplify']['ran'] == 'no'] + warns, info


def share_text(advisor, fable):
    total = advisor + fable
    pct = 100.0 * advisor / total if total > 0 else 0.0
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


def group_line(name, group):
    tot = {'runs': len(group), 'tasks': 0, 'fix_rounds': 0, 'stops': 0}
    tot.update((k, 0) for k in FINDINGS + TEST_RESULTS)
    for r in group:
        for k in ('tasks', 'fix_rounds', 'stops'):
            tot[k] += num(r.get(k))
        for k in FINDINGS:
            tot[k] += r['final_findings'][k]
        for item in r['failing_test']:
            tot[item['result']] += 1
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


def full_report(tr, runs, stats, since, now):
    out = [f"Workflow report{' since ' + since.date().isoformat() if since else ''}"]
    tk = tr.tokens
    calls = sum(n for n, _ in tr.by_role.values())
    spent = cost({name: tk[label] for label, name, _ in TOKENS})
    out.append(f"Advisor calls: calls={calls} input={tk['input']} output={tk['output']} "
               f"cache_read={tk['cache_read']} cache_write={tk['cache_write']} cost=${spent:.2f}")
    out.append('Advisor calls by role:')
    for label in sorted(tr.by_role):
        calls, flagged = tr.by_role[label]
        out.append(f'  {label}: calls={calls}' + (' FLAG should not consult' if flagged else ''))
    failed = ' '.join(f'{k}={v}' for k, v in sorted(tr.failed.items())) or 'none'
    out.append(f'Advisor failed calls: {failed}')
    out.append(f'Advisor notes: notes={tr.notes} changed={tr.changed} missing={tr.missing}')
    out.append('Weeks from Sunday 07:00 UTC, advisor share of Fable use:')
    for week in sorted(set(tr.week_advisor) | set(tr.week_fable)):
        out.append(f'  {week.date().isoformat()}: ' + share_text(tr.week_advisor.get(week, 0.0),
                                                                    tr.week_fable.get(week, 0.0)))
    out.append(f'Trial: {trial(runs, now)}')
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
    for (name, decision, reason), count in sorted(tr.hooks.items()):
        out.append(f'  {name} {decision} x{count}: {reason}')
    calls = tr.main_calls
    out.append(f'Questions: main_calls={calls} typed={tr.typed} ({rate(tr.typed, calls)} per 1000) '
               f'ask_user_question={tr.ask_user} ({rate(tr.ask_user, calls)} per 1000) '
               f'wrap_up_ask={tr.wrap_up_ask} ({rate(tr.wrap_up_ask, calls)} per 1000)')
    out.append(f"Malformed lines: skipped={stats['skipped']}")
    return '\n'.join(out)


def warnings(projects_root, runs, now, records):
    """This week's WARN lines."""
    week = week_start(now)
    return run_warnings(projects_root, [r for r in runs if r['_time'] is not None and r['_time'] >= week], now, records)[0]


def brief_report(tr, runs, now, records, projects_root):
    week = week_start(now)
    warns = warnings(projects_root, runs, now, records)
    if warns:
        last = warns[0] + (f' (+{len(warns) - 1} more, run --health)' if len(warns) > 1 else '')
    else:
        last = f'Consultations with no note: {tr.missing}'
    return '\n'.join([
        "Advisor share of this week's Fable use: " + share_text(tr.week_advisor.get(week, 0.0),
                                                               tr.week_fable.get(week, 0.0)),
        f'Trial: {trial(runs, now)}',
        last,
    ])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0], formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split('\n', 2)[2])
    ap.add_argument('--since', help='YYYY-MM-DD')
    ap.add_argument('--projects', default=os.path.expanduser('~/.claude/projects'))
    ap.add_argument('--log', default=os.path.expanduser('~/.claude/workflow-log.jsonl'))
    ap.add_argument('--now', help='ISO time to use as now (for tests)')
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--brief', action='store_true')
    mode.add_argument('--health', action='store_true')
    mode.add_argument('--schema', action='store_true')
    args = ap.parse_args()
    if args.schema:
        print(json.dumps(SCHEMA, indent=2))
        return 0
    since = parse_time(args.since)
    if args.since and since is None:
        ap.error('--since must be YYYY-MM-DD')
    now = parse_time(args.now) if args.now else datetime.datetime.now(datetime.timezone.utc)
    if now is None:
        ap.error('--now must be an ISO 8601 time')
    stats = {'skipped': 0}
    records = read_runs(args.log, stats)
    runs = [r for r in records if since is None or r['_time'] is None or r['_time'] >= since]
    if args.health:
        warns, info = run_warnings(args.projects, runs, now, records)
        print('\n'.join((warns or ['Health: no problems']) + info))
        return 0
    tr = read_transcripts(args.projects, since, stats)
    print(brief_report(tr, runs, now, records, args.projects) if args.brief else full_report(tr, runs, stats, since, now))
    return 0


if __name__ == '__main__':
    sys.exit(main())
