"""PreToolUse hook on Agent: stop dispatches that would run on the wrong model or skip simplify.

Denies a dispatch with no model when subagent_type is missing or general-purpose, since it would silently run on
the main session's model. Denies a final-reviewer dispatch when the session's transcript shows no simplify call.
It says nothing about every other dispatch, so the normal permission flow carries on.

If the hook itself breaks, or a transcript can't be read, the dispatch goes ahead: a bug here must not stop all
Agent use.
"""
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
                  'task-reviewer, fix-checker, final-reviewer) or pass a model.')
SIMPLIFY_REASON = "This session's transcript shows no simplify call. Run the simplify skill first, then dispatch final-reviewer again."


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


def transcript_calls_simplify(path):
    with open(path, 'rb') as transcript:
        for line in transcript:
            if b'simplify' not in line:  # Parsing only these lines is what keeps a huge transcript fast.
                continue
            try:
                if row_calls_simplify(json.loads(line)):
                    return True
            except (ValueError, AttributeError, TypeError):  # A torn or odd line says nothing about simplify.
                continue
    return False


def transcripts_to_scan(path):
    if SUBAGENT_FOLDER in path:  # <session>/subagents/agent-x.jsonl: the session's own record is <session>.jsonl.
        return [path, path.rsplit(SUBAGENT_FOLDER, 1)[0] + '.jsonl']
    return [path]


def final_review_reason(path):
    if not isinstance(path, str) or not path:
        return None
    all_read = True
    for transcript in transcripts_to_scan(path):
        try:
            if transcript_calls_simplify(transcript):
                return None
        except OSError:
            all_read = False
    return SIMPLIFY_REASON if all_read else None


def decide(payload):
    """Return the reason to refuse this dispatch, or None to stay silent."""
    if payload.get('tool_name') != 'Agent' or not isinstance(payload.get('tool_input'), dict):
        return None
    tool_input = payload['tool_input']
    subagent_type = tool_input.get('subagent_type')
    if subagent_type == 'final-reviewer':
        return final_review_reason(payload.get('transcript_path'))
    if subagent_type in UNNAMED_TYPES and not tool_input.get('model'):
        return UNNAMED_REASON
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
