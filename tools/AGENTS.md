# Tool guidance

## Scope and sources of truth

This directory holds standalone packages, each with its own rules below:
[`tools/nous-playwright`](nous-playwright/) and
[`tools/factory-ui`](factory-ui/).

### nous-playwright

The [`tools/nous-playwright`](nous-playwright/) package's
[`README.md`](nous-playwright/README.md), `CODEX_HANDOFF.md`, package manifest,
Playwright config, and `.gitignore` are the sources of truth. It is not the
repository's older global E2E setup and does not inherit seeded credentials,
global setup/teardown, or a root web-server lifecycle.

The package is pinned to Node 24 and `pnpm@10.18.2`; its isolated install uses
`--ignore-workspace`. The test targets a real NOUS service when authenticated,
so a discovery result and the earlier manual walkthrough are not an
authenticated end-to-end pass.

### factory-ui

[`tools/factory-ui`](factory-ui/) is a read-only web UI for the SWE factory,
which lives outside the repo. Its [`README.md`](factory-ui/README.md) is the
source of truth. Python standard library only; no npm, no build step, no
third-party package.

- Read-only toward the factory directory: the server opens files read-only,
  keeps its cache in memory and writes no file. No route, form or button
  changes factory state.
- It binds `127.0.0.1` only; nothing changes the bind address.
- It runs only the helper commands in `factory_ui/runner.py` `ALLOWED`
  (`doctor`, `budget`), and `runner.py` is the only module that imports
  `subprocess`. A new command needs a new proof from the helper code that it
  writes nothing and changes no state, recorded in the README.
- No committed file holds a home path, a user or host name, a session id, a
  key or a token. Test fixtures are invented and copy only the shape of the
  real files; tests never touch `~/.claude`.
- Colours live only in `static/tokens.css`; file text reaches the page only
  through text nodes or `static/render.js`.
- Verify with
  `python3 -m pytest tools/factory-ui/tests --confcutdir=tools/factory-ui/tests -q -p no:cacheprovider --no-cov`
  from the repository root. CI does not run these tests.

## Invalid patterns (nous-playwright)

- Keep this package standalone. Do not add its private auth/session or runtime
  artifacts to the root E2E suite, root lockfile, or a commit without an
  explicitly reviewed integration plan and the current suite's setup/teardown
  audit.
- Treat `.auth/`, `test-results/`, `playwright-report/`, traces, videos,
  screenshots, and `created-resources.json` as private workspace artifacts.
  They may contain authenticated session state or workspace content; do not
  commit, upload, paste, or quote them in reports. The package `.gitignore`
  is part of this boundary.
- Never embed, request, or print passwords, tokens, or storage-state contents.
  `pnpm auth` is interactive authentication through the user's normal login;
  it requires explicit user authorization and the saved state file remains
  private.
- An authenticated test creates a unique project and conversation and retains
  them for inspection. Do not run it, create live projects, approve live tool
  actions, or otherwise mutate a live project without explicit user
  authorization for the target environment. Do not delete the existing source
  paper or remove approval gates/retries to obtain a green result.
- Do not describe `pnpm test:list`, static selector checks, or a manual
  walkthrough as proof of authenticated persistence, citation, approval,
  reload, or live API behavior. Preserve one worker and zero automatic retries when the real test is authorized.

## Required workflow (nous-playwright)

Before editing, read the package README and handoff, then inspect its own
config and test consumers. Keep the install boundary isolated and use the
matching Node/pnpm versions. For a real run, confirm the target URL and
matching indexed-paper identity together, obtain authentication through the
user's normal flow, and record only non-sensitive resource/artifact locations
privately. If auth or a live target is unavailable, finish static/discovery
checks and report the live run as blocked rather than inventing credentials or
a passing result.

## Verification (nous-playwright)

The only offline structural check for this package is test discovery, run from
`tools/nous-playwright` with its isolated dependencies installed:

```sh
pnpm test:list
```

This lists Playwright tests and does not authenticate, contact the target
service, create a project, or prove the end-to-end workflow. The authenticated
test and interactive auth are service/credential-dependent and require the
separate authorization described above; if not run, report them as `NOT RUN`
or blocked.
