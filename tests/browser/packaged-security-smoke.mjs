#!/usr/bin/env node

import { spawn } from "node:child_process";
import { chmod, cp, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const playwrightPath = process.env.PLAYWRIGHT_CORE_PATH || "playwright-core";
const { chromium } = require(playwrightPath);

const ownRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const root = resolve(process.env.CLIPPER_ROOT || ownRoot);
const chromiumExecutable = process.env.CHROMIUM_BIN || "/usr/bin/chromium";
const nativeBinary = process.env.CLIPPER_NATIVE_BIN;
const readerBinary = process.env.CLIPPER_READER_BIN;
const helperBinary = process.env.CLIPPER_HELPER_BIN;
const scratch = await mkdtemp(join(tmpdir(), "clipper-packaged-security-"));
const extension = join(scratch, "extension");
const profile = join(scratch, "profile");
const configHome = join(scratch, "config");
const home = join(scratch, "home");
const nativeHost = join(scratch, "native-security-host.mjs");
const nativeLog = join(scratch, "native-requests.jsonl");
const imagePixel = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
  "base64",
);
const requests = [];
let cancelledStalledBodies = 0;
const stalledResponses = new Set();
let context;
let server;
let cleanupPromise;

const browserEnv = {
  PATH: process.env.PATH || "/usr/bin:/bin",
  HOME: home,
  XDG_CONFIG_HOME: configHome,
  XDG_CACHE_HOME: join(scratch, "cache"),
  XDG_STATE_HOME: join(scratch, "state"),
  XDG_DATA_HOME: join(scratch, "data"),
  CLIPPER_SECURITY_NATIVE_LOG: nativeLog,
  LANG: "C.UTF-8",
};

function cleanup() {
  cleanupPromise ??= (async () => {
    for (const response of stalledResponses) response.destroy();
    if (context) await context.close().catch(() => undefined);
    if (server?.listening) {
      server.closeAllConnections?.();
      await new Promise((resolveClose) => server.close(resolveClose));
    }
    await rm(scratch, { recursive: true, force: true });
  })();
  return cleanupPromise;
}

const wallDeadline = setTimeout(() => {
  process.stderr.write("Packaged security smoke exceeded its 45-second wall deadline; terminating Chromium.\n");
  const forcedExit = setTimeout(() => process.exit(1), 5_000);
  forcedExit.unref();
  void cleanup().finally(() => process.exit(1));
}, 45_000);

async function launch() {
  return chromium.launchPersistentContext(profile, {
    executablePath: chromiumExecutable,
    headless: true,
    env: browserEnv,
    args: [
      `--disable-extensions-except=${extension}`,
      `--load-extension=${extension}`,
      "--no-first-run",
      "--no-default-browser-check",
      "--no-proxy-server",
      "--host-resolver-rules=MAP clipper-security.test 127.0.0.1",
    ],
  });
}

async function worker(activeContext) {
  return activeContext.serviceWorkers()[0] || activeContext.waitForEvent("serviceworker", { timeout: 10_000 });
}

async function captureFromPackagedPopup(activeContext, activeWorker, page) {
  const popup = await activeContext.newPage();
  const workerUrl = new URL(activeWorker.url());
  await popup.goto(`${workerUrl.protocol}//${workerUrl.hostname}/popup.html`, { waitUntil: "domcontentloaded" });
  await page.bringToFront();
  const response = await popup.evaluate(async () => chrome.runtime.sendMessage({ type: "capture.start" }));
  await popup.close();
  if (response?.ok !== true || !response.result) throw new Error(response?.error || "Packaged capture failed.");
  return response.result;
}

async function installFixtureDeadlineSeam() {
  const backgroundPath = join(extension, "dist", "background.js");
  let background = await readFile(backgroundPath, "utf8");
  const replacements = [
    {
      pattern: /([A-Za-z_$][\w$]*)\.resourceTimeoutMs \?\? RESOURCE_TIMEOUT_MS/,
      replacement: "globalThis.__clipperPackagedFixtureTimeouts?.resourceTimeoutMs ?? $1.resourceTimeoutMs ?? RESOURCE_TIMEOUT_MS",
      label: "resource deadline",
    },
    {
      pattern: /([A-Za-z_$][\w$]*)\.totalTimeoutMs \?\? TOTAL_RETRIEVAL_TIMEOUT_MS/,
      replacement: "globalThis.__clipperPackagedFixtureTimeouts?.totalTimeoutMs ?? $1.totalTimeoutMs ?? TOTAL_RETRIEVAL_TIMEOUT_MS",
      label: "total deadline",
    },
  ];
  for (const { pattern, replacement, label } of replacements) {
    const matches = [...background.matchAll(new RegExp(pattern.source, "g"))];
    if (matches.length !== 1) {
      throw new Error(`Packaged fixture expected one ${label} seam, found ${matches.length}.`);
    }
    background = background.replace(pattern, replacement);
  }
  await writeFile(backgroundPath, background);
}

