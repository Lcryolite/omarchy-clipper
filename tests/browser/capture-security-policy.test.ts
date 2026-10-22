import assert from "node:assert/strict";
import { test } from "node:test";

import { submitArticleCapture, type NativeRequest } from "../../extension/src/capture.js";
import type { ArticleCapture } from "../../extension/src/page.js";

function capture(urls: string[]): ArticleCapture {
  return {
    kind: "article",
    title: "Security fixture",
    sourceUrl: "https://example.test/article",
    scope: "article",
    html: "<article>Security fixture</article>",
    text: "Security fixture",
    images: urls.map((url, index) => ({
      key: `image-${index + 1}`,
      url,
      alt: `image ${index + 1}`,
      name: `image-${index + 1}.png`,
    })),
  };
}

function native(importAsset: (request: Record<string, unknown>) => Promise<Record<string, unknown>> = async () => ({ id: "asset-1" })): NativeRequest {
  return async (request) => {
    if (request.op === "capture.check") return { existing: null };
    if (request.op === "capture.begin") return { captureId: "capture-1", clipId: "clip-1" };
    if (request.op === "asset.import") return importAsset(request);
    if (request.op === "capture.finish") return { id: "clip-1", status: "incomplete" };
    throw new Error(`Unexpected operation ${request.op}`);
  };
}

test("controlled image retrieval rejects unsafe addresses and redirects before import", async () => {
  const rejected = [
    "https://user:secret@images.example.test/private.png",
    "https://intranet/image.png",
    "https://[::ffff:127.0.0.1]/mapped.png",
    "https://224.0.0.1/multicast.png",
  ];
  const urls = [...rejected, "https://images.example.test/redirect.png"];
  const fetched: string[] = [];
  let imports = 0;
  const result = await submitArticleCapture(capture(urls), {
    native: native(async () => {
      imports += 1;
      return { id: "unexpected" };
    }),
    hasOriginAccess: async () => true,
    fetchResource: async (url) => {
      fetched.push(url);
      return new Response(null, { status: 302, headers: { location: "https://redirected.example.test/image.png" } });
    },
    finalize: (html) => html,
  });

  assert.equal(result.kind, "saved");
  if (result.kind !== "saved") return;
  assert.deepEqual(fetched, ["https://images.example.test/redirect.png"]);
  assert.equal(imports, 0);
  assert.equal(result.missing.length, 5);
  assert.equal(result.missing.at(-1)?.reason, "Image redirects are not captured.");
});

test("resource deadline includes body consumption and cancels a stalled reader", async () => {
  let cancelled = false;
  const stalled = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new Uint8Array([137, 80, 78, 71]));
    },
    cancel() {
      cancelled = true;
    },
  });
  const result = await submitArticleCapture(capture(["https://images.example.test/stalled.png"]), {
    native: native(),
    hasOriginAccess: async () => true,
    fetchResource: async () => new Response(stalled, { headers: { "content-type": "image/png" } }),
    finalize: (html) => html,
    resourceTimeoutMs: 30,
    totalTimeoutMs: 500,
  });

  assert.equal(result.kind, "saved");
  if (result.kind !== "saved") return;
  assert.equal(cancelled, true);
  assert.equal(result.missing[0]?.reason, "Image request timed out.");
});

test("total retrieval deadline prevents starting later resources", async () => {
  const fetched: string[] = [];
  let imports = 0;
  let finishedRequest: Record<string, unknown> | undefined;
  let importSignal: AbortSignal | undefined;
  const unboundedOperations: string[] = [];
  const startedAt = performance.now();
  const result = await submitArticleCapture(capture([
    "https://images.example.test/first.png",
    "https://images.example.test/second.png",
  ]), {
    native: async (request, signal?: AbortSignal) => {
      if (signal) importSignal = signal;
      else unboundedOperations.push(request.op);
      if (request.op === "capture.check") return { existing: null };
      if (request.op === "capture.begin") return { captureId: "capture-1", clipId: "clip-1" };
      if (request.op === "asset.import") {
        assert.equal(request.captureId, "capture-1");
        imports += 1;
        await new Promise((resolve) => setTimeout(resolve, 500));
        return { id: "asset-first" };
      }
      if (request.op === "capture.finish") {
        finishedRequest = request;
        return { id: "clip-1", status: "incomplete" };
      }
      throw new Error(`Unexpected operation ${request.op}`);
    },
    hasOriginAccess: async () => true,
    fetchResource: async (url) => {
      fetched.push(url);
      await new Promise((resolve) => setTimeout(resolve, 25));
      return new Response(new Uint8Array([137, 80, 78, 71]), { headers: { "content-type": "image/png" } });
    },
    finalize: (html) => html,
    resourceTimeoutMs: 100,
    totalTimeoutMs: 50,
  });

  assert.equal(result.kind, "saved");
  if (result.kind !== "saved") return;
  assert.ok(performance.now() - startedAt < 300, "asset.import must remain inside the total deadline");
  assert.deepEqual(fetched, ["https://images.example.test/first.png"]);
  assert.equal(imports, 1);
  assert.equal(importSignal?.aborted, true);
  assert.deepEqual(unboundedOperations, ["capture.check", "capture.begin", "capture.finish"]);
  assert.deepEqual(finishedRequest?.assetIds, []);
  assert.equal(result.missing.length, 2);
  assert.ok(result.missing.every((item) => item.reason === "Image capture exceeded the 60-second total retrieval limit."));
});

test("caller cancellation stops body consumption and aborts the pending capture", async () => {
  const controller = new AbortController();
  const fetched: string[] = [];
  const operations: string[] = [];
  let readerCancelled = false;
  const stalled = new ReadableStream<Uint8Array>({
    start(streamController) {
      streamController.enqueue(new Uint8Array([137, 80, 78, 71]));
    },
    cancel() {
      readerCancelled = true;
    },
  });

  await assert.rejects(submitArticleCapture(capture([
    "https://images.example.test/first.png",
    "https://images.example.test/second.png",
  ]), {
    native: async (request, signal?: AbortSignal) => {
      assert.equal(signal, undefined, `${request.op} must remain available for cancellation recovery`);
      operations.push(request.op);
      if (request.op === "capture.check") return { existing: null };
      if (request.op === "capture.begin") return { captureId: "capture-cancelled" };
      if (request.op === "capture.abort") return { captureId: "capture-cancelled", status: "aborted" };
      throw new Error(`Unexpected operation ${request.op}`);
    },
    hasOriginAccess: async () => true,
    fetchResource: async (url) => {
      fetched.push(url);
      setTimeout(() => controller.abort(new Error("Capture cancelled by caller.")), 5);
      return new Response(stalled, { headers: { "content-type": "image/png" } });
    },
    finalize: (html) => html,
    resourceTimeoutMs: 100,
    totalTimeoutMs: 500,
    signal: controller.signal,
  }), /Capture cancelled by caller\./);

  assert.equal(readerCancelled, true);
  assert.deepEqual(fetched, ["https://images.example.test/first.png"]);
  assert.deepEqual(operations, ["capture.check", "capture.begin", "capture.abort"]);
});
