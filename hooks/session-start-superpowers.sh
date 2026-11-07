#!/bin/sh
# SessionStart hook: puts the using-superpowers rules and the karpathy guidelines in context, so they don't
# depend on Claude choosing to load them. Claude Code adds this stdout
# as context. Each file is handled on its own: a missing one is noted on stderr and the other still prints.

# load TAG FILE INTRO WHAT: prints FILE wrapped in <TAG> after INTRO, or notes on stderr that WHAT didn't load.
load() {
  if [ -r "$2" ]; then
    echo "<$1>"
    echo "$3"
    cat "$2"
    echo "</$1>"
  else
    echo "session-start-superpowers: cannot read $2, so $4 not loaded" >&2
  fi
}

load using-superpowers-session-start "$HOME/.claude/skills/using-superpowers/SKILL.md" \
  'The using-superpowers skill is already loaded below, so it does not need invoking. Use the Skill tool for every other skill.' \
  'the using-superpowers rules were'
load karpathy-guidelines-session-start "$HOME/.claude/plugins/marketplaces/karpathy-skills/skills/karpathy-guidelines/SKILL.md" \
  'The karpathy-guidelines skill is already loaded below, so it does not need invoking.' \
  'the karpathy-guidelines were'

exit 0
