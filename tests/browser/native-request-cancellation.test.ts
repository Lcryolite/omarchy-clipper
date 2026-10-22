import assert from "node:assert/strict";
import { test } from "node:test";

type Callback = (...args: any[]) => void;

class FakeEvent<T extends Callback> {
  readonly listeners = new Set<T>();

  addListener(listener: T): void {
    this.listeners.add(listener);
  }

  removeListener(listener: T): void {
    this.listeners.delete(listener);
  }

  snapshot(): T[] {
    return [...this.listeners];
  }
}

class FakePort {
  readonly onMessage = new FakeEvent<(response: unknown) => void>();
  readonly onDisconnect = new FakeEvent<() => void>();
  readonly posted: unknown[] = [];
  disconnectCalls = 0;

  postMessage(message: unknown): void {
    this.posted.push(message);
  }

  disconnect(): void {
    this.disconnectCalls += 1;
  }
}

test("native request cancellation disconnects once and ignores late port events", async () => {
  const ports: FakePort[] = [];
  const runtime = {
    lastError: undefined as { message?: string } | undefined,
    connectNative: () => {
      const port = new FakePort();
      ports.push(port);
      return port;
    },
    onMessage: { addListener: () => undefined },
    onInstalled: { addListener: () => undefined },
  };
  (globalThis as { chrome?: unknown }).chrome = { runtime };

  const moduleUrl = new URL("../../extension/src/background.ts?native-request-cancellation", import.meta.url);
  const background = await import(moduleUrl.href) as typeof import("../../extension/src/background.js");
  const controller = new AbortController();
  const cancelled = new Error("Native request cancelled by caller.");
  const pending = background.nativeRequest({ op: "asset.import" }, controller.signal);
  const port = ports[0]!;
  const lateMessages = port.onMessage.snapshot();
  const lateDisconnects = port.onDisconnect.snapshot();

  controller.abort(cancelled);

  await assert.rejects(pending, (error) => error === cancelled);
  assert.equal(port.disconnectCalls, 1);
  assert.equal(port.onMessage.listeners.size, 0);
  assert.equal(port.onDisconnect.listeners.size, 0);

  lateMessages.forEach((listener) => listener({ ok: true, result: { id: "late-asset" } }));
  runtime.lastError = { message: "Late disconnect." };
  lateDisconnects.forEach((listener) => listener());
  runtime.lastError = undefined;
  await Promise.resolve();
  assert.equal(port.disconnectCalls, 1);

  const finish = background.nativeRequest({ op: "capture.finish" });
  const finishPort = ports[1]!;
  finishPort.onMessage.snapshot().forEach((listener) => listener({ ok: true, result: { id: "clip-1" } }));
  assert.deepEqual(await finish, { id: "clip-1" });
  assert.equal(finishPort.disconnectCalls, 1);

  const alreadyCancelled = new AbortController();
  alreadyCancelled.abort(cancelled);
  await assert.rejects(
    background.nativeRequest({ op: "capture.check" }, alreadyCancelled.signal),
    (error) => error === cancelled,
  );
  assert.equal(ports.length, 2, "a pre-cancelled request must not open a native port");
});
