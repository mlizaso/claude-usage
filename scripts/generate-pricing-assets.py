#!/usr/bin/env python3
"""Render the browser and documentation pricing projections.

``claude_usage.pricing`` is the canonical pricing source.  The dashboard is a
classic-script bundle and the README is a published artifact, so neither can
import Python at runtime.  This small, stdlib-only renderer keeps those two
projections deterministic and gives CI a cheap ``--check`` drift guard.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
JS_PATH = ROOT / "web" / "js" / "10-pricing.js"
README_PATH = ROOT / "docs" / "README.md"
RATE_FIELDS = ("input", "output", "cache_write", "cache_read", "cache_write_1h")
DOC_RATE_FIELDS = ("input", "output", "cache_write", "cache_write_1h", "cache_read")
TABLE_HEADER = (
    "| Model | Input | Output | Cache Write (5m) | Cache Write (1h) | Cache Read |\n"
    "|-------|-------|--------|------------------|------------------|------------|"
)


def _load_source():
    # Import only after ROOT is known so the script works from any cwd and does
    # not require an installed package.
    sys.path.insert(0, str(ROOT))
    from claude_usage import pricing

    return pricing


def _number(value):
    """Format a finite pricing number as stable JavaScript source."""
    if isinstance(value, int):
        return str(value)
    text = format(value, ".15g")
    # JSON and JavaScript both accept this spelling, but lowercase exponent
    # output is nicer to diff than Python's occasional ``e+00`` variant.
    return text.replace("E", "e")


def _js_rate_object(rates):
    fields = ", ".join(f"{field}: {_number(rates[field])}" for field in RATE_FIELDS)
    return "{ " + fields + " }"


def _js_data(pricing):
    lines = [
        "const PRICING = {",
        "  // Generated from claude_usage.pricing.PRICING; do not edit by hand.",
    ]
    for model, rates in pricing.PRICING.items():
        # Canonical model ids are ordinary ASCII identifiers. JSON quoting is
        # still used for the policy values below, where dates are arbitrary
        # strings and may eventually contain a different spelling.
        lines.append(f"  '{model}': {_js_rate_object(rates)},")
    lines.extend([
        "};",
        "// Model ids are data, including JavaScript's magic object names. With an",
        "// ordinary prototype, assigning a user-supplied `__proto__` override changes",
        "// the table's prototype instead of adding that model. A null-prototype table",
        "// makes every string an ordinary own key.",
        "Object.setPrototypeOf(PRICING, null);",
        "",
        "// Generated from claude_usage.pricing.RATE_POLICIES; do not edit by hand.",
        "const RATE_POLICIES = Object.freeze({",
    ])
    for model, policy in pricing.RATE_POLICIES.items():
        lines.append(f"  {json.dumps(model)}: Object.freeze({{")
        if "start" in policy:
            lines.append(f"    start: {json.dumps(policy['start'])},")
            lines.append(
                f"    before: Object.freeze({_js_rate_object(policy['before'])}),"
            )
        if "until" in policy:
            lines.append(f"    until: {json.dumps(policy['until'])},")
        lines.append(
            f"    rates: Object.freeze({_js_rate_object(policy['rates'])}),"
        )
        if "after" in policy:
            lines.append(
                f"    after: Object.freeze({_js_rate_object(policy['after'])}),"
            )
        lines.extend(["  }),"])
    lines.extend([
        "});",
        "",
        "// Generated from the canonical long-context policy.",
        f"const LONG_CONTEXT_THRESHOLD = {pricing.LONG_CONTEXT_THRESHOLD};",
        "const LONG_CONTEXT_MODELS = Object.freeze([",
    ])
    lines.extend(
        f"  {json.dumps(model)}," for model in sorted(pricing.LONG_CONTEXT_MODELS)
    )
    lines.extend([
        "]);",
        "",
        "// Generated provenance and override-field contracts.",
        "let ESTIMATED_RATE_MODELS = Object.freeze([",
    ])
    lines.extend(
        f"  {json.dumps(model)}," for model in sorted(pricing.ESTIMATED_RATE_MODELS)
    )
    lines.extend([
        "]);",
        "const RATE_FIELDS = Object.freeze([",
    ])
    lines.extend(f"  {json.dumps(field)}," for field in pricing.RATE_FIELDS)
    lines.append("]);")
    return "\n".join(lines)


def _replace_between(text, begin, end, replacement):
    pattern = re.compile(
        rf"(?ms)(?P<prefix>^[ \t]*{re.escape(begin)}[ \t]*\n)"
        rf".*?"
        rf"(?P<suffix>^[ \t]*{re.escape(end)}[ \t]*$)"
    )
    match = pattern.search(text)
    if match is None:
        raise ValueError(f"missing generated block markers: {begin!r} / {end!r}")
    return text[:match.start()] + match.group("prefix") + replacement + "\n" + match.group("suffix") + text[match.end():]


def _money(value):
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    if "." not in text:
        text += ".00"
    elif len(text.rsplit(".", 1)[1]) == 1:
        text += "0"
    return f"${text}/MTok"


def _markdown_table(pricing, models):
    lines = [TABLE_HEADER]
    for model in models:
        rates = pricing.PRICING[model]
        suffix = " †" if model in pricing.ESTIMATED_RATE_MODELS else ""
        values = [_money(rates[field]) for field in DOC_RATE_FIELDS]
        lines.append("| " + model + suffix + " | " + " | ".join(values) + " |")
    return "\n".join(lines)


def _render_js(text, pricing):
    begin = "// BEGIN GENERATED PRICING DATA. Run scripts/generate-pricing-assets.py."
    end = "// END GENERATED PRICING DATA."
    return _replace_between(text, begin, end, _js_data(pricing))


def _render_readme(text, pricing):
    blocks = (
        ("anthropic", "claude-"),
        ("openai", None),
    )
    rendered = text
    for name, prefix in blocks:
        begin = f"<!-- BEGIN GENERATED PRICING TABLE: {name}. Run scripts/generate-pricing-assets.py. -->"
        end = f"<!-- END GENERATED PRICING TABLE: {name}. -->"
        models = [
            model for model in pricing.PRICING
            if (model.startswith(prefix) if prefix else not model.startswith("claude-"))
        ]
        rendered = _replace_between(rendered, begin, end,
                                    _markdown_table(pricing, models))
    return rendered


def render():
    pricing = _load_source()
    return {
        JS_PATH: _render_js(JS_PATH.read_text(encoding="utf-8"), pricing),
        README_PATH: _render_readme(README_PATH.read_text(encoding="utf-8"), pricing),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if generated files differ instead of writing them",
    )
    args = parser.parse_args(argv)
    changed = []
    for path, expected in render().items():
        actual = path.read_text(encoding="utf-8")
        if actual != expected:
            changed.append(path)
            if not args.check:
                path.write_text(expected, encoding="utf-8")
    if changed and args.check:
        for path in changed:
            print(f"pricing assets are stale: {path}", file=sys.stderr)
        return 1
    if changed:
        for path in changed:
            print(f"updated {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
