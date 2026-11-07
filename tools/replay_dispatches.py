"""Replay every past Agent dispatch through the real dispatch hook and print what it would have refused.

Reads transcripts under ~/.claude/projects, or --projects DIR, and never writes to them.

Run with: /usr/bin/python3 -I tools/replay_dispatches.py [--projects DIR]
"""
import argparse
import collections
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, '..', 'hooks', 'guard-agent-dispatch.py')
PYTHON = '/usr/bin/python3'
MISSING_TYPE = '(missing)'
MAX_DESCRIPTIONS = 20


def transcripts(root):
    for folder, folders, names in os.walk(root):
        folders.sort()  # A stable order, so two runs list the same denied descriptions.
        for name in sorted(names):
            if name.endswith('.jsonl'):
                yield os.path.join(folder, name)


def dispatches_in(line):
    row = json.loads(line)
    if row.get('type') != 'assistant':
        return []
    return [block for block in row.get('message', {}).get('content') or []
            if block.get('type') == 'tool_use' and block.get('name') == 'Agent']


def hook_decision(tool_input, path):
    """Return 'deny' or None from the real hook, or raise ValueError if it breaks its contract."""
    payload = {'hook_event_name': 'PreToolUse', 'tool_name': 'Agent', 'tool_input': tool_input, 'transcript_path': path}
    p = subprocess.run([PYTHON, '-I', HOOK], input=json.dumps(payload).encode(), stdout=subprocess.PIPE,
                       stderr=subprocess.DEVNULL, timeout=60)
    if p.returncode != 0:
        raise ValueError(f'exit {p.returncode}')
    return json.loads(p.stdout)['hookSpecificOutput']['permissionDecision'] if p.stdout.strip() else None


def unique_dispatches(root, skipped):
    """Yield (tool_input, transcript path) once per call. skipped[0] counts what could not be read."""
    seen = set()  # Forks, resumed sessions and streaming log the same call more than once.
    for path in transcripts(root):
        try:
            with open(path, 'rb') as transcript:
                for line in transcript:
                    if b'"Agent"' not in line:  # This cheap test is what makes a 935 MB tree practical.
                        continue
                    try:
                        blocks = dispatches_in(line)
                        new = [b for b in blocks if not b.get('id') or b['id'] not in seen]
                        seen.update(b['id'] for b in new if b.get('id'))
                    except (ValueError, AttributeError, TypeError):
                        skipped[0] += 1
                        continue
                    for block in new:
                        yield block.get('input'), path
        except OSError:
            skipped[0] += 1


def replay(root):
    counts = collections.defaultdict(lambda: {'denied': 0, 'silent': 0})
    descriptions = []
    denied_with_model = hook_failures = 0
    skipped = [0]
    for tool_input, path in unique_dispatches(root, skipped):
        details = tool_input if isinstance(tool_input, dict) else {}
        try:
            decision = hook_decision(tool_input, path)
        except (ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
            hook_failures += 1
            continue
        label = str(details.get('subagent_type') or MISSING_TYPE)
        if decision == 'deny':
            counts[label]['denied'] += 1
            denied_with_model += bool(details.get('model'))
            descriptions.append((label, details.get('description', '(no description)')))
        else:
            counts[label]['silent'] += 1
    return counts, descriptions, denied_with_model, hook_failures, skipped[0]


def main():
    parser = argparse.ArgumentParser(description='Replay past Agent dispatches through the dispatch hook.')
    parser.add_argument('--projects', default=os.path.expanduser('~/.claude/projects'), help='transcript folder')
    args = parser.parse_args()
    if not os.path.isdir(args.projects):
        parser.error(f'no such folder: {args.projects}')

    counts, descriptions, denied_with_model, hook_failures, skipped = replay(args.projects)

    denied = sum(c['denied'] for c in counts.values())
    silent = sum(c['silent'] for c in counts.values())
    print(f'Dispatches: {denied + silent}, denied {denied}, silent {silent}')
    print(f'Denied with a model: {denied_with_model}')
    print(f'Hook failures: {hook_failures}')
    print(f'Bad lines or unreadable files skipped: {skipped}')
    print()
    print(f'{"subagent_type":<40}{"denied":>8}{"silent":>8}')
    for label, c in sorted(counts.items(), key=lambda item: (-item[1]['denied'], -item[1]['silent'], item[0])):
        print(f'{label:<40}{c["denied"]:>8}{c["silent"]:>8}')
    print()
    print(f'Denied descriptions (first {min(denied, MAX_DESCRIPTIONS)} of {denied}):')
    for label, description in descriptions[:MAX_DESCRIPTIONS]:
        print(f'  {label}: {" ".join(str(description).split())}')
    return 1 if hook_failures else 0


if __name__ == '__main__':
    sys.exit(main())
