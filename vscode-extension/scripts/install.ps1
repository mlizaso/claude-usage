# Install the Codex / Claude Usage VS Code extension on Windows.
# Usage:  .\scripts\install.ps1 [path\to\file.vsix]
# With no argument, always builds the exact reviewed checkout before installing.

[CmdletBinding()]
param(
    [string]$Vsix = ""
)

$ErrorActionPreference = "Stop"
$ExtRoot = Split-Path -Parent $PSScriptRoot

function Assert-NativeSuccess {
    param([string]$Step)
    # Stop does not enforce native exit codes on Windows PowerShell or when
    # PSNativeCommandUseErrorActionPreference is disabled.
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE."
    }
}

function Find-CodeCli {
    foreach ($name in @("code.cmd", "code-insiders.cmd", "code", "code-insiders")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    foreach ($candidate in @(
        "$env:LOCALAPPDATA\Programs\Microsoft VS Code\bin\code.cmd",
        "$env:LOCALAPPDATA\Programs\Microsoft VS Code Insiders\bin\code-insiders.cmd"
    )) {
        if (Test-Path $candidate) { return $candidate }
    }
    throw "Could not find VS Code CLI. Install VS Code or add 'code' to PATH."
}

if (-not $Vsix) {
    Set-Location -LiteralPath $ExtRoot
    # Always reconstruct dependencies from package-lock.json so stale or
    # locally modified build tooling cannot affect the packaged extension.
    npm ci --ignore-scripts --omit=optional --no-audit --no-fund
    Assert-NativeSuccess "Dependency installation"
    npm audit signatures
    Assert-NativeSuccess "Dependency signature verification"
    npm audit --ignore-scripts --audit-level=low
    Assert-NativeSuccess "Dependency advisory audit"
    npm run package
    Assert-NativeSuccess "Extension packaging"
    $PackageName = node -p "require('./package.json').name"
    Assert-NativeSuccess "Reading the package name"
    $PackageVersion = node -p "require('./package.json').version"
    Assert-NativeSuccess "Reading the package version"
    $Vsix = Join-Path $ExtRoot "$PackageName-$PackageVersion.vsix"
}

if (-not $Vsix -or -not (Test-Path $Vsix)) {
    throw "Expected packaged .vsix was not produced: $Vsix"
}

$CodeCli = Find-CodeCli

# v1.7.0 renamed the extension from claude-usage-private to claude-usage, so its
# id moved from mlizaso.claude-usage-private to mlizaso.claude-usage while every
# contribution id stayed byte-identical -- the same four commands, the same view
# container, the same view and the same three settings. `--install-extension
# --force` overwrites the SAME id and does not uninstall a different one, so
# without this block an existing user of this fork ends up with both extensions
# installed and enabled, each declaring all of that. Removing the old one is the
# only mechanism available: a VS Code manifest cannot declare that it supersedes
# a previous id.
#
# Best-effort on purpose. A new user does not have it, and `code` exits non-zero
# when asked to uninstall an extension it cannot find. That must not abort the
# install, so $ErrorActionPreference is dropped to Continue for the duration.
#
# Which runtimes need that is version-dependent and NOT a simple "7.4+", which
# is what this comment used to claim. $PSNativeCommandUseErrorActionPreference
# makes a native command's non-zero exit a terminating error under Stop; it was
# introduced as an experimental feature, was on by default in 7.4, and is OFF
# again on current builds. Measured 2026-08-14 on pwsh 7.6.4: the variable reads
# False, and `& sh -c "exit 3"` under Stop does not throw -- with the bare call
# and with the `2>&1 | Out-Null` pipeline used below. So on 7.6.4 this guard is
# belt and braces; on 7.4, and on any build where a user or policy turns the
# preference on, it is what keeps a new user's install from aborting. Windows
# PowerShell 5.1 never had the behaviour. The try/catch and the $LASTEXITCODE
# reset cover all of them, which is why the guard stays despite being inert on
# the version measured here.
$LegacyExtensionId = "mlizaso.claude-usage-private"
$PreviousErrorAction = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    & $CodeCli --uninstall-extension $LegacyExtensionId 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Output "Removed the superseded extension $LegacyExtensionId."
    }
} catch {
    # Not installed, which is the common case. Nothing to remove.
} finally {
    $ErrorActionPreference = $PreviousErrorAction
    $global:LASTEXITCODE = 0
}

Write-Output "Installing $Vsix via $CodeCli ..."
& $CodeCli --install-extension $Vsix --force
Assert-NativeSuccess "Extension installation"
Write-Output "Done. Reload VS Code (Ctrl+Shift+P -> Reload Window) to see the Codex / Claude Usage sidebar."
