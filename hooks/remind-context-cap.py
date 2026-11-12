"""Stop hook: hold a turn's end once when the session is past the context cap with no handoff offer made.

The rule is scripts/workflow-report.py's cap_slips. Reading the session's transcript in file order, the threshold
starts at CONTEXT_CAP; an offer moves it to the latest context plus OFFER_STEP, but never under the cap, and a
compaction's summary row puts it back to the cap. An offer is a wrap-up action or an AskUserQuestion option whose
label OFFER_LABEL matches, or a start_session call whose brief starts with HANDOFF_MARKER. When the latest context is
over the threshold, the hook holds the turn and tells the model to make the offer, naming compaction as well from
COMPACT_AT. A turn already continuing because of a Stop hook is never held again, so the hold happens once.

If the hook itself breaks, or the transcript can't be read, the turn ends: a bug here must not stop sessions ending.
"""
import json
import os
import re
import sys

# Copied from scripts/workflow-report.py, since hooks can't import it: keep them the same.
WRAP_UP = 'mcp__ccd_turn__wrap_up'
START_SESSION = 'mcp__ccd_session__start_session'
HANDOFF_MARKER = re.compile(r'^Handoff \d+:')
CONTEXT_CAP = 300_000
OFFER_STEP = 50_000
COMPACT_AT = 450_000
OFFER_LABEL = re.compile(r'hand[ -]?off|compact', re.I)
ASK_USER = 'AskUserQuestion'


def context_tokens(own):
    """The context of a call: the input, cache-read and cache-write tokens of the largest of its own iterations own,
    each of which samples over the whole prefix."""
    return max((sum(num(i.get(name)) for name in ('input_tokens', 'cache_read_input_tokens',
                                                    'cache_creation_input_tokens')) for i in own), default=0)


def num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


# Only lines holding one of these can hold a context, an offer or a compaction.
NEEDLES = (b'"usage"', b'wrap_up', b'AskUserQuestion', b'start_session', b'isCompactSummary')
CAP_REASON = (f'Context is {{}}K, past the {CONTEXT_CAP // 1000}K cap. Before ending this turn, draft the handoff and '
              'offer it with a button labelled "Hand off": in the next-step box, or in a question where there is no '
              'box. A skip means not now.')
COMPACT_REASON = (f'Context is {{}}K, past {COMPACT_AT // 1000}K. Before ending this turn, offer compaction or the '
                  'handoff, with buttons labelled "Compact" and "Hand off": in the next-step box, or in a question '
                  'where there is no box. A skip means not now.')


def is_offer(block):
    inp = block.get('input')
    if block.get('type') != 'tool_use' or not isinstance(inp, dict):
        return False
    if block.get('name') == START_SESSION:
        return isinstance(inp.get('prompt'), str) and bool(HANDOFF_MARKER.match(inp['prompt']))
    if block.get('name') == WRAP_UP:
        choices = inp.get('actions')
    elif block.get('name') == ASK_USER:
        choices = [o for q in inp.get('questions') or [] if isinstance(q, dict) for o in q.get('options') or []]
    else:
        return False
    return any(isinstance(c, dict) and isinstance(c.get('label'), str) and OFFER_LABEL.search(c['label'])
               for c in choices or [])


def latest_and_threshold(path):
    """The latest call's context and the threshold it is compared with, reading the transcript in file order.

    Each assistant row's context is taken as it comes, without the reporter's once-per-message pick by output tokens,
    since a message's rows share their input side."""
    latest, threshold = 0, CONTEXT_CAP
    with open(path, 'rb') as transcript:
        for line in transcript:
            if not any(needle in line for needle in NEEDLES):  # Parsing only these keeps a huge transcript fast.
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            if row.get('type') == 'user' and row.get('isCompactSummary') is True:
                threshold = CONTEXT_CAP
            message = row.get('message')
            if row.get('type') != 'assistant' or not isinstance(message, dict):
                continue
            usage = message.get('usage')
            if isinstance(usage, dict):
                iterations = [i for i in usage.get('iterations') or [] if isinstance(i, dict)]
                own = [i for i in iterations if i.get('type') == 'message'] if iterations else [usage]
                latest = context_tokens(own)
            if any(isinstance(block, dict) and is_offer(block) for block in message.get('content') or []):
                threshold = max(CONTEXT_CAP, latest + OFFER_STEP)
    return latest, threshold


def main():
    try:
        payload = json.load(sys.stdin.buffer)
        if payload.get('stop_hook_active') is True or not isinstance(payload.get('transcript_path'), str):
            return
        latest, threshold = latest_and_threshold(payload['transcript_path'])
        if latest > threshold:
            reason = (COMPACT_REASON if latest >= COMPACT_AT else CAP_REASON).format(latest // 1000)
            # Unbuffered, so a closed pipe fails here and not as a nonzero exit while Python shuts down.
            os.write(1, json.dumps({'decision': 'block', 'reason': reason}).encode() + b'\n')
    except Exception:  # A broken hook must not stop a session from ending.
        pass


if __name__ == '__main__':
    main()
