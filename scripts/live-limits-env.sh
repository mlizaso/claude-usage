#!/usr/bin/env bash
# Turn on live plan limits in the current shell, in one step.
#
#     source scripts/live-limits-env.sh
#
# SOURCE it, do not run it: a subprocess cannot export into your shell.
#
# What it sets, and why it sets a COMMAND rather than a token:
#
#   CODEX_CLAUDE_USAGE_TOKEN_COMMAND  re-read the credential for every query
#   CODEX_CLAUDE_USAGE_LIVE_LIMITS=1  the opt-in; without it nothing queries anything
#
# Claude Code's access token expires about twelve minutes after it is issued, so
# a token exported once works for a poll or two and then silently falls back to
# the stale cache -- which looks exactly like the feature being broken. Naming a
# command instead keeps it fresh, and keeps the credential out of your
# environment, where it would sit in /proc/<pid>/environ and in every shell that
# inherited it.
#
# The tool never reads your Keychain on its own. This is you telling it to.

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  echo "source this file, do not execute it:  source ${0}" >&2
  exit 1
fi

_cu_reader='python3 -c "import json,sys; print(json.load(sys.stdin)[\"claudeAiOauth\"][\"accessToken\"])"'

case "$(uname -s)" in
  Darwin)
    export CODEX_CLAUDE_USAGE_TOKEN_COMMAND="security find-generic-password -s 'Claude Code-credentials' -w | ${_cu_reader}"
    ;;
  *)
    # Linux/WSL keep the same JSON under ~/.claude/.credentials.json on the
    # installs that have it. If yours differs, set the variable yourself -- the
    # only contract is "prints an access token on stdout".
    export CODEX_CLAUDE_USAGE_TOKEN_COMMAND="cat \"\$HOME/.claude/.credentials.json\" | ${_cu_reader}"
    ;;
esac
unset _cu_reader

export CODEX_CLAUDE_USAGE_LIVE_LIMITS=1

# Prove it works now rather than at the next poll, and never print the token:
# a setup step that reports success without checking is how a 401 goes unnoticed
# for an hour.
if _cu_token="$(eval "$CODEX_CLAUDE_USAGE_TOKEN_COMMAND" 2>/dev/null)" && [ -n "$_cu_token" ]; then
  echo "live plan limits: ON  (token resolved, ${#_cu_token} chars, re-read per query)"
else
  echo "live plan limits: enabled, but the token command returned nothing." >&2
  echo "  try it directly:  $CODEX_CLAUDE_USAGE_TOKEN_COMMAND" >&2
  echo "  the dashboard will keep using the local cache until it works." >&2
fi
unset _cu_token
