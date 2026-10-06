import { describe, it, expect, vi } from "vitest";
import { renderHtml, escapeHtml, makeNonce, DashboardSidebar } from "../src/sidebar";

// Minimal vscode mock so we can instantiate DashboardSidebar in node-only tests.
//
// Uri.joinPath / Uri.file are real enough to exercise the *production* branch
// of resolveWebviewView (the one that runs whenever extensionUri is supplied,
// which extension.ts always does). workspaceFolders is present purely so a
// test can assert the webview's localResourceRoots never picks it up.
//
// Two arguments only: Vitest's signature is `mock(path, factory?)`, and a
// Jest-style third argument such as `{ virtual: true }` is discarded in
// silence while reading as though it declared something.
vi.mock("vscode", () => {
  const uri = (fsPath: string) => ({
    scheme: "file",
    fsPath,
    toString: () => `file://${fsPath}`,
  });
  return {
    Uri: {
      file: uri,
      joinPath: (base: { fsPath: string }, ...segments: string[]) =>
        uri([base.fsPath.replace(/\/$/, ""), ...segments].join("/")),
    },
    workspace: {
      workspaceFolders: [{ uri: uri("/Users/dev/some-cloned-repo") }],
    },
  };
});

const EXTENSION_DIR = "/Applications/vscode/extensions/claude-usage";

function fakeExtensionUri() {
  return { scheme: "file", fsPath: EXTENSION_DIR, toString: () => `file://${EXTENSION_DIR}` } as any;
}

/** Fake view carrying the webview API surface a real VS Code host provides. */
function makeFakeViewWithWebviewUri() {
  const view = makeFakeView() as any;
  view.webview.cspSource = "vscode-webview://deadbeef";
  view.webview.asWebviewUri = (u: { fsPath: string }) => ({
    toString: () => `https://deadbeef.vscode-cdn.net${u.fsPath}`,
  });
  return view;
}

function makeFakeView() {
  let html = "";
  // Counted, not just stored: "the document changed" and "the document was
  // written again" are different claims, and only the second distinguishes a
  // refresh() that re-rendered from one that returned early.
  let htmlWrites = 0;
  const disposeListeners: Array<() => void> = [];
  return {
    webview: {
      get html() { return html; },
      set html(v: string) { html = v; htmlWrites += 1; },
      options: undefined as unknown,
    },
    onDidDispose(listener: () => void) {
      disposeListeners.push(listener);
      return { dispose: () => {} };
    },
    _triggerDispose() { disposeListeners.forEach((l) => l()); },
    _html: () => html,
    _htmlWrites: () => htmlWrites,
  };
}

/**
 * Pull the Content-Security-Policy value out of a rendered document.
 *
 * Anchored on the http-equiv attribute rather than on the document's first
 * `content="…"` — that happens to be the CSP today only because `<meta charset>`
 * carries none. Measured: with the loose `/content="([^"]*)"/`, inserting a
 * `<meta name="viewport" content="width=device-width">` above the CSP meta
 * fails the iframe-pane assertion, i.e. an innocuous markup edit reads as a
 * policy regression. The anchor makes the same edit a no-op.
 */
function cspOf(html: string): string {
  const match = html.match(/http-equiv="Content-Security-Policy"\s+content="([^"]*)"/);
  expect(match).not.toBeNull();
  return match![1];
}

describe("escapeHtml", () => {
  it("escapes the five HTML-significant characters", () => {
    expect(escapeHtml(`<script>alert("x&y'z")</script>`))
      .toBe("&lt;script&gt;alert(&quot;x&amp;y&#39;z&quot;)&lt;/script&gt;");
  });

  it("passes through safe text unchanged", () => {
    expect(escapeHtml("Codex / Claude Usage Dashboard")).toBe("Codex / Claude Usage Dashboard");
  });

  it("handles empty input", () => {
    expect(escapeHtml("")).toBe("");
  });
});

