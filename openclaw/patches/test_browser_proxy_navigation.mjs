import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { pathToFileURL } from "node:url";

const patchScript = path.resolve(import.meta.dirname, "patch_browser_proxy_navigation.mjs");
const fixtureRoot = fs.mkdtempSync(path.join(os.tmpdir(), "openclaw-browser-proxy-patch-"));
try {
  fs.mkdirSync(path.join(fixtureRoot, "dist"), { recursive: true });
  fs.writeFileSync(
    path.join(fixtureRoot, "package.json"),
    JSON.stringify({ version: "2026.7.1-2", type: "module" }),
  );
  fs.writeFileSync(
    path.join(fixtureRoot, "dist", "chrome-fixture.js"),
    [
      "const lookups = [];",
      "function normalizeHostname(value) { return String(value || '').trim().toLowerCase().replace(/\\.$/, ''); }",
      "function isPrivateNetworkAllowedByPolicy(policy) { return policy?.dangerouslyAllowPrivateNetwork === true || policy?.allowPrivateNetwork === true; }",
      "async function resolvePinnedHostnameWithPolicy(hostname) { lookups.push(normalizeHostname(hostname)); }",
      "async function assertBrowserNavigationAllowed(opts) {",
      "\tconst parsed = new URL(opts.url);",
      "\tawait resolvePinnedHostnameWithPolicy(parsed.hostname, {",
      "\t\tlookupFn: opts.lookupFn,",
      "\t\tpolicy: opts.ssrfPolicy",
      "\t});",
      "}",
      "export async function check(url, browserProxyMode = 'explicit-browser-proxy', allowPrivate = true) {",
      "\tconst before = lookups.length;",
      "\tawait assertBrowserNavigationAllowed({ url, browserProxyMode, ssrfPolicy: { dangerouslyAllowPrivateNetwork: allowPrivate } });",
      "\treturn lookups.length - before;",
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
  assert.match(source, /p-\[a-z0-9_-\]\{16\}/);
  assert.equal((source.match(/CLAWMANAGER_MANAGED_PREVIEW_PROXY_DNS/g) || []).length, 1);

  const fixture = await import(pathToFileURL(path.join(fixtureRoot, "dist", "chrome-fixture.js")).href);
  assert.equal(await fixture.check("https://example.com/"), 1, "ordinary public hosts must retain upstream DNS pinning");
  assert.equal(
    await fixture.check("http://clawmanager-egress-proxy.clawmanager-system.svc.cluster.local:3128/v2/interactive/x"),
    1,
    "the resolvable Preview bootstrap must retain upstream DNS handling",
  );
  assert.equal(
    await fixture.check("http://p-abcdefghijklmnop.clawmanager-team-preview.invalid/v2/interactive/x"),
    0,
    "the exact isolated Preview host must delegate DNS to the managed proxy",
  );
  assert.equal(
    await fixture.check("http://p-abcdefghijklmnop.clawmanager-team-preview.invalid/v2/interactive/x", "direct"),
    1,
    "direct Browser profiles must not receive the managed Preview exception",
  );
  assert.equal(
    await fixture.check("http://p-abcdefghijklmnop.clawmanager-team-preview.invalid/v2/interactive/x", "explicit-browser-proxy", false),
    1,
    "strict private-network policy must not receive the managed Preview exception",
  );
  for (const hostname of [
    "clawmanager-team-preview.invalid",
    "p-short.clawmanager-team-preview.invalid",
    "p-abcdefghijklmnop.evil.clawmanager-team-preview.invalid",
    "xp-abcdefghijklmnop.clawmanager-team-preview.invalid",
    "p-abcdefghijklmnop.clawmanager-team-preview.invalid.evil.example",
  ]) {
    assert.equal(
      await fixture.check(`http://${hostname}/v2/interactive/x`),
      1,
      `lookalike host must retain upstream validation: ${hostname}`,
    );
  }
} finally {
  // Windows can briefly retain a handle after the dynamically imported fixture
  // is evaluated. Let rmSync retry the transient ENOTEMPTY/EPERM window so a
  // successful behavior test is not reported as a product failure.
  fs.rmSync(fixtureRoot, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
}

process.stdout.write("OpenClaw Browser proxy navigation patch test passed\n");
