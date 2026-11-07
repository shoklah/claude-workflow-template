"""PreToolUse hook: stop Bash commands that would commit directly to main or master.

Answers "ask", so you approve or refuse in the permission prompt. Nothing Claude can type skips that
prompt. In bypassPermissions mode, where the docs don't promise a prompt, it answers "deny".
It says nothing about every other command, so the normal permission flow and any other hooks carry on.

It only reads the command text, so it can't see commits made inside scripts or other programs.
If the hook itself breaks, the command goes ahead: a bug here must not stop all Bash use.
"""
import json
import os
import re
import shlex
import subprocess
import sys

PROTECTED = {'main', 'master'}
OPERATORS = set(';&|()\n')
SHELLS = {'bash', 'sh', 'zsh', 'dash', 'ksh'}
SHELL_WORDS = {'if', 'then', 'elif', 'else', 'fi', 'do', 'done', 'while', 'until', '{', '}', '!'}
PREFIX_COMMANDS = {'command', 'builtin', 'exec', 'nohup', 'time', 'sudo'}
GIT_LOCATION_VARS = {'GIT_DIR', 'GIT_WORK_TREE'}
GIT_OPTIONS_WITH_VALUE = {'-c', '--namespace', '--config-env', '--super-prefix'}
HEREDOC = re.compile(r"(?<!<)<<-?\s*(['\"]?)([A-Za-z_][\w.-]*)\1")
ASSIGNMENT = re.compile(r'^([A-Za-z_]\w*)=')
VARIABLE = re.compile(r'\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))')
DECLARATIONS = {'export', 'readonly', 'declare', 'typeset', 'local'}
MENTIONS_GIT_COMMIT = re.compile(r'\bgit\b.*\bcommit\b', re.S)
UNKNOWN = object()  # A folder or branch that can't be worked out from the command text.


def strip_heredoc_bodies(command):
    lines = command.split('\n')
    kept = []
    i = 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        i += 1
        for match in HEREDOC.finditer(line):
            for j in range(i, len(lines)):
                if lines[j].lstrip('\t') == match.group(2):
                    i = j + 1
                    break
    return '\n'.join(kept)


def split_commands(command):
    """Yield one word list per simple command, plus '(' and ')' for subshells."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|()\n')
    lexer.whitespace = ' \t\r'
    lexer.whitespace_split = True
    lexer.commenters = ''
    words = []
    for token in lexer:
        if token and set(token) <= OPERATORS:
            if words:
                yield words
                words = []
            for char in token:
                if char in '()':
                    yield char
        else:
            words.append(token)
    if words:
        yield words


def strip_prefixes(words):
    """Drop what can sit before the real command. Returns (words, git_location_was_overridden)."""
    overridden = False
    i = 0
    while i < len(words):
        word = words[i]
        base = os.path.basename(word)
        assignment = ASSIGNMENT.match(word)
        if word in SHELL_WORDS:
            i += 1
        elif assignment:
            overridden = overridden or assignment.group(1) in GIT_LOCATION_VARS
            i += 1
        elif base == 'env':
            i += 1
            while i < len(words) and words[i].startswith('-'):
                i += 2 if words[i] == '-u' else 1
        elif base in PREFIX_COMMANDS:
            i += 1
            while i < len(words) and words[i].startswith('-'):
                i += 1
        elif base == 'timeout':
            i += 1
            while i < len(words) and words[i].startswith('-'):
                i += 1
            i += 1  # The duration.
        else:
            break
    return words[i:], overridden


def expand(text, variables):
    """Fill in $NAME and ${NAME} from earlier in this command. Returns UNKNOWN if any value can't be worked out."""
    known = True

    def fill(match):
        nonlocal known
        name = match.group(1) or match.group(2)
        value = os.path.expanduser('~') if name == 'HOME' else variables.get(name, UNKNOWN)
        if value is UNKNOWN:
            known = False
            return ''
        return value

    filled = VARIABLE.sub(fill, text)
    return filled if known and not re.search(r'[$`]', filled) else UNKNOWN


def record_assignments(words, variables):
    for word in words:
        assignment = ASSIGNMENT.match(word)
        if assignment:
            variables[assignment.group(1)] = expand(word[assignment.end():], variables)


def resolve(base, path, variables):
    path = expand(path, variables)
    if path is UNKNOWN:
        return UNKNOWN
    if path == '~' or path.startswith('~/'):
        path = os.path.expanduser('~') + path[1:]
    if re.search(r'[*?]', path) or path == '-':
        return UNKNOWN
    if os.path.isabs(path):
        return os.path.normpath(path)
    if base is UNKNOWN:
        return UNKNOWN
    return os.path.normpath(os.path.join(base, path))


def run_git(folder, *args):
    # Git variables inherited from the harness must not redirect these lookups.
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    try:
        return subprocess.run(['git', '-C', folder, *args], capture_output=True, text=True, timeout=5, env=env)
    except (OSError, subprocess.SubprocessError):
        return None


def git_output(folder, *args):
    result = run_git(folder, *args)
    return result.stdout.strip() if result and result.returncode == 0 else None