describe("makeNonce", () => {
  it("is base64url (alphanumeric plus - and _, no padding)", () => {
    expect(makeNonce()).toMatch(/^[A-Za-z0-9_-]+$/);
  });

  it("is exactly 32 chars (24 random bytes → 32 base64url chars)", () => {
    expect(makeNonce()).toHaveLength(32);
  });

  it("yields different values on consecutive calls", () => {
    expect(makeNonce()).not.toBe(makeNonce());
  });

  it("draws its bytes from node:crypto, not from Math.random", async () => {
    // Nothing about the string itself can catch a predictable nonce: 32 chars
    // drawn uniformly from the base64url alphabet by Math.random() satisfies the
    // character class, the length and the "consecutive calls differ" checks
    // above. Only the source discriminates, so the source is what is asserted.
    //
    // Scoped with doMock + a fresh module registry rather than a file-level
    // vi.mock, which would hand every other test in this file a constant nonce
    // and silently defeat the three checks above.
    vi.resetModules();
    const randomBytes = vi.fn(() => Buffer.from("k".repeat(24), "utf8"));
    vi.doMock("node:crypto", () => ({ randomBytes }));
    try {
      const mocked = await import("../src/sidebar");
      expect(mocked.makeNonce()).toBe(Buffer.from("k".repeat(24), "utf8").toString("base64url"));
      expect(randomBytes).toHaveBeenCalledWith(24);
    } finally {
      vi.doUnmock("node:crypto");
      vi.resetModules();
    }
  });
});

// The CSP is the webview's outermost gate — what it may frame and what it may
// execute — so it is asserted by exact equality, not by `toContain` of
// fragments. Substring assertions catch a directive being *removed* and never
// one being *added*: appending `https: http:` to frame-src, swapping
// `default-src 'none'` for `default-src *`, or adding `'unsafe-inline'` beside
// the script-src nonce each left the suite at 105/105. All three shapes the
// renderer can emit are pinned, because the status pane's img-src is
// conditional and a single golden would have to be loose enough to re-open the
// gap it closes.
describe("renderHtml Content-Security-Policy (exact, not by fragment)", () => {
  const NONCE = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345";

  it("iframe pane: frames loopback only, and nothing else is granted", () => {
    expect(cspOf(renderHtml("http://127.0.0.1:9000/", "", NONCE))).toBe(
      "default-src 'none'; frame-src http://127.0.0.1:* http://localhost:*;"
      + ` style-src 'unsafe-inline'; script-src 'nonce-${NONCE}';`,
    );
  });

  it("status pane without an icon: no frame-src and no img-src at all", () => {
    expect(cspOf(renderHtml(null, "", NONCE))).toBe(
      `default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${NONCE}';`,
    );
  });

  it("status pane with an icon: img-src is exactly the webview's own cspSource", () => {
    const html = renderHtml(null, "", NONCE, "https://host/icon.svg", "vscode-webview://abc");
    expect(cspOf(html)).toBe(
      "default-src 'none'; img-src vscode-webview://abc;"
      + ` style-src 'unsafe-inline'; script-src 'nonce-${NONCE}';`,
    );
  });
});

describe("renderHtml with iframe URL", () => {
  const NONCE = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345";

  it("embeds the iframe pointing at the given URL", () => {
    const token = "a".repeat(43);
    const html = renderHtml(`http://127.0.0.1:54321/#token=${token}`, "", NONCE);
    expect(html).toContain(`src="http://127.0.0.1:54321/#token=${token}"`);
    expect(html).toContain("<iframe");
  });

  it("escapes URL into the iframe src so attribute syntax can't break", () => {
    const html = renderHtml(`http://127.0.0.1:9000/?q="><script>x</script>`, "", NONCE);
    expect(html).not.toContain("<script>x</script>");
    expect(html).toContain("&quot;");
    expect(html).toContain("&lt;script&gt;");
  });

  it("includes a CSP frame-src that allows localhost", () => {
    const html = renderHtml("http://127.0.0.1:9000/", "", NONCE);
    expect(html).toContain("frame-src http://127.0.0.1:* http://localhost:*");
  });

  it("includes the script-src nonce", () => {
    const html = renderHtml("http://127.0.0.1:9000/", "", NONCE);
    expect(html).toContain(`script-src 'nonce-${NONCE}'`);
  });

  it("sandbox grants only what the dashboard needs (incl. downloads for CSV export)", () => {
    const html = renderHtml("http://127.0.0.1:9000/", "", NONCE);
    expect(html).toContain("sandbox=\"allow-scripts allow-same-origin allow-downloads\"");
    // allow-downloads lets the dashboard's CSV export (a Blob + a.download click)
    // work inside the webview. Specifically NOT allow-popups — it doesn't open windows.
    expect(html).not.toContain("allow-popups");
  });

  it("frame-src does NOT include third-party CDN (iframe has its own CSP)", () => {
    const html = renderHtml("http://127.0.0.1:9000/", "", NONCE);
    expect(html).not.toContain("cdn.jsdelivr.net");
  });
});

