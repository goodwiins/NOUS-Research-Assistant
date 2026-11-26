# factory-ui

A local, read-only web UI for the SWE factory on this Mac. The factory lives
outside the repo, in `~/.claude/factory/`; this server reads it at runtime and
shows:

- **Jobs**: every job folder with phase, round, PR, home Mac, age, Linear id,
  lease and cache age; per job the spec, build.md, each round's reviews and
  verdicts, the verify verdict and `diff-finding` flag, decision files,
  pr-body.md and related-tests.md.
- **Verifier**: `verify.mode`, the rows of `verify-backtest.jsonl`, the
  backtests block and the verify runs per round.
- **Budget**: `F budget` for the newest usage reading, plus every reading of
  the last 6 hours.
- **Health**: `F doctor` problems, leftovers and residuals.
- **Parallel**: a placeholder for parallel slots, claims and waiting jobs,
  from `parallel.mode` and `parallel-log.jsonl` once they exist.

It is Python standard library only (3.11 or newer) with static HTML, CSS and
JavaScript: no package manager, no build step, no third-party package.

## Start and stop

```sh
tools/factory-ui/run.sh                # 127.0.0.1:9417, sandboxed
tools/factory-ui/run.sh --no-sandbox   # without sandbox-exec
tools/factory-ui/run.sh --port 9420    # another port
```

Then open `http://127.0.0.1:9417/`. The server runs in the foreground; Ctrl-C
stops it. There is no launch agent and no background mode.

`run.sh` starts the server under `/usr/bin/sandbox-exec` with a profile that
denies every file write under `$HOME/.claude` and `$HOME/.config` (built from
`$HOME` at start), for the server and the helper commands it runs.
`--no-sandbox` skips it.

The bind address is always `127.0.0.1`. The default port is 9417. `--port`
refuses 3000-3010, 8000-8010, 4100-4899 (factory job and verify ports), the
helper's protected service ports (5432, 5433, 6379, 6333, 6334, 7474, 7687,
8080, 8888, 9000, 11434, 54321-54324), and anything below 1024 or above
49151. A busy port ends the start with an error; the server never picks
another port. A request whose `Host` is not `127.0.0.1:<port>` or
`localhost:<port>` gets 403.

## Allowlist

The server can run exactly two helper (`F`) commands, as fixed argv tuples in
`factory_ui/runner.py` (`ALLOWED`); the name comes from the server's route
table, never from the request:

| Command | Route | Why it is read-only (helper code) |
| --- | --- | --- |
| `F doctor` | `/api/health` | `cmd_doctor` pings Linear and lists its issues (queries, no mutation) without `all_jobs()`, so `write_targets` is never reached; `verify_doctor` reads files and tool versions and changes nothing. It prints the key file's name and a mode check, never the key. |
| `F budget` | `/api/budget` | `cmd_budget`, `budget_state`, `budget_limits` and `_budget_state` read the environment and one file, `usage/<session id>.json`. No write, no network. |

The child gets an environment built from scratch (`HOME`, `PATH`, `LANG`,
`LC_ALL`, `TMPDIR`, `USER`, `LOGNAME`, `PYTHONDONTWRITEBYTECODE=1`; for
`budget` also `CLAUDE_CODE_SESSION_ID`, set to the stem of the newest
`usage/*.json` by `seen_at`). It gets no terminal on stdin, so the helper's
owner-only paths (`is_owner()`) never apply. Exit 0 and 1 are both answers;
exit 6 shows as "helper bug".

A new command needs a new proof from the helper code before it joins
`ALLOWED`.

### Not run: `list`, `stats`, `show`

`F list` and `F stats` call `write_targets()`, which rewrites
`factory-targets.json`; `F show` rewrites the job's `job.json`. All three
write under `~/.claude`, so the UI reads files instead. `F verify-mode` is
not run either: the UI reads `verify.mode` with the helper's `verify_mode`
rules. `F slot` and `F parallel` are never run.

### Switching to `F list` and `F stats`

`factory_ui/sources.py` is the one place the server gets the Jobs rows
(`jobs_rows`), the backtests block (`backtests_block`) and the backtest rows
(`backtest_rows`). Today both read
files: `jobs/*/job.json` as the helper's `cached_jobs` and `merge` read it,
and `verify-backtest.jsonl` counted line by line like the helper's
`verify_stats`. The rows carry the `F list` field names (`id`, `phase`,
`round`, `pr`, `home`, `lease`) and the block uses the `F stats`
`verify.backtests` key names. Once the helper gains a no-write mode, switch
by changing `jobs_rows` and `backtests_block` to call it, and add the
commands to `ALLOWED` with a new proof. Nothing else changes. `F stats`
prints no rows, so `backtest_rows` stays a file read.

## Rules

- **No writes.** The server opens files `O_RDONLY | O_NOFOLLOW` only, keeps
  its cache in memory, logs to stderr and writes no file anywhere.
- **Reads.** Every read goes through one guard (`paths.safe_read`): it opens
  the path one component at a time from the factory root's descriptor with
  `O_NOFOLLOW`, so no symlink below the root is followed; the file must be a
  regular file and is cut at 2 MB with a notice. A `verify-backtest.jsonl`
  over 2 MB is flagged on the Verifier view. A job id must match the job-name pattern and
  be a folder `os.listdir` returned; an artifact key must be one of `spec`,
  `build`, `decision`, `decision-<n>`, `pr-body`, `related-tests`,
  `round-<n>/review-<lens>` or `round-<n>/shadow-verify`.
- **Secrets.** The server never opens the Linear key file or anything under
  `~/.config/`. `write_token`, `lease.session` and `docs` are never shown;
  session ids show by their first 8 characters at most, and never more
  than half of the id. Environment values are
  never logged or returned.
- **`## Re-run`.** A verify file's `## Re-run` section prints the verify
  sandbox's environment. The server replaces its body with "hidden:
  environment values; open the file on disk" in both the rendered and the
  raw view.
- **Rendering.** `static/render.js` builds DOM nodes from markdown, so no
  HTML in a file becomes an element; links keep only `http:`, `https:` and
  `#` targets. The page sends GET requests only and has no form.
- **Cache and rate limit**, enforced by the runner whatever the number of
  tabs or refresh clicks, one run per command at a time:

  | Command | Cache | Minimum interval | Timeout |
  | --- | --- | --- | --- |
  | `doctor` | 300 s (stale result served while one refresh runs) | 60 s | 120 s |
  | `budget` | 15 s | 10 s | 10 s |

  A failure or timeout is cached like a result. On timeout the server sends
  SIGTERM to the process group it started (SIGKILL after 5 s if it is still
  there). Output is read with bounded buffers; stdout over 1 MB stops the
  group and is an error. File views refetch every 15 s
  while the tab is visible.

## This Mac's cache

The Jobs view is this Mac's cache: the `cache` object in each `job.json` is
the shared state this Mac last read from Linear. A job another Mac created
and this Mac never read is missing, and a row can lag behind Linear; the
cache age column says by how much. The backtests block is recomputed from
this Mac's `verify-backtest.jsonl`.

## Tests

From the repository root, with a Python that has pytest:

```sh
python3 -m pytest tools/factory-ui/tests --confcutdir=tools/factory-ui/tests -q -p no:cacheprovider --no-cov
```

The tests build an invented factory tree under a temporary folder with a fake
helper that prints canned JSON; they never touch `~/.claude`. The view tests
render the frontend under `node` with a minimal DOM and are skipped without
node. CI does not run them.
