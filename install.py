"""Installs this repo's workflow files into $HOME/.claude, one step at a time, and rolls them back.

Usage: /usr/bin/python3 -I install.py {snapshot|agents|section|scripts|docs|skills|hooks|rollback SNAPSHOT_DIR}

  snapshot   Copies settings.json, CLAUDE.md and hooks/ from ~/.claude into a new folder,
             ~/.agents/backups/<YYYY-MM-DD-HHMM>-workflow/, and prints its path. Missing files are
             noted, not copied. If that folder already exists, a suffix -2, -3... is added; an
             existing folder is never written into.
  agents     Copies agents/*.md into ~/.claude/agents/.
  section    Puts claude-md/agentic-workflow.md between the agentic-workflow markers in
             ~/.claude/CLAUDE.md, appending the markers at the end when they are absent.
  scripts    Copies scripts/*.py into ~/.claude/scripts/.
  docs       Copies claude-md/kits.md into ~/.claude/docs/.
  skills     Copies skills/<name>/SKILL.md into ~/.claude/skills/<name>/.
  hooks      Copies hooks/* into ~/.claude/hooks/ and the hook tests into ~/.claude/hooks/tests/,
             then adds the Agent dispatch hook and the Stop context cap hook to ~/.claude/settings.json,
             each once.
  rollback   Restores what the snapshot holds, removes settings.json or CLAUDE.md if the snapshot
             found none, and removes every file install copies that the snapshot doesn't hold.
             Other files stay.

Every write goes through a temp file in the same folder and a rename. Invalid input stops with
exit 2 before anything is written.
"""
import json
import os
import shutil
import stat
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.abspath(__file__))
START = '<!-- agentic-workflow:start -->'
END = '<!-- agentic-workflow:end -->'
DISPATCH_COMMAND = '/usr/bin/python3 -I "$HOME/.claude/hooks/guard-agent-dispatch.py"'
DISPATCH_GROUP = {'matcher': 'Agent', 'hooks': [{'type': 'command', 'command': DISPATCH_COMMAND, 'timeout': 10}]}
CONTEXT_CAP_COMMAND = '/usr/bin/python3 -I "$HOME/.claude/hooks/remind-context-cap.py"'
CONTEXT_CAP_GROUP = {'hooks': [{'type': 'command', 'command': CONTEXT_CAP_COMMAND, 'timeout': 10}]}
HOOK_GROUPS = (('PreToolUse', 'guard-agent-dispatch.py', DISPATCH_GROUP),
               ('Stop', 'remind-context-cap.py', CONTEXT_CAP_GROUP))
HOOK_TESTS = ['test_hooks.py', 'test_dispatch.py', 'settings-snippet.json']
SNAPSHOT_MARK = 'snapshot.json'
SNAPSHOT_ITEMS = ['settings.json', 'CLAUDE.md', 'hooks']
USAGE = 'usage: install.py {snapshot|agents|section|scripts|docs|skills|hooks|rollback SNAPSHOT_DIR}'


class Stop(Exception):
    """Invalid input: report it and exit 2 without writing anything."""


def repo_files(folder, suffix='', dirs=False):
    path = os.path.join(REPO, folder)
    kind = os.path.isdir if dirs else os.path.isfile
    return sorted(f for f in os.listdir(path)
                  if f.endswith(suffix) and not f.startswith('.') and kind(os.path.join(path, f)))


def install_plan():
    """For each copying step, its (repo source, target relative to ~/.claude) pairs."""
    def pairs(source_folder, names, target_folder):
        return [(os.path.join(REPO, source_folder, f), os.path.join(target_folder, f)) for f in names]
    return {
        'agents': pairs('agents', repo_files('agents', '.md'), 'agents'),
        'scripts': pairs('scripts', repo_files('scripts', '.py'), 'scripts'),
        'docs': pairs('claude-md', ['kits.md'], 'docs'),
        'skills': pairs('skills', [os.path.join(n, 'SKILL.md') for n in repo_files('skills', dirs=True)], 'skills'),
        'hooks': (pairs('hooks', repo_files('hooks'), 'hooks')
                  + pairs('tests', HOOK_TESTS, os.path.join('hooks', 'tests'))),
    }


def write_atomic(path, data, mode):
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=folder, prefix='.' + os.path.basename(path) + '.')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        if os.path.exists(temp):
            os.unlink(temp)
        raise


def copy_atomic(source, target):
    with open(source, 'rb') as f:
        data = f.read()
    write_atomic(target, data, stat.S_IMODE(os.stat(source).st_mode))


def write_text(path, text):
    mode = stat.S_IMODE(os.stat(path).st_mode) if os.path.exists(path) else 0o644
    write_atomic(path, text.encode('utf-8'), mode)


def copy_into(claude, step):
    for source, relative in install_plan()[step]:
        copy_atomic(source, os.path.join(claude, relative))
        print('installed ' + os.path.join(claude, relative))


def snapshot(home, claude):
    backups = os.path.join(home, '.agents', 'backups')
    os.makedirs(backups, exist_ok=True)
    base = os.path.join(backups, time.strftime('%Y-%m-%d-%H%M') + '-workflow')
    folder, n = base, 1
    while True:
        try:
            os.mkdir(folder)
            break
        except FileExistsError:
            n += 1
            folder = '%s-%d' % (base, n)
    had = {}
    for item in SNAPSHOT_ITEMS:
        source = os.path.join(claude, item)
        had[item] = os.path.exists(source)
        if os.path.isdir(source):
            shutil.copytree(source, os.path.join(folder, item), symlinks=True)
        elif had[item]:
            shutil.copy2(source, os.path.join(folder, item))
    mark = {'had': had, 'taken': time.strftime('%Y-%m-%dT%H:%M:%S')}
    with open(os.path.join(folder, SNAPSHOT_MARK), 'w', encoding='utf-8') as f:
        f.write(json.dumps(mark, indent=2) + '\n')
    print(folder)


