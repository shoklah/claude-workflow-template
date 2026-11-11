"""PreToolUse hook on Agent: stop dispatches that would run on the wrong model or skip a simplify.

Denies a dispatch with no model when subagent_type is missing or general-purpose, since it would silently run on
the main session's model. Then two ordering rules, each needing its own unlock per review:
- a final review is denied when the session's transcript shows no simplify call since the last final review that
  launched. A final review is a final-reviewer dispatch, or a general-purpose or untyped one whose description says
  final review. The exception is a pair's second half: a final-reviewer passing a Fable model is allowed when exactly
  one final review, not passing a Fable model, launched since the last simplify.
- a plan review is denied when the transcript shows no launched plan simplify since the last plan review that
  launched. A plan review is a plan-reviewer dispatch, or a general-purpose or untyped one whose description says plan
  review; a plan simplify is a plan-simplifier dispatch, or a general-purpose or untyped one with a model whose
  description says plan simplify.
A dispatch is launched once its tool_result is not an error. The hook says nothing about every other dispatch, so the
normal permission flow carries on.

If the hook itself breaks, or a transcript can't be read, the dispatch goes ahead: a bug here must not stop all
Agent use.
"""
import heapq
import json
import os
import re
import sys

UNNAMED_TYPES = {None, '', 'general-purpose'}
SUBAGENT_FOLDER = '/subagents/'
# A typed slash command starts the message with these tags; a message that only quotes them further in does not count.
SIMPLIFY_COMMAND = re.compile(r'\s*(<command-message>.*?</command-message>\s*)?<command-name>/simplify</command-name>', re.S)
UNNAMED_REASON = ("This dispatch has no model and no named agent, so it would silently run on the main session's "
                  'model. Use a named agent (implementer, implementer-copy, implementer-escalated, plan-reviewer, '
                  'plan-simplifier, task-reviewer, fix-checker, final-reviewer) or pass a model.')
SIMPLIFY_REASON = ("No simplify since the last final review in this session's transcript. Run the simplify skill, "
                   'then dispatch the final review again.')
PLAN_SIMPLIFY_REASON = ("No plan-simplifier since the last plan review in this session's transcript. Dispatch "
                        'plan-simplifier, apply its findings, then dispatch the plan review again.')
# A general-purpose or untyped dispatch is a final review when its description says so, and is not a fix wave or a
# fix-check of one.
FINAL_REVIEW_DESCRIPTION = re.compile(r'final[ -]review', re.I)
NOT_FINAL_REVIEW = re.compile(r'fix|check', re.I)
# The same for plan reviews and plan simplifies, which NOT_FINAL_REVIEW also excludes: a check that only mentions
# plan-simplifier is not a plan simplify.
PLAN_REVIEW_DESCRIPTION = re.compile(r'plan[ -](re-?)?review', re.I)
PLAN_SIMPLIFY_DESCRIPTION = re.compile(r'plan[ -]simplif', re.I)
# Only lines holding one of these can hold a row that either rule's events come from.
EVENT_NEEDLES = (b'simplify', b'"Agent"')


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


def passes_fable(tool_input):
    model = tool_input.get('model')
    return isinstance(model, str) and model.startswith(('fable', 'claude-fable'))


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


def dispatch_calls(row, is_kind):
    """(call_id, tool_input) for the Agent dispatches in this row whose input is_kind accepts."""
    if row.get('type') != 'assistant':
        return []
    return [(block['id'], block['input']) for block in row.get('message', {}).get('content') or []
            if block.get('type') == 'tool_use' and block.get('name') == 'Agent' and isinstance(block.get('id'), str)
            and isinstance(block.get('input'), dict) and is_kind(block['input'])]


def final_review_events(row):
    """(unlocks, call_id, tool_input) triples: a simplify call unlocks with no id to launch, a final review locks."""
    simplify = [(True, None, None)] if row_calls_simplify(row) else []
    return simplify + [(False, call_id, tool_input) for call_id, tool_input in dispatch_calls(row, is_final_review)]


def plan_review_events(row):
    """(unlocks, call_id, tool_input) triples: a plan simplify unlocks and a plan review locks, each only once it
    launched."""
    return ([(True, call_id, tool_input) for call_id, tool_input in dispatch_calls(row, is_plan_simplify)]
            + [(False, call_id, tool_input) for call_id, tool_input in dispatch_calls(row, is_plan_review)])


def launched_ids(row, ids):
    """The ids, among ids, that this row reports as launched: their tool_result is not an error.

    Only is_error decides this, never the result's text, which may be a review that says "not found" or "denied".
    """
    content = row.get('message', {}).get('content')
    if row.get('type') != 'user' or not isinstance(content, list):
        return []
    return [block['tool_use_id'] for block in content
            if block.get('type') == 'tool_result' and block.get('tool_use_id') in ids and block.get('is_error') is not True]


