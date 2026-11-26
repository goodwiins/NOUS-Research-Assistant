# tools/

Standalone developer tooling that is not part of the application build.
Each package has its own README and rules:

- [`nous-playwright/`](nous-playwright/README.md): an isolated Playwright
  setup (Node 24, pnpm 10.18.2) with its own config, unit tests and handoff
  notes. It is separate from the main end-to-end suite in `tests/e2e/` and
  does not inherit that suite's seeded credentials or global setup.
- [`factory-ui/`](factory-ui/README.md): a local, read-only web UI for the SWE
  factory (Python standard library). It binds 127.0.0.1 only, writes nothing,
  and runs only the helper commands on its closed allowlist.

See [`AGENTS.md`](AGENTS.md) for the editing rules.
