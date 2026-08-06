import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";

const patchScript = path.resolve(import.meta.dirname, "patch_browser_proxy_navigation.mjs");
const fixtureRoot = fs.mkdtempSync(path.join(os.tmpdir(), "openclaw-browser-proxy-patch-"));
try {
  fs.mkdirSync(path.join(fixtureRoot, "dist"), { recursive: true });
  fs.writeFileSync(
    path.join(fixtureRoot, "package.json"),
    JSON.stringify({ version: "2026.7.1-2" }),
  );
  fs.writeFileSync(
    path.join(fixtureRoot, "dist", "chrome-fixture.js"),
    [
      "async function assertBrowserNavigationAllowed(opts) {",
      "\tawait resolvePinnedHostnameWithPolicy(parsed.hostname, {",
      "\t\tlookupFn: opts.lookupFn,",
      "\t\tpolicy: opts.ssrfPolicy",
      "\t});",
      "}",
    ].join("\n"),
  );

  const run = (mode) => spawnSync(process.execPath, [patchScript, mode], {
    env: { ...process.env, OPENCLAW_PACKAGE_ROOT: fixtureRoot },
    encoding: "utf8",
  });
  const patched = run("--patch");
  assert.equal(patched.status, 0, patched.stderr || patched.stdout);
  const verified = run("--verify");
  assert.equal(verified.status, 0, verified.stderr || verified.stdout);

  const source = fs.readFileSync(path.join(fixtureRoot, "dist", "chrome-fixture.js"), "utf8");
  assert.match(source, /CLAWMANAGER_MANAGED_PREVIEW_PROXY_DNS/);
  assert.match(source, /explicit-browser-proxy/);
  assert.match(source, /isPrivateNetworkAllowedByPolicy/);
  assert.match(source, /isExplicitlyAllowedBrowserHostname/);
  assert.equal((source.match(/CLAWMANAGER_MANAGED_PREVIEW_PROXY_DNS/g) || []).length, 1);
} finally {
  fs.rmSync(fixtureRoot, { recursive: true, force: true });
}

process.stdout.write("OpenClaw Browser proxy navigation patch test passed\n");
