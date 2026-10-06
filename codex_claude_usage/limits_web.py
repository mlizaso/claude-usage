"""Assemble the standalone quota page without importing the full dashboard."""

from .assets import asset_roots, find_asset_dir


def find_limits_web_dir():
    return find_asset_dir(__file__, "web/limits", "index.html")


def load_page_template():
    """Read and assemble immutable page bytes once at process start."""
    web = find_limits_web_dir()
    if web is None:
        searched = ", ".join(
            str(root / "web" / "limits") for root in asset_roots(__file__))
        raise RuntimeError(
            "Quota web assets not found. Looked for "
            f"web/limits/index.html in: {searched}.")
    shell = (web / "index.html").read_text(encoding="utf-8")
    css = (web / "app.css").read_text(encoding="utf-8")
    script = (web / "app.js").read_text(encoding="utf-8")
    if "</script" in script.lower():
        raise RuntimeError("Quota page script contains a closing script tag")
    for marker in ("__LIMITS_CSS__", "__LIMITS_JS__", "__CSP_NONCE__"):
        if marker not in shell:
            raise RuntimeError(f"Quota page template is missing {marker}")
    return shell.replace("__LIMITS_CSS__", css).replace("__LIMITS_JS__", script)


PAGE_TEMPLATE = load_page_template()


def render_page(nonce):
    """Return one response document carrying a request-specific CSP nonce."""
    return PAGE_TEMPLATE.replace("__CSP_NONCE__", nonce).encode("utf-8")
