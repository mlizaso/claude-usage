// Recreate generated TypeScript output before packaging so stale JavaScript
// cannot be included in the VSIX. The fixed path is deliberately rejected if
// it is a symlink or non-directory rather than following it.

const fs = require("node:fs");
const path = require("node:path");

const extensionRoot = path.resolve(__dirname, "..");
const outDir = path.join(extensionRoot, "out");

if (fs.existsSync(outDir)) {
  const info = fs.lstatSync(outDir);
  if (info.isSymbolicLink() || !info.isDirectory()) {
    console.error(`clean-build: ERROR - refusing unsafe output path ${outDir}`);
    process.exit(1);
  }
  fs.rmSync(outDir, { recursive: true, force: true });
}

console.log("clean-build: removed generated out/");
