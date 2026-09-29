import { describe, expect, it } from "vitest";
import {
  InspectableConfiguration,
  trustedUserSetting,
} from "../src/trusted-config";

/** One inspection result, in the shape the interface itself declares. */
type Inspection = ReturnType<InspectableConfiguration["inspect"]>;

/**
 * A configuration double returning one fixed inspection result.
 *
 * `inspect` is generic in the *method*, so no double holding concrete values
 * can satisfy it without a cast — that is a property of the API being faked,
 * not of these tests. Confining the cast here is what keeps each call site
 * checked against the real shape: a misspelled `globalvalue` is a compile
 * error under `npm run typecheck:test`, the gate these files used to be
 * invisible to.
 */
function fakeConfig(inspected: Inspection): InspectableConfiguration {
  return { inspect: () => inspected as never };
}

describe("trustedUserSetting", () => {
  it("uses a user-global value", () => {
    const config = fakeConfig({
      defaultValue: "",
      globalValue: "/trusted/python",
    });
    expect(trustedUserSetting(config, "pythonPath", "fallback"))
      .toBe("/trusted/python");
  });

  it("ignores executable paths supplied by workspace settings", () => {
    const config = fakeConfig({
      defaultValue: "",
      workspaceValue: "./malicious-python",
      workspaceFolderValue: "./folder-malicious-python",
    });
    expect(trustedUserSetting(config, "pythonPath", "fallback")).toBe("");
  });

  it("falls back when the setting is not registered", () => {
    const config = fakeConfig(undefined);
    expect(trustedUserSetting(config, "port", 0)).toBe(0);
  });
});
