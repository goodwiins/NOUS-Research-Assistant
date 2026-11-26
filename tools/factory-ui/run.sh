#!/usr/bin/env bash
# Start the factory UI in the foreground on 127.0.0.1:9417. Ctrl-C stops it.
#
# By default the server runs under sandbox-exec with a profile that denies
# every file write under ~/.claude and ~/.config, for the server and the two
# helper commands it may run. --no-sandbox skips the sandbox. Other arguments
# (--port N) go to the server.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
sandbox=1
args=()
for arg in "$@"; do
  case "$arg" in
    --no-sandbox) sandbox=0 ;;
    *) args+=("$arg") ;;
  esac
done

cd "$here"
# -B: write no bytecode cache next to the package.
cmd=(python3 -B -m factory_ui ${args[@]+"${args[@]}"})

if [ "$sandbox" = 1 ]; then
  profile="(version 1)(allow default)(deny file-write* (subpath \"$HOME/.claude\") (subpath \"$HOME/.config\"))"
  exec /usr/bin/sandbox-exec -p "$profile" "${cmd[@]}"
fi
exec "${cmd[@]}"
