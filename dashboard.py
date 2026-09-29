"""Source-checkout launcher and alias for :mod:`claude_usage.dashboard`."""

if __name__ == "__main__":
    import runpy

    runpy.run_module("claude_usage.dashboard", run_name="__main__")
else:
    from claude_usage._compat import alias_module as _alias_module

    _alias_module(__name__, "claude_usage.dashboard")
