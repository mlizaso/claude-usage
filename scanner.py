"""Source-checkout launcher and compatibility alias for the scanner."""

if __name__ == "__main__":
    import runpy

    runpy.run_module("codex_claude_usage.scanner", run_name="__main__")
else:
    from codex_claude_usage._compat import alias_module as _alias_module

    _alias_module(__name__, "codex_claude_usage.scanner")