describe("renderHtml with null URL (status pane)", () => {
  const NONCE = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345";

  it("renders the placeholder when no URL is set", () => {
    const html = renderHtml(null, "", NONCE);
    expect(html).toContain("Codex / Claude Usage");
    expect(html).toContain("not running yet");
    expect(html).not.toContain("<iframe");
  });

  it("renders a custom status message when provided", () => {
    const html = renderHtml(null, "Server failed to bind to port 8080", NONCE);
    expect(html).toContain("Server failed to bind to port 8080");
  });

  it("escapes status text", () => {
    const html = renderHtml(null, "<img onerror=x>", NONCE);
    expect(html).not.toContain("<img onerror=x>");
    expect(html).toContain("&lt;img onerror=x&gt;");
  });

  it("does NOT include the frame-src CSP (no iframe to allow)", () => {
    const html = renderHtml(null, "", NONCE);
    expect(html).not.toContain("frame-src");
  });

  it("offers a Retry button (invoking the open command) only when showRetry is set", () => {
    const html = renderHtml(null, "Failed to start dashboard: timed out", NONCE, "", "", true);
    expect(html).toContain('href="command:claudeUsage.open"');
    expect(html).toContain("Retry");
  });

  it("does NOT show the Retry button during normal startup (showRetry defaults false)", () => {
    const html = renderHtml(null, "Starting dashboard at http://127.0.0.1:8080/…", NONCE);
    expect(html).not.toContain("command:claudeUsage.open");
    expect(html).not.toContain("Retry");
  });

  it("renders the logo and an img-src CSP when an icon URI is provided", () => {
    const html = renderHtml(null, "", NONCE, "https://host/icon.svg", "vscode-webview://abc");
    expect(html).toContain('class="logo"');
    expect(html).toContain("img-src vscode-webview://abc");
    expect(html).toContain('mask: url("https://host/icon.svg")');
  });

  it("omits the logo and img-src when no icon URI is provided", () => {
    const html = renderHtml(null, "", NONCE);
    expect(html).not.toContain('class="logo"');
    expect(html).not.toContain("img-src");
  });
});

describe("DashboardSidebar onShow auto-start", () => {
  it("invokes the onShow callback when resolveWebviewView is called", () => {
    const onShow = vi.fn();
    const sidebar = new DashboardSidebar(onShow);
    const fakeView = makeFakeView() as any;
    sidebar.resolveWebviewView(fakeView);
    expect(onShow).toHaveBeenCalledTimes(1);
  });

  it("doesn't throw without a callback (default no-op)", () => {
    const sidebar = new DashboardSidebar();
    const fakeView = makeFakeView() as any;
    expect(() => sidebar.resolveWebviewView(fakeView)).not.toThrow();
  });

  it("renders HTML into the webview on resolve", () => {
    const sidebar = new DashboardSidebar();
    const fakeView = makeFakeView() as any;
    sidebar.resolveWebviewView(fakeView);
    expect(fakeView._html()).toContain("<html");
  });

  it("allows only the fixed retry command URI", () => {
    const sidebar = new DashboardSidebar();
    const fakeView = makeFakeView() as any;
    sidebar.resolveWebviewView(fakeView);
    expect(fakeView.webview.options.enableCommandUris)
      .toEqual(["claudeUsage.open"]);
  });

  it("grants no local resource roots at all when constructed without an extensionUri", () => {
    // Fallback branch only — production always passes context.extensionUri.
    // The real posture is asserted in the block below.
    const sidebar = new DashboardSidebar();
    const fakeView = makeFakeView() as any;
    sidebar.resolveWebviewView(fakeView);
    expect(fakeView.webview.options.localResourceRoots).toEqual([]);
  });

  it("re-fires onShow on every resolveWebviewView (e.g. user collapses+reopens)", () => {
    const onShow = vi.fn();
    const sidebar = new DashboardSidebar(onShow);
    const fakeView1 = makeFakeView() as any;
    sidebar.resolveWebviewView(fakeView1);
    fakeView1._triggerDispose();
    const fakeView2 = makeFakeView() as any;
    sidebar.resolveWebviewView(fakeView2);
    expect(onShow).toHaveBeenCalledTimes(2);
  });

  it("keeps the replacement view when the old view is disposed later", () => {
    const sidebar = new DashboardSidebar();
    const oldView = makeFakeView() as any;
    const currentView = makeFakeView() as any;
    sidebar.resolveWebviewView(oldView);
    sidebar.resolveWebviewView(currentView);
    oldView._triggerDispose();
    sidebar.setStatus("Starting the replacement dashboard");
    expect(currentView._html()).toContain("Starting the replacement dashboard");
    sidebar.setUrl("http://127.0.0.1:9000/#token=current");
    expect(currentView._html()).toContain('src="http://127.0.0.1:9000/#token=current"');
  });
});

