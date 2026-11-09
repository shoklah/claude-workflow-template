---
name: workflow-dashboard
description: Rebuild the workflow dashboard from the run log and transcripts, and publish it as a private artifact at the same link each time.
argument-hint: "[--since YYYY-MM-DD]"
disable-model-invocation: true
---

Rebuild the workflow dashboard and publish it as a private artifact. Running this command is the owner's yes to publish, for this one time.

## Build

Rebuild the page at its default path, passing any arguments on (`$ARGUMENTS`), then copy it into your scratchpad directory, since the artifact tools only publish files from the working folder or the scratchpad. The build takes about 30 seconds:

```bash
/usr/bin/python3 -I ~/.claude/scripts/workflow-report.py $ARGUMENTS --html && cp ~/.claude/workflow-dashboard.html <scratchpad>/
```

If it fails, show its error and stop. Read the page's summary line with `grep -m1 '^<p>Generated' ~/.claude/workflow-dashboard.html`, not the whole page.

## Publish

- The page holds personal data: project paths, session IDs and costs. Publish it private and never share it.
- Publish the scratchpad copy from disk, with a tool that takes a file path, such as `publish_files`. Never paste the page in as content.
- `~/.claude/workflow-dashboard-artifact.txt` holds the artifact's link from the last run. If it exists, update that artifact in place, so the link stays the same.
- If the file is missing, or that artifact no longer exists, publish a new one titled "Workflow dashboard" with the icon "chart", and write its link into the file, alone on one line.

## Finish

Give the link and the summary line: its window, run counts, points and estimated cost. If publishing isn't possible, say why and give the path `~/.claude/workflow-dashboard.html` instead.
