class ClaudeUsage < Formula
  desc "Token, cost, and session dashboard for Claude Code and Codex"
  homepage "https://github.com/mlizaso/claude-usage"
  license "MIT"
  # Head-only by design: a stable formula embedded in its own source archive
  # cannot pin that archive without a self-referential checksum. Requiring
  # --HEAD also prevents this security-hardened fork from installing an older,
  # unhardened upstream release.
  head "https://github.com/mlizaso/claude-usage.git", branch: "main"

  depends_on "python@3.13"

  def install
    libexec.install "claude_usage"
    # The MIT notice travels with the software: the licence requires it in
    # "all copies or substantial portions", and a keg is one.
    libexec.install "LICENSE"
    libexec.install "vendor"
    libexec.install "web"

    # Reference the versioned interpreter (python3.13): modern python@3.x kegs
    # only ship "python3.13" in their bin — the unversioned "python3" symlink
    # lives in libexec/bin, so opt_bin/"python3" doesn't exist and the shim
    # fails at runtime with "No such file or directory" (#46).
    (bin/"claude-usage").write <<~EOS
      #!/bin/bash
      safe_env=(
        "HOME=$HOME"
        "PATH=#{formula_opt_bin("python@3.13")}:/usr/bin:/bin"
        "TMPDIR=${TMPDIR:-/tmp}"
        # How the user actually typed this. The shim runs `cli.py` through
        # runpy with `sys.argv=sys.argv[2:]`, so argv[0] inside Python is
        # `<libexec>/cli.py` and `safetext.invocation()` cannot tell this
        # surface from a git checkout -- it printed "run: python cli.py scan",
        # naming a file under libexec that a Homebrew user cannot run at all
        # (and could not, given `-I -S` and the shim's own sys.path setup).
        # Set here rather than sniffed in Python: this script is the only thing
        # that knows the name on PATH.
        "CLAUDE_USAGE_INVOKED_AS=claude-usage"
      )
      # `env -i` below erases everything this list does not name, so a variable
      # left out of it does not fail — it silently does nothing. Measured
      # 2026-08-10 against a keg replica of this shim: with CLAUDE_USAGE_RATES
      # dropped, `stats` reported $30.0000 where the same command from a clone
      # of the same commit reported $2.0000, and there is no --rates flag to
      # fall back to. tests/test_brew_shim_env.py holds this list, and the
      # reason each remaining CLAUDE_*/ANTHROPIC_* variable the product reads is
      # deliberately WITHHELD rather than added here — do not widen it without
      # classifying the new name there.
      passthrough=(
        LANG LANGUAGE LC_ALL LC_CTYPE
        HOST PORT
        CLAUDE_USAGE_DB CLAUDE_USAGE_RATES CLAUDE_USAGE_PROJECTS_DIRS
        CLAUDE_USAGE_DOCKER
        CLAUDE_USAGE_THRESHOLDS LIMITS_PORT
        CLAUDE_USAGE_LIVE_LIMITS CLAUDE_USAGE_LIMITS_URL
        CLAUDE_CONFIG_DIR CLAUDE_USAGE_CONFIG
      )
      for name in "${passthrough[@]}"; do
        if [[ -n "${!name:-}" ]]; then
          safe_env+=("$name=${!name}")
        fi
      done
      # TZ is tested for being SET, not for being non-empty, so it cannot join
      # the loop above: POSIX reads an empty TZ as UTC, so `${!name:-}` would
      # drop a deliberate `TZ=` and silently swap "force UTC" for the machine's
      # own zone — the local calendar day every report is bucketed on. Nothing
      # in Python reads TZ; libc and SQLite's `localtime` modifier do.
      if [[ -n "${TZ+x}" ]]; then
        safe_env+=("TZ=$TZ")
      fi
      exec /usr/bin/env -i "${safe_env[@]}" \
        "#{formula_opt_bin("python@3.13")}/python3.13" -I -S -c \
        'import runpy,sys; root=sys.argv[1]; sys.path.insert(0,root); sys.argv=sys.argv[1:]; runpy.run_module("claude_usage.cli",run_name="__main__")' \
        "#{libexec}" "$@"
    EOS
    chmod 0755, bin/"claude-usage"
  end

  test do
    # 1. No-args invocation prints the usage banner — exercises the shim.
    output = shell_output("#{bin}/claude-usage")
    assert_match "Claude Code Usage Dashboard", output
    assert_match "scan", output
    assert_match "dashboard", output

    # 2. `scan` against an empty projects dir exercises the real code path
    #    end-to-end (sqlite open, glob walk, summary print) without touching
    #    the user's real ~/.claude/usage.db. Homebrew's test sandbox provides
    #    testpath, so this stays isolated.
    (testpath/"projects").mkpath
    scan_output = shell_output("#{bin}/claude-usage scan --projects-dir #{testpath}/projects")
    assert_match "Scan complete", scan_output
  end
end