async function setCaptureTimeouts(activeWorker, resourceTimeoutMs, totalTimeoutMs) {
  await activeWorker.evaluate(({ resource, total }) => {
    globalThis.__clipperPackagedFixtureTimeouts = { resourceTimeoutMs: resource, totalTimeoutMs: total };
  }, { resource: resourceTimeoutMs, total: totalTimeoutMs });
}

function shellQuote(value) {
  return `'${value.replaceAll("'", `'\\''`)}'`;
}

try {
  await mkdir(home, { recursive: true });
  await cp(join(root, "extension"), extension, { recursive: true });
  await installFixtureDeadlineSeam();
  const manifestPath = join(extension, "manifest.json");
  const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
  manifest.host_permissions = ["http://clipper-security.test/*"];
  await writeFile(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);

  context = await launch();
  let activeWorker = await worker(context);
  const extensionId = new URL(activeWorker.url()).hostname;
  await context.close();
  context = undefined;

  if (nativeBinary) {
    const nativeArguments = [
      nativeBinary,
      "--data-dir", join(scratch, "collection"),
      ...(readerBinary ? ["--reader-bin", readerBinary] : []),
      ...(helperBinary ? ["--helper-bin", helperBinary] : []),
      "--native",
      "--allowed-origin", `chrome-extension://${extensionId}/`,
    ];
    await writeFile(nativeHost, `#!/bin/sh\nexec ${nativeArguments.map(shellQuote).join(" ")} "$@"\n`);
  } else {
    await writeFile(nativeHost, `#!/usr/bin/env node
import { appendFileSync } from "node:fs";
let buffered = Buffer.alloc(0);
function reply(value) {
  const body = Buffer.from(JSON.stringify(value));
  const header = Buffer.alloc(4);
  header.writeUInt32LE(body.length, 0);
  process.stdout.write(Buffer.concat([header, body]));
}
process.stdin.on("data", (chunk) => {
  buffered = Buffer.concat([buffered, chunk]);
  if (buffered.length < 4) return;
  const length = buffered.readUInt32LE(0);
  if (buffered.length < length + 4) return;
  const request = JSON.parse(buffered.subarray(4, length + 4).toString("utf8"));
  appendFileSync(process.env.CLIPPER_SECURITY_NATIVE_LOG, JSON.stringify(request) + "\\n", { mode: 0o600 });
  let result;
  if (request.op === "capture.check") result = { existing: null };
  else if (request.op === "capture.begin") result = { captureId: "capture-security", clipId: "clip-security" };
  else if (request.op === "asset.import") result = { id: "asset-security" };
  else if (request.op === "capture.finish") result = {
    id: "clip-security",
    status: request.missing?.length ? "incomplete" : "ready",
    missing: request.missing || [],
  };
  else result = {};
  reply({ ok: true, result });
});
`);
  }
  await chmod(nativeHost, 0o700);

  const origin = `chrome-extension://${extensionId}/`;
  const hostManifest = {
    name: "com.omarchy.clipper",
    description: "Synthetic packaged-extension security host",
    path: nativeHost,
    type: "stdio",
    allowed_origins: [origin],
  };
  for (const directory of [
    join(configHome, "chromium", "NativeMessagingHosts"),
    join(profile, "NativeMessagingHosts"),
  ]) {
    await mkdir(directory, { recursive: true });
    await writeFile(join(directory, "com.omarchy.clipper.json"), `${JSON.stringify(hostManifest, null, 2)}\n`);
  }

  server = createServer((request, response) => {
    const url = new URL(request.url || "/", "http://127.0.0.1");
    if (url.pathname.startsWith("/image-") && url.pathname.endsWith(".png")) {
      requests.push({ path: url.pathname, cookie: request.headers.cookie, referer: request.headers.referer });
      if (url.pathname === "/image-stall.png" || url.pathname === "/image-total-stall.png") {
        stalledResponses.add(response);
        response.on("close", () => {
          stalledResponses.delete(response);
          cancelledStalledBodies += 1;
        });
        response.writeHead(200, { "content-type": "image/png", "cache-control": "no-store" });
        response.write(imagePixel.subarray(0, 8));
        return;
      }
      if (url.pathname === "/image-slow.png") {
        response.writeHead(200, { "content-type": "image/png", "cache-control": "no-store" });
        response.write(imagePixel.subarray(0, 8));
        setTimeout(() => response.end(imagePixel.subarray(8)), 60);
        return;
      }
      if (url.pathname === "/image-redirect.png") {
        response.writeHead(302, { location: "/image-redirect-target.png", "cache-control": "no-store" });
        response.end();
        return;
      }
      response.writeHead(200, {
        "content-type": "image/png",
        "content-length": String(imagePixel.length),
        "cache-control": "no-store",
      });
      response.end(imagePixel);
      return;
    }
    if (url.pathname === "/article") {
      const address = server.address();
      if (!address || typeof address === "string") throw new Error("Fixture server has no TCP address.");
      response.writeHead(200, {
        "content-type": "text/html; charset=utf-8",
        "cache-control": "no-store",
        "set-cookie": "syntheticSession=secret; Path=/; SameSite=Lax",
      });
      response.end(`<!doctype html><title>Packaged capture security</title><main><article>
        <h1>Packaged capture security</h1>
        <p>This meaningful synthetic article verifies controlled image retrieval in the packaged extension.</p>
        <figure><img data-src="/image-fast.png" alt="Allowed"><figcaption>Allowed image</figcaption></figure>
        <figure><img data-src="/image-redirect.png" alt="Redirect"><figcaption>Redirected image</figcaption></figure>
        <figure><img data-src="http://127.0.0.1:${address.port}/image-private.png" alt="Private"><figcaption>Private image</figcaption></figure>
      </article></main>`);
      return;
    }
    if (url.pathname === "/resource-deadline") {
      response.writeHead(200, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" });
      response.end(`<!doctype html><title>Resource deadline</title><main><article>
        <h1>Resource deadline</h1><p>This meaningful article verifies that a stalled response body is cancelled.</p>
        <img data-src="/image-stall.png" alt="Stalled body">
      </article></main>`);
      return;
    }
    if (url.pathname === "/total-deadline") {
      response.writeHead(200, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" });
      response.end(`<!doctype html><title>Total deadline</title><main><article>
        <h1>Total deadline</h1><p>This meaningful article verifies that the total retrieval budget stops later work.</p>
        <img data-src="/image-slow.png" alt="Slow image">
        <img data-src="/image-total-stall.png" alt="Total stall">
        <img data-src="/image-late.png" alt="Must not start">
      </article></main>`);
      return;
    }
    response.writeHead(404);
    response.end();
  });
  await new Promise((resolveListen, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolveListen);
  });
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("Fixture server did not bind.");

  context = await launch();
  activeWorker = await worker(context);
  const page = context.pages()[0] || await context.newPage();
  const sourceUrl = `http://clipper-security.test:${address.port}/article`;
  await page.goto(sourceUrl, { waitUntil: "domcontentloaded", timeout: 15_000 });
  const state = await captureFromPackagedPopup(context, activeWorker, page);
  const nativeRequests = nativeBinary
    ? []
    : (await readFile(nativeLog, "utf8"))
      .trim()
      .split("\n")
      .filter(Boolean)
      .map((line) => JSON.parse(line));
  const paths = requests.map((request) => request.path);
  const finish = nativeRequests.find((request) => request.op === "capture.finish");
  const imports = nativeRequests.filter((request) => request.op === "asset.import");

  if (state.status !== "incomplete") throw new Error(`Expected incomplete packaged capture, got ${JSON.stringify(state)}`);
  if (paths.filter((path) => path === "/image-fast.png").length !== 1) {
    throw new Error(`Allowed image was not fetched exactly once: ${JSON.stringify(requests)}`);
  }
  if (paths.filter((path) => path === "/image-redirect.png").length !== 1
    || paths.includes("/image-redirect-target.png") || paths.includes("/image-private.png")) {
    throw new Error(`Redirect/private image policy failed: ${JSON.stringify(requests)}`);
  }
  if (requests.some((request) => request.cookie || request.referer)) {
    throw new Error(`Controlled requests exposed cookie/referrer headers: ${JSON.stringify(requests)}`);
  }
  if (!nativeBinary && (imports.length !== 1 || finish?.missing?.length !== 2)) {
    throw new Error(`Native protocol did not receive one asset and two missing resources: ${JSON.stringify(nativeRequests)}`);
  }
  if (nativeBinary && state.missing?.length !== 2) {
    throw new Error(`Real native host did not return two missing resources: ${JSON.stringify(state)}`);
  }

  await setCaptureTimeouts(activeWorker, 100, 1_000);
  const resourcePage = await context.newPage();
  await resourcePage.goto(`http://clipper-security.test:${address.port}/resource-deadline`, {
    waitUntil: "domcontentloaded",
    timeout: 15_000,
  });
  const resourceStarted = performance.now();
  const resourceState = await captureFromPackagedPopup(context, activeWorker, resourcePage);
  const resourceElapsedMs = performance.now() - resourceStarted;
  await new Promise((resolveWait) => setTimeout(resolveWait, 40));
  if (resourceState.status !== "incomplete" || resourceState.missing?.[0]?.reason !== "Image request timed out.") {
    throw new Error(`Stalled body did not report the resource deadline: ${JSON.stringify(resourceState)}`);
  }
  if (resourceElapsedMs > 1_000 || cancelledStalledBodies < 1) {
    throw new Error(`Stalled body was not cancelled promptly (${resourceElapsedMs} ms, cancellations ${cancelledStalledBodies}).`);
  }

  await setCaptureTimeouts(activeWorker, 1_000, 300);
  const totalPage = await context.newPage();
  await totalPage.goto(`http://clipper-security.test:${address.port}/total-deadline`, {
    waitUntil: "domcontentloaded",
    timeout: 15_000,
  });
  const totalStarted = performance.now();
  const totalState = await captureFromPackagedPopup(context, activeWorker, totalPage);
  const totalElapsedMs = performance.now() - totalStarted;
  await new Promise((resolveWait) => setTimeout(resolveWait, 40));
  const totalPaths = requests.map((request) => request.path);
  if (totalState.status !== "incomplete"
    || !totalState.missing?.some((item) => item.reason === "Image capture exceeded the 60-second total retrieval limit.")) {
    throw new Error(`Total retrieval deadline did not produce the expected missing reason: ${JSON.stringify(totalState)}`);
  }
  if (totalPaths.includes("/image-late.png") || totalElapsedMs > 1_200 || cancelledStalledBodies < 2) {
    throw new Error(`Total deadline did not cancel in-flight work and suppress later requests: ${JSON.stringify({ totalPaths, totalElapsedMs, cancelledStalledBodies })}`);
  }

  process.stdout.write(`${JSON.stringify({
    root,
    browser: await page.evaluate(() => navigator.userAgent),
    extensionId,
    native: nativeBinary || "synthetic",
    state: state.status,
    requests,
    assetImports: nativeBinary ? "verified-by-saved-state" : imports.length,
    missing: nativeBinary ? state.missing : finish.missing,
    incidentalRequestsAbsent: paths.filter((path) => path === "/image-fast.png").length === 1,
    redirectTargetAbsent: !paths.includes("/image-redirect-target.png"),
    privateTargetAbsent: !paths.includes("/image-private.png"),
    credentialsOmitted: requests.every((request) => !request.cookie),
    referrerOmitted: requests.every((request) => !request.referer),
    resourceDeadline: {
      elapsedMs: Math.round(resourceElapsedMs),
      reason: resourceState.missing[0].reason,
      readerCancelled: cancelledStalledBodies >= 1,
    },
    totalDeadline: {
      elapsedMs: Math.round(totalElapsedMs),
      reason: totalState.missing.find((item) => item.reason.includes("total retrieval limit"))?.reason,
      readerCancelled: cancelledStalledBodies >= 2,
      laterRequestAbsent: !totalPaths.includes("/image-late.png"),
    },
  }, null, 2)}\n`);
} finally {
  clearTimeout(wallDeadline);
  await cleanup();
}