def section(claude):
    path = os.path.join(claude, 'CLAUDE.md')
    with open(os.path.join(REPO, 'claude-md', 'agentic-workflow.md'), encoding='utf-8') as f:
        body = f.read()
    if not body.endswith('\n'):
        body += '\n'
    block = START + '\n' + body + END
    text = ''
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            text = f.read()
    starts, ends = text.count(START), text.count(END)
    if starts == 0 and ends == 0:
        if text and not text.endswith('\n'):
            text += '\n'
        text = text + ('\n' if text else '') + block + '\n'
    elif starts == 1 and ends == 1 and text.index(START) < text.index(END):
        before, rest = text.split(START, 1)
        text = before + block + rest.split(END, 1)[1]
    else:
        raise Stop('%s: expected one %s marker before one %s marker, found %d start and %d end markers'
                   % (path, START, END, starts, ends))
    write_text(path, text)
    print('updated ' + path)


def load_settings(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            settings = json.load(f)
    except (ValueError, UnicodeDecodeError) as e:
        raise Stop('%s is not valid JSON: %s' % (path, e))
    if not isinstance(settings, dict):
        raise Stop('%s does not hold a JSON object' % path)
    hooks = settings.get('hooks', {})
    if not isinstance(hooks, dict):
        raise Stop('%s: "hooks" is not an object' % path)
    for event, _, _ in HOOK_GROUPS:
        if not isinstance(hooks.get(event, []), list):
            raise Stop('%s: "hooks.%s" is not a list' % (path, event))
    return settings


def has_hook(groups, script, matcher=None):
    # A group running the script counts even after the owner edits its timeout, path or quoting.
    for group in groups:
        if isinstance(group, dict) and group.get('matcher') == matcher and isinstance(group.get('hooks'), list):
            for hook in group['hooks']:
                if isinstance(hook, dict) and script in str(hook.get('command', '')):
                    return True
    return False


def hooks(claude):
    path = os.path.join(claude, 'settings.json')
    settings = load_settings(path)
    copy_into(claude, 'hooks')
    events = settings.setdefault('hooks', {})
    missing = [(event, group) for event, script, group in HOOK_GROUPS
               if not has_hook(events.get(event, []), script, group.get('matcher'))]
    if not missing:
        print('unchanged ' + path + ' (Agent and Stop hooks already present)')
        return
    for event, group in missing:
        events.setdefault(event, []).append(group)
    write_text(path, json.dumps(settings, indent=2, ensure_ascii=False) + '\n')
    print('updated ' + path)


def rollback(claude, folder):
    folder = os.path.abspath(folder)
    try:
        with open(os.path.join(folder, SNAPSHOT_MARK), encoding='utf-8') as f:
            had = json.load(f)['had']
        missing = [item for item in SNAPSHOT_ITEMS
                   if not isinstance(had.get(item), bool) or (had[item] and not os.path.exists(os.path.join(folder, item)))]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        raise Stop('%s is not a snapshot made by install.py snapshot: %s' % (folder, e))
    if missing:
        raise Stop('%s is not a complete snapshot: missing %s' % (folder, ', '.join(missing)))
    kept = set()
    for item in ('settings.json', 'CLAUDE.md'):
        target = os.path.join(claude, item)
        if had[item]:
            copy_atomic(os.path.join(folder, item), target)
            print('restored ' + target)
        elif os.path.exists(target):
            os.unlink(target)
            print('removed ' + target)
    if had['hooks']:
        saved = os.path.join(folder, 'hooks')
        for dirpath, _, files in os.walk(saved):
            for name in files:
                relative = os.path.join('hooks', os.path.relpath(os.path.join(dirpath, name), saved))
                copy_atomic(os.path.join(dirpath, name), os.path.join(claude, relative))
                kept.add(relative)
                print('restored ' + os.path.join(claude, relative))
    for relative in (rel for step in install_plan().values() for _, rel in step):
        target = os.path.join(claude, relative)
        if relative not in kept and os.path.lexists(target):
            os.unlink(target)
            print('removed ' + target)


def main(argv):
    commands = {'snapshot': 0, 'agents': 0, 'section': 0, 'scripts': 0, 'docs': 0, 'skills': 0, 'hooks': 0, 'rollback': 1}
    if not argv or argv[0] not in commands or len(argv) != 1 + commands[argv[0]]:
        print(USAGE, file=sys.stderr)
        return 2
    home = os.environ.get('HOME', '')
    if not home or not os.path.isabs(home):
        print('install.py: HOME must be an absolute path', file=sys.stderr)
        return 2
    claude = os.path.join(home, '.claude')
    command = argv[0]
    try:
        if command == 'snapshot':
            snapshot(home, claude)
        elif command in ('agents', 'scripts', 'docs', 'skills'):
            copy_into(claude, command)
        elif command == 'section':
            section(claude)
        elif command == 'hooks':
            hooks(claude)
        else:
            rollback(claude, argv[1])
    except Stop as e:
        print('install.py: ' + str(e), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
