#!/usr/bin/env bash
# Install the Claude Usage VS Code extension on macOS / Linux / WSL.
# Usage:  ./scripts/install.sh [path/to/file.vsix]
# With no argument, always builds the exact reviewed checkout before installing.

set -euo pipefail
repo_root="$(cd "$(dirname "$0")/.." && pwd)"

find_code_cli() {
    for name in code code-insiders; do
        if command -v "$name" >/dev/null 2>&1; then
            echo "$name"; return 0
        fi
    done
    for path in \
        "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code" \
        "/Applications/Visual Studio Code - Insiders.app/Contents/Resources/app/bin/code-insiders" \
    ; do
        [ -x "$path" ] && { echo "$path"; return 0; }
    done
    echo "Could not find VS Code CLI. Install VS Code or add 'code' to PATH." >&2
    return 1
}

vsix="${1-}"
if [ -z "$vsix" ]; then
    cd "$repo_root"
    # Always reconstruct dependencies from the lockfile. Reusing an existing
    # node_modules would let stale or locally modified build tooling affect the
    # VSIX while claiming to package the reviewed checkout.
    npm ci --ignore-scripts --omit=optional --no-audit --no-fund
    npm audit signatures
    npm audit --ignore-scripts --audit-level=low
    npm run package
    package_name="$(node -p "require('./package.json').name")"
    package_version="$(node -p "require('./package.json').version")"
    vsix="$repo_root/${package_name}-${package_version}.vsix"
fi

if [ -z "$vsix" ] || [ ! -f "$vsix" ]; then
    echo "Expected packaged .vsix was not produced: $vsix" >&2
    exit 1
fi

code_cli="$(find_code_cli)"

# v1.7.0 renamed the extension from claude-usage-private to claude-usage, so its
# id moved from mlizaso.claude-usage-private to mlizaso.claude-usage while every
# contribution id stayed byte-identical -- the same four commands, the same view
# container, the same view and the same three settings. `--install-extension
# --force` overwrites the SAME id and does not uninstall a different one, so
# without this line an existing user of this fork ends up with both extensions
# installed and enabled, each declaring all of that. Removing the old one is the
# only mechanism available: a VS Code manifest cannot declare that it supersedes
# a previous id.
#
# Best-effort on purpose. A new user does not have it, and `code` exits non-zero
# when asked to uninstall an extension it cannot find -- which under
# `set -euo pipefail` would abort the install before it started. A command run as
# an `if` condition is exempt from errexit, which is why the call sits there
# rather than behind a trailing `|| true`.
legacy_extension_id="mlizaso.claude-usage-private"
if "$code_cli" --uninstall-extension "$legacy_extension_id" >/dev/null 2>&1; then
    echo "Removed the superseded extension $legacy_extension_id."
fi

echo "Installing $vsix via $code_cli ..."
"$code_cli" --install-extension "$vsix" --force
echo "Done. Reload VS Code (Cmd+Shift+P → Reload Window) to see the Claude Usage sidebar."
