import fs from "node:fs";
import path from "node:path";

const expectedVersion = "2026.7.1-2";
const packageRoot = process.env.OPENCLAW_PACKAGE_ROOT || "/usr/local/lib/node_modules/openclaw";
const packageJson = JSON.parse(fs.readFileSync(path.join(packageRoot, "package.json"), "utf8"));
if (packageJson.version !== expectedVersion) {
  throw new Error(`refusing to patch OpenClaw ${packageJson.version}; expected ${expectedVersion}`);
}

const distDir = path.join(packageRoot, "dist");
const marker = "CLAWMANAGER_MANAGED_PREVIEW_PROXY_DNS";
const candidates = fs.readdirSync(distDir)
  .filter((name) => /^chrome-.*\.js$/.test(name))
  .map((name) => path.join(distDir, name))
  .filter((file) => fs.readFileSync(file, "utf8").includes("async function assertBrowserNavigationAllowed(opts)"));

if (candidates.length !== 1) {
  throw new Error(`expected one OpenClaw browser navigation module, found ${candidates.length}`);
}

const target = candidates[0];
let source = fs.readFileSync(target, "utf8");
const insertionPoint = "\tawait resolvePinnedHostnameWithPolicy(parsed.hostname, {";
const patchBody = [
  `\t// ${marker}: Chromium is already forced through the operator-managed`,
  "\t// forward proxy. Delegate DNS only for an explicitly allowlisted host;",
  "\t// direct profiles and arbitrary destinations retain upstream DNS pinning.",
  "\tif (opts.browserProxyMode === \"explicit-browser-proxy\" &&",
  "\t\tisPrivateNetworkAllowedByPolicy(opts.ssrfPolicy) &&",
  "\t\tisExplicitlyAllowedBrowserHostname(parsed.hostname, opts.ssrfPolicy)) return;",
  insertionPoint,
].join("\n");

if (process.argv.includes("--patch") && !source.includes(marker)) {
  const occurrences = source.split(insertionPoint).length - 1;
  if (occurrences !== 1) {
    throw new Error(`expected one navigation DNS call, found ${occurrences}`);
  }
  source = source.replace(insertionPoint, patchBody);
  fs.writeFileSync(target, source);
}

source = fs.readFileSync(target, "utf8");
for (const required of [
  marker,
  'opts.browserProxyMode === "explicit-browser-proxy"',
  "isPrivateNetworkAllowedByPolicy(opts.ssrfPolicy)",
  "isExplicitlyAllowedBrowserHostname(parsed.hostname, opts.ssrfPolicy)",
  insertionPoint,
]) {
  if (!source.includes(required)) {
    throw new Error(`OpenClaw managed Preview Browser patch is incomplete: ${required}`);
  }
}

process.stdout.write(`OpenClaw managed Preview Browser patch verified in ${path.basename(target)}\n`);
