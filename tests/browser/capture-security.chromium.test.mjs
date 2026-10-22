import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { test } from "node:test";

import { build } from "esbuild";

const chromium = process.env.CLIPPER_CHROMIUM ?? "/usr/bin/chromium";

function listen(server) {
  return new Promise((resolveListen, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolveListen(server.address()));
  });
}

function isolatedBrowserEnv(temporary) {
  return {
    PATH: process.env.PATH ?? "/usr/bin:/bin",
    HOME: join(temporary, "home"),
    XDG_CONFIG_HOME: join(temporary, "config"),
    XDG_CACHE_HOME: join(temporary, "cache"),
    XDG_STATE_HOME: join(temporary, "state"),
    XDG_DATA_HOME: join(temporary, "data"),
    LANG: "C.UTF-8",
  };
}

function runChromium(args, temporary) {
  return new Promise((resolveRun, reject) => {
    const child = spawn(chromium, args, {
      stdio: ["ignore", "pipe", "pipe"],
      env: isolatedBrowserEnv(temporary),
    });
    const stdout = [];
    const stderr = [];
    let timedOut = false;
    const deadline = setTimeout(() => {
      timedOut = true;
      child.kill("SIGKILL");
    }, 15_000);
    child.stdout.on("data", (chunk) => stdout.push(chunk));
    child.stderr.on("data", (chunk) => stderr.push(chunk));
    child.once("error", (error) => {
      clearTimeout(deadline);
      reject(error);
    });
    child.once("exit", (code, signal) => {
      clearTimeout(deadline);
      const output = Buffer.concat(stdout).toString("utf8");
      if (timedOut) reject(new Error("Chromium exceeded the 15-second wall deadline and was killed."));
      else if (code === 0) resolveRun(output);
      else reject(new Error(`Chromium exited with ${code ?? signal}: ${Buffer.concat(stderr).toString("utf8")}`));
    });
  });
}

test("complete Chromium capture stays inert while article and selection retain image metadata", { timeout: 20_000 }, async () => {
  const temporary = await mkdtemp(join(tmpdir(), "clipper-inert-capture-"));
  let bundle = "";
  let captureActive = false;
  const captureRequests = [];
  const pixel = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=", "base64");

  const server = createServer((request, response) => {
    const url = new URL(request.url ?? "/", "http://127.0.0.1");
    if (url.pathname === "/capture.js") {
      response.writeHead(200, { "content-type": "text/javascript", "cache-control": "no-store" });
      response.end(bundle);
      return;
    }
    if (url.pathname === "/phase") {
      captureActive = url.searchParams.get("active") === "1";
      response.writeHead(204, { "cache-control": "no-store" });
      response.end();
      return;
    }
    if (url.pathname.endsWith(".png") || url.pathname.endsWith(".mp4")) {
      if (captureActive) captureRequests.push(url.pathname);
      response.writeHead(200, {
        "content-type": "image/png",
        "content-length": String(pixel.length),
        "cache-control": "no-store",
      });
      response.end(pixel);
      return;
    }
    if (url.pathname === "/") {
      response.writeHead(200, { "content-type": "text/html", "cache-control": "no-store" });
      response.end(`<!doctype html>
        <title>Inert capture fixture</title>
        <main><article>
          <h1>Inert capture fixture</h1>
          <p>This meaningful article body remains readable after capture without loading promoted lazy resources.</p>
          <figure id="selected"><img data-src="/lazy-article.png" alt="Architecture diagram"><figcaption>Architecture diagram</figcaption>
            <video poster="/selection-poster.png" preload="none"><source src="/selection-video.mp4" type="video/mp4"></video>
          </figure>
        </article></main>
        <script src="/capture.js"></script>
        <script>
          (async () => {
            await fetch("/phase?active=1", { cache: "no-store" });
            const article = ClipperCapture.capturePage(document);
            const range = document.createRange();
            range.selectNode(document.querySelector("#selected"));
            const selection = getSelection();
            selection.removeAllRanges();
            selection.addRange(range);
            const selected = ClipperCapture.capturePage(document, "selection");
            await new Promise((resolve) => setTimeout(resolve, 250));
            await fetch("/phase?active=0", { cache: "no-store" });
            const result = document.createElement("pre");
            result.id = "capture-result";
            result.textContent = JSON.stringify({ article, selected });
            document.body.append(result);
          })().catch((error) => {
            const result = document.createElement("pre");
            result.id = "capture-error";
            result.textContent = String(error?.stack ?? error);
            document.body.append(result);
          });
        </script>`);
      return;
    }
    response.writeHead(404);
    response.end();
  });

  try {
    const built = await build({
      stdin: {
        contents: 'export { capturePage } from "./extension/src/page.ts";',
        resolveDir: resolve("."),
        sourcefile: "capture-security-entry.ts",
      },
      bundle: true,
      format: "iife",
      globalName: "ClipperCapture",
      platform: "browser",
      target: "chrome120",
      write: false,
    });
    bundle = built.outputFiles[0].text;
    const address = await listen(server);
    const html = await runChromium([
      "--headless=new",
      "--no-sandbox",
      "--disable-gpu",
      "--disable-background-networking",
      `--user-data-dir=${join(temporary, "profile")}`,
      "--virtual-time-budget=3000",
      "--dump-dom",
      `http://127.0.0.1:${address.port}/`,
    ], temporary);

    assert.doesNotMatch(html, /id="capture-error"/);
    const encoded = html.match(/<pre id="capture-result">([^<]+)<\/pre>/)?.[1];
    assert.ok(encoded, "Chromium did not return the capture result");
    const result = JSON.parse(encoded.replaceAll("&quot;", '"').replaceAll("&amp;", "&"));
    assert.equal(result.article.kind, "article");
    assert.match(result.article.text, /meaningful article body/);
    assert.deepEqual(result.article.images.map(({ url, alt }) => ({ url, alt })), [{
      url: `http://127.0.0.1:${address.port}/lazy-article.png`,
      alt: "Architecture diagram",
    }]);
    assert.equal(result.selected.kind, "selection");
    assert.match(result.selected.text, /Architecture diagram/);
    assert.equal(result.selected.images[0]?.url, `http://127.0.0.1:${address.port}/lazy-article.png`);
    assert.deepEqual(captureRequests, []);
  } finally {
    await new Promise((resolveClose) => server.close(resolveClose));
    await rm(temporary, { recursive: true, force: true });
  }
});