def ordered_events(path, row_events):
    """Return (timestamp, unlocks, call_id, tool_input) in file order, from row_events on each line holding one of
    EVENT_NEEDLES.

    A row with no timestamp takes the previous row's, so file order holds when it is merged with another transcript.
    """
    events, timestamp = [], ''
    with open(path, 'rb') as transcript:
        for line in transcript:
            if not any(needle in line for needle in EVENT_NEEDLES):  # Parsing only these keeps a huge transcript fast.
                continue
            try:
                row = json.loads(line)
                if isinstance(row.get('timestamp'), str):
                    timestamp = row['timestamp']
                found = row_events(row)
            except (ValueError, AttributeError, TypeError):  # A torn or odd line says nothing either way.
                continue
            events.extend((timestamp, unlocks, call_id, tool_input) for unlocks, call_id, tool_input in found)
    return events


def launched_dispatches(paths, ids):
    needles = [call_id.encode() for call_id in ids]
    launched = set()
    for path in paths:
        with open(path, 'rb') as transcript:
            for line in transcript:
                if not any(needle in line for needle in needles):  # Only lines naming one of the ids can hold its result.
                    continue
                try:
                    launched.update(launched_ids(json.loads(line), ids))
                except (ValueError, AttributeError, TypeError):
                    continue
    return launched


def locks_since_last_unlock(paths, row_events):
    """The tool inputs of the locks after the last unlock, in order, or None when no unlock counts yet. Raises OSError
    if a file can't be read.

    An event with a call_id counts once that dispatch launched: it has a tool_result that is not an error, so the
    dispatch being judged, which has none yet, and one that was refused or not found don't count. Rows from several
    transcripts merge by timestamp.
    """
    per_file = [ordered_events(path, row_events) for path in paths]
    ids = {call_id for events in per_file for _, _, call_id, _ in events if call_id is not None}
    launched = launched_dispatches(paths, ids) if ids else set()
    locks, unlocked = [], False
    for _, unlocks, call_id, tool_input in heapq.merge(*per_file, key=lambda event: event[0]):
        if call_id is None or call_id in launched:
            if unlocks:
                locks, unlocked = [], True
            else:
                locks.append(tool_input)
    return locks if unlocked else None


def transcripts_to_scan(path):
    if SUBAGENT_FOLDER in path:  # <session>/subagents/agent-x.jsonl: the session's own record is <session>.jsonl.
        return [path, path.rsplit(SUBAGENT_FOLDER, 1)[0] + '.jsonl']
    return [path]


def ordering_reason(path, row_events, reason, pair=False):
    """reason unless an unlock comes after the last lock in the transcripts, or, with pair, exactly one lock not
    passing Fable follows the last unlock; None when there is no path or one can't be read. The pair rule is
    scripts/workflow-report.py's pair_halves, which counts these dispatches: keep them the same."""
    if not isinstance(path, str) or not path:
        return None
    try:
        locks = locks_since_last_unlock(transcripts_to_scan(path), row_events)
    except OSError:
        return None
    if locks is not None and (not locks or pair and len(locks) == 1 and not passes_fable(locks[0])):
        return None
    return reason


def decide(payload):
    """Return the reason to refuse this dispatch, or None to stay silent."""
    if payload.get('tool_name') != 'Agent' or not isinstance(payload.get('tool_input'), dict):
        return None
    tool_input = payload['tool_input']
    if tool_input.get('subagent_type') in UNNAMED_TYPES and not tool_input.get('model'):
        return UNNAMED_REASON
    if is_final_review(tool_input):
        pair = tool_input.get('subagent_type') == 'final-reviewer' and passes_fable(tool_input)
        return ordering_reason(payload.get('transcript_path'), final_review_events, SIMPLIFY_REASON, pair)
    if is_plan_review(tool_input):
        return ordering_reason(payload.get('transcript_path'), plan_review_events, PLAN_SIMPLIFY_REASON)
    return None


def main():
    try:
        reason = decide(json.load(sys.stdin.buffer))
        if reason:
            output = {'hookSpecificOutput': {
                'hookEventName': 'PreToolUse',
                'permissionDecision': 'deny',
                'permissionDecisionReason': reason,
            }}
            # Unbuffered, so a closed pipe fails here and not as a nonzero exit while Python shuts down.
            os.write(1, json.dumps(output).encode() + b'\n')
    except Exception as error:  # A broken guard must not block Agent.
        print(f'guard-agent-dispatch: {error}', file=sys.stderr)


if __name__ == '__main__':
    main()
