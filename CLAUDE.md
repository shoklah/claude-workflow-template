# claude-workflow

## Code conventions

- The run log only grows: no record is dropped or rewritten when its format changes, and every reader handles every past `schema` version, treating a missing field as absent.
- `workflow-report.py --json` carries a `version`: adding keys keeps it; renaming or removing a key bumps it.
- In `--json`, a fact the report could not read (a run with no window, a dispatch with no subagent transcript) is `null`, never `0` or empty.
- Output that holds personal data (the report's JSON and page, the ledger archive) is written outside every repo and is never exported.
- The `--html` page is built only from `report_data`'s dict, with no script and nothing loaded from outside; every value goes through `cell`, which escapes it and shows null as `–`.
- An agent whose report is checked ends it with one fixed `Name: key=N key=N` count line, which `--health` reads.
- `read_runs` cleans each run record once, keeping the parsed record as `_raw`: a malformed value reads as absent (None, 0 or `[]`), a step that is not a dict of a known kind is dropped, and a bool never counts as a number.
- Transcript usage counts once per `message.id`, from the row with the most output tokens.
- `PRICES` holds one price per model ID with its date suffix stripped; a model it lacks shows tokens only, with cost `null`.
- A run that spans a weekly usage reset (its `started` and `date` in different weeks, or, lacking either, points that went down) has `null` points in `run_entry`, and every points sum adds only the known ones.
- A run window is `(start, end]` compared as transcript timestamp strings: a time from anywhere else goes through `stamp` first.
- A definition a script can't import, from a hook or a test, is copied under a `keep them the same` comment naming its source.
- The dashboard page is published only by `/workflow-dashboard`, as a private artifact whose link is kept in `~/.claude/workflow-dashboard-artifact.txt`, so each run updates the same artifact.
- Tests point every `~/.claude` path option of the report (`--projects`, `--log`, `--agents`, `--export-stamp`) into their temp folder, so none reads the real home.
- A script that acts on a checkout (`failing-test-check.py`, `wave.py`) prints one JSON object on stdout and exits 0 when done, 1 when the state isn't what it expects and 2 on a usage error (on stderr, nothing on stdout), with 3 for `failing-test-check.py`'s `couldnt_run`; a refusal changes nothing.
- A new skill or command gets a row in the README's `### Offered skills and commands` table, with its moment or "never offered" and why, and an offered one gets its moment in the **Offers** bullet of `claude-md/agentic-workflow.md`.