// extension.ts always constructs the sidebar with context.extensionUri, so this
// is the branch that actually ships. Testing only the no-extensionUri fallback
// left the webview's real filesystem grant unasserted: widening it to the whole
// disk, to every workspace folder, or to the extension root (which carries the
// bundled python/ tree) kept the suite green.
describe("DashboardSidebar webview resource grants (production extensionUri path)", () => {
  function resolveWithExtensionUri() {
    const sidebar = new DashboardSidebar(() => {}, fakeExtensionUri());
    const view = makeFakeViewWithWebviewUri();
    sidebar.resolveWebviewView(view);
    return view;
  }

  it("grants exactly one local resource root: the bundled resources directory", () => {
    const roots = resolveWithExtensionUri().webview.options.localResourceRoots;
    expect(roots.map((r: any) => r.fsPath)).toEqual([`${EXTENSION_DIR}/resources`]);
  });

  it("does not grant the extension root (which contains the bundled python sources)", () => {
    const roots = resolveWithExtensionUri().webview.options.localResourceRoots;
    expect(roots.map((r: any) => r.fsPath)).not.toContain(EXTENSION_DIR);
    for (const root of roots) {
      expect(root.fsPath.startsWith(`${EXTENSION_DIR}/resources`)).toBe(true);
    }
  });

  it("does not grant any workspace folder to the webview", () => {
    const roots = resolveWithExtensionUri().webview.options.localResourceRoots;
    expect(roots.map((r: any) => r.fsPath)).not.toContain("/Users/dev/some-cloned-repo");
  });

  it("does not grant a filesystem root", () => {
    const roots = resolveWithExtensionUri().webview.options.localResourceRoots;
    expect(roots.map((r: any) => r.fsPath)).not.toContain("/");
  });

  it("still allows only the fixed retry command URI on the production path", () => {
    const view = resolveWithExtensionUri();
    expect(view.webview.options.enableCommandUris).toEqual(["claudeUsage.open"]);
  });

  it("resolves the bundled icon through asWebviewUri and renders it", () => {
    const view = resolveWithExtensionUri();
    expect(view._html()).toContain(
      `mask: url("https://deadbeef.vscode-cdn.net${EXTENSION_DIR}/resources/icon.svg")`,
    );
    expect(view._html()).toContain('class="logo"');
  });

  it("captures the webview cspSource into the status pane's img-src", () => {
    // The status pane's CSP is built from this value; losing it silently drops
    // img-src and the logo stops rendering.
    expect(resolveWithExtensionUri()._html()).toContain("img-src vscode-webview://deadbeef");
  });

  it("never resolves the icon from outside the granted resources root", () => {
    const html = resolveWithExtensionUri()._html();
    const match = html.match(/mask: url\("([^"]+)"\)/);
    expect(match).not.toBeNull();
    expect(match![1]).toContain(`${EXTENSION_DIR}/resources/`);
  });
});

function attachedSidebar() {
  const sidebar = new DashboardSidebar(() => {}, fakeExtensionUri());
  const view = makeFakeViewWithWebviewUri();
  sidebar.resolveWebviewView(view);
  return { sidebar, view };
}