def parse_git(args, folder, variables):
    """Returns (folder, subcommand, rest). The folder is UNKNOWN when options move git elsewhere."""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == '-C' and i + 1 < len(args):
            folder = resolve(folder, args[i + 1], variables)
            i += 2
        elif arg in ('--git-dir', '--work-tree'):
            folder = UNKNOWN
            i += 2
        elif arg.startswith(('--git-dir=', '--work-tree=')):
            folder = UNKNOWN
            i += 1
        elif arg in GIT_OPTIONS_WITH_VALUE:
            i += 2
        elif arg.startswith('-'):
            i += 1
        else:
            return folder, arg, args[i + 1:]
    return folder, None, []


def switch_target(subcommand, rest, folder):
    """The branch a checkout or switch lands on, or None if it stays on the current one."""
    creating = {'checkout': ('-b', '-B', '--orphan'), 'switch': ('-c', '-C', '--create', '--force-create', '--orphan')}
    for i, arg in enumerate(rest):
        if arg in creating[subcommand] and i + 1 < len(rest):
            return rest[i + 1]
    if '--' in rest:
        return None  # "git checkout main -- file" restores a file and stays put.
    names = [arg for arg in rest if not arg.startswith('-')]
    if not names:
        return None
    is_branch = run_git(folder, 'rev-parse', '-q', '--verify', f'refs/heads/{names[0]}')
    return names[0] if is_branch and is_branch.returncode == 0 else None


def stop_reason(command, folder, switched=None, variables=None):
    """Returns why this command should stop, or None if it is fine."""
    switched = {} if switched is None else switched
    variables = {} if variables is None else variables
    stack = []
    for item in split_commands(strip_heredoc_bodies(command)):
        if item == '(':
            stack.append((folder, dict(switched), dict(variables)))
            continue
        if item == ')':
            if stack:
                folder, switched, variables = stack.pop()
            continue
        words, overridden = strip_prefixes(item)
        if not words:
            # Only assignments, so they set shell variables. Before a command they would apply to that
            # command alone, after the shell has already expanded its arguments.
            record_assignments(item, variables)
            continue
        name = os.path.basename(words[0])
        if name in DECLARATIONS:
            record_assignments(words[1:], variables)
        elif name in ('cd', 'pushd'):
            paths = [w for w in words[1:] if not w.startswith('-') or w == '-']
            folder = resolve(folder, paths[0], variables) if paths else os.path.expanduser('~')
        elif name == 'eval':
            reason = stop_reason(' '.join(words[1:]), folder, switched, variables)
            if reason:
                return reason
        elif name in SHELLS:
            for i, word in enumerate(words[1:-1], start=1):
                if re.match(r'^-[A-Za-z]*c$', word):
                    # A child shell doesn't see this shell's unexported variables.
                    reason = stop_reason(words[i + 1], folder, dict(switched), {})
                    if reason:
                        return reason
                    break
        elif name == 'git':
            target, subcommand, rest = parse_git(words[1:], folder, variables)
            if overridden:
                target = UNKNOWN
            if subcommand in ('checkout', 'switch') and target is not UNKNOWN:
                top = git_output(target, 'rev-parse', '--show-toplevel')
                branch = switch_target(subcommand, rest, target) if top else None
                if branch:
                    switched[top] = branch
            elif subcommand == 'commit':
                reason = commit_reason(target, switched)
                if reason:
                    return reason
    return None


def commit_reason(folder, switched):
    if folder is UNKNOWN:
        return ("This command commits, but the guard can't tell which branch it would land on. "
                'Approve it only if that branch is not main or master.')
    top = git_output(folder, 'rev-parse', '--show-toplevel')
    if not top:
        return None  # Not a repository, so git will refuse anyway.
    branch = switched.get(top)
    if branch is None:
        branch = git_output(folder, 'symbolic-ref', '--short', '-q', 'HEAD')
        if not branch:
            return None  # Detached HEAD.
        if not git_output(folder, 'rev-parse', '-q', '--verify', 'HEAD'):
            return None  # A repository's first commit has nothing on the branch to protect.
    if branch in PROTECTED:
        return f"This commits directly to '{branch}' in {top}. Approve it only if you want this commit on '{branch}'."
    return None


def decide(payload):
    if payload.get('tool_name') != 'Bash':
        return None
    command = payload['tool_input']['command']
    if not isinstance(command, str) or 'commit' not in command:
        return None
    folder = payload.get('cwd') or os.getcwd()
    try:
        return stop_reason(command, folder)
    except ValueError:  # Quotes shlex can't balance.
        if MENTIONS_GIT_COMMIT.search(strip_heredoc_bodies(command)):
            return "The guard can't parse this command and it mentions a git commit. Approve it only if that is safe."
        return None


def main():
    try:
        payload = json.load(sys.stdin)
        reason = decide(payload)
    except Exception as error:  # A broken guard must not block Bash.
        print(f'guard-main-commit: {error}', file=sys.stderr)
        return
    if reason:
        decision = 'deny' if payload.get('permission_mode') == 'bypassPermissions' else 'ask'
        print(json.dumps({'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'permissionDecision': decision,
            'permissionDecisionReason': reason,
        }}))


if __name__ == '__main__':
    main()