// The Retry button is gated on the private `failed` flag, but the two Retry
// tests above call renderHtml(..., showRetry) directly, so they assert the
// renderer and not the wiring: flipping setError's `this.failed = true` to
// false — or setStatus's `false` to true — kept the suite at 105/105. Both
// directions matter. #147 was the button being absent after a failed start; the
// fix's own comment ("Only true after a start attempt actually failed") is the
// other half, and the no-Python / no-cli.py paths in extension.ts deliberately
// call setStatus so that no Retry is offered where retrying cannot help.
describe("DashboardSidebar failure state machine", () => {
  it("replaces an old iframe with the next startup status", () => {
    const { sidebar, view } = attachedSidebar();
    sidebar.setUrl("http://127.0.0.1:9000/#token=old");
    sidebar.setStatus("Starting replacement dashboard");
    expect(view._html()).toContain("Starting replacement dashboard");
    expect(view._html()).not.toContain("<iframe");
    expect(view._html()).not.toContain("#token=old");
    expect(view._html()).not.toContain("command:claudeUsage.open");
  });

  it("shows an error and Retry after a previously running dashboard fails", () => {
    const { sidebar, view } = attachedSidebar();
    sidebar.setUrl("http://127.0.0.1:9000/#token=old");
    sidebar.setError("Failed to start replacement dashboard");
    expect(view._html()).toContain("Failed to start replacement dashboard");
    expect(view._html()).toContain('href="command:claudeUsage.open"');
    expect(view._html()).not.toContain("<iframe");
    expect(view._html()).not.toContain("#token=old");
  });

  it("offers no Retry while the server is merely starting", () => {
    const { sidebar, view } = attachedSidebar();
    sidebar.setStatus("Starting dashboard at http://127.0.0.1:8080/…");
    expect(view._html()).toContain("Starting dashboard at http://127.0.0.1:8080/…");
    expect(view._html()).not.toContain("command:claudeUsage.open");
  });

  it("offers Retry once a start attempt has actually failed", () => {
    const { sidebar, view } = attachedSidebar();
    sidebar.setStatus("Starting dashboard…");
    sidebar.setError("Failed to start dashboard: timed out");
    expect(view._html()).toContain("Failed to start dashboard: timed out");
    expect(view._html()).toContain('href="command:claudeUsage.open"');
  });

  it("withdraws Retry when a later attempt goes back to starting", () => {
    const { sidebar, view } = attachedSidebar();
    sidebar.setError("Failed to start dashboard: timed out");
    sidebar.setStatus("Starting dashboard at http://127.0.0.1:8080/…");
    expect(view._html()).not.toContain("command:claudeUsage.open");
    expect(view._html()).not.toContain("Retry");
  });

  it("replaces the error pane with the iframe once the URL arrives", () => {
    const { sidebar, view } = attachedSidebar();
    sidebar.setError("Failed to start dashboard: timed out");
    sidebar.setUrl("http://127.0.0.1:9000/#token=abc");
    expect(view._html()).toContain('<iframe src="http://127.0.0.1:9000/#token=abc"');
    expect(view._html()).not.toContain("command:claudeUsage.open");
  });
});

// refresh() is the final UI step of the claudeUsage.rescan command and of the
// already-ready branch of openDashboard, and had no test at all: inverting its
// guard to `if (this.view) return` made every refresh a silent no-op with the
// suite green. Asserting only "the HTML string changed" would not be enough
// either — that also passes with no URL set — so the URL is in place first and
// the write itself is counted.
describe("DashboardSidebar.refresh", () => {
  it("re-emits the same iframe as a fresh document", () => {
    const { sidebar, view } = attachedSidebar();
    sidebar.setUrl("http://127.0.0.1:9000/#token=abc");
    const before = view._html();
    const writesBefore = view._htmlWrites();
    sidebar.refresh();
    expect(view._htmlWrites()).toBe(writesBefore + 1);
    // A new document, because the per-render nonce differs — that regeneration
    // is what makes the host reload the frame — but the same URL inside it.
    expect(view._html()).not.toBe(before);
    expect(view._html()).toContain('<iframe src="http://127.0.0.1:9000/#token=abc"');
  });

  it("is a no-op rather than a throw when no view is attached", () => {
    const sidebar = new DashboardSidebar();
    expect(() => sidebar.refresh()).not.toThrow();
  });
});
