import type { ArticleCapture, CapturedImage, SelectionCapture } from "./page.js";
import { NativeHostError } from "./native-response.js";

export type NativeRequestMessage = { op: string; [key: string]: unknown };
export type NativeRequest = (request: NativeRequestMessage) => Promise<Record<string, any>>;

export type MissingResource = {
  url: string;
  reason: string;
  category: "permission" | "transient" | "unsupported";
  retryable: boolean;
};

export type CaptureSubmission =
  | {
      kind: "saved";
      clip: Record<string, any>;
      missing: MissingResource[];
    }
  | {
      kind: "duplicate";
      state: "active";
      clipId: string;
      actions: ["open", "update"];
    }
  | {
      kind: "duplicate";
      state: "capturing";
      clipId: string;
      actions: [];
    }
  | {
      kind: "duplicate";
      state: "incomplete";
      clipId: string;
      actions: ["open", "update"];
      missing: MissingResource[];
    }
  | {
      kind: "duplicate";
      state: "trash";
      clipId: string;
      actions: ["restore", "new"];
    };

export type DuplicateAction = "update" | "new";

export type CaptureDependencies = {
  native: NativeRequest;
  fetchResource: (url: string, signal: AbortSignal) => Promise<Response>;
  hasOriginAccess: (url: string) => Promise<boolean>;
  finalize: (html: string, assetIds: Readonly<Record<string, string>>) => string | Promise<string>;
  duplicateAction?: DuplicateAction;
  updateTargetId?: string;
};

const MAX_ASSET_BYTES = 12 * 1024 * 1024;
const MAX_ASSETS = 32;
const RESOURCE_TIMEOUT_MS = 8_000;
const SAFE_IMAGE_TYPES = new Set([
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
  "image/bmp",
]);

class UnsupportedImageError extends Error {}

// Only historical saved records without category metadata need text-based decoding.
const LEGACY_UNSUPPORTED_IMAGE_REASONS = new Set([
  "Asset image dimensions are unsupported",
  "Asset image format is unsupported",
  "Declared MIME type does not match image data",
]);

const UNSUPPORTED_IMAGE_CODES = new Set([
  "IMAGE_DIMENSIONS_UNSUPPORTED",
  "IMAGE_FORMAT_UNSUPPORTED",
  "IMAGE_MIME_MISMATCH",
]);

function record(value: unknown, context: string): Record<string, any> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`Invalid ${context} response from the native host.`);
  }
  return value as Record<string, any>;
}

function safeId(value: unknown, context: string): string {
  if (typeof value !== "string" || !/^[A-Za-z0-9._-]{1,128}$/.test(value)) {
    throw new Error(`Invalid ${context} returned by the native host.`);
  }
  return value;
}

function normalizedMissing(value: unknown): MissingResource[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((candidate): MissingResource[] => {
    if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) return [];
    const item = candidate as Record<string, unknown>;
    const url = typeof item.url === "string" ? item.url : "";
    const reason = typeof item.reason === "string" ? item.reason : "Image capture failed.";
    const category = item.category === "permission" || item.category === "unsupported" || item.category === "transient"
      ? item.category
      : reason.startsWith("Permission is required")
        ? "permission"
        : /not a supported image type/i.test(reason) || LEGACY_UNSUPPORTED_IMAGE_REASONS.has(reason)
          ? "unsupported"
          : "transient";
    return [{
      url,
      reason,
      category,
      retryable: typeof item.retryable === "boolean" ? item.retryable : category !== "unsupported",
    }];
  });
}

function duplicate(existing: Record<string, unknown>): CaptureSubmission {
  const clipId = safeId(existing.id, "clip ID");
  if (typeof existing.deletedAt === "string" && existing.deletedAt !== "") {
    return { kind: "duplicate", state: "trash", clipId, actions: ["restore", "new"] };
  }
  const pending = existing.pendingCapture && typeof existing.pendingCapture === "object" && !Array.isArray(existing.pendingCapture)
    ? existing.pendingCapture as Record<string, unknown>
    : undefined;
  if (pending?.status === "pending" || existing.status === "capturing") {
    return { kind: "duplicate", state: "capturing", clipId, actions: [] };
  }
  if (pending || existing.status === "incomplete") {
    const source = pending ?? existing;
    const missing = normalizedMissing(source.missing);
    if (missing.length === 0 && typeof source.reason === "string" && source.reason) {
      missing.push({ url: "", reason: source.reason, category: "transient", retryable: true });
    }
    return { kind: "duplicate", state: "incomplete", clipId, actions: ["open", "update"], missing };
  }
  return { kind: "duplicate", state: "active", clipId, actions: ["open", "update"] };
}

function isPublicHttpUrl(value: string): boolean {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return false;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return false;

  const hostname = url.hostname.toLowerCase().replace(/^\[|\]$/g, "");
  if (
    hostname === "localhost" ||
    hostname.endsWith(".localhost") ||
    hostname.endsWith(".local") ||
    hostname === "0.0.0.0" ||
    hostname === "::" ||
    hostname === "::1"
  ) {
    return false;
  }

  const ipv4 = hostname.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  if (ipv4) {
    const octets = ipv4.slice(1).map(Number);
    if (octets.some((octet) => octet > 255)) return false;
    const [a = 0, b = 0] = octets;
    if (
      a === 0 ||
      a === 10 ||
      a === 127 ||
      (a === 169 && b === 254) ||
      (a === 172 && b >= 16 && b <= 31) ||
      (a === 192 && b === 168)
    ) {
      return false;
    }
  }

  if (hostname.includes(":")) {
    const normalized = hostname.toLowerCase();
    if (normalized.startsWith("fc") || normalized.startsWith("fd") || normalized.startsWith("fe8") || normalized.startsWith("fe9") || normalized.startsWith("fea") || normalized.startsWith("feb")) {
      return false;
    }
  }
  return true;
}

async function readBounded(response: Response): Promise<Uint8Array> {
  if (!response.ok) throw new Error(`Image request returned HTTP ${response.status}.`);
  const declaredLength = Number(response.headers.get("content-length") ?? "0");
  if (Number.isFinite(declaredLength) && declaredLength > MAX_ASSET_BYTES) {
    throw new UnsupportedImageError("Image exceeds the 12 MiB capture limit.");
  }

  if (!response.body) {
    const bytes = new Uint8Array(await response.arrayBuffer());
    if (bytes.byteLength > MAX_ASSET_BYTES) throw new UnsupportedImageError("Image exceeds the 12 MiB capture limit.");
    return bytes;
  }

  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  while (true) {
    const item = await reader.read();
    if (item.done) break;
    length += item.value.byteLength;
    if (length > MAX_ASSET_BYTES) {
      await reader.cancel();
      throw new UnsupportedImageError("Image exceeds the 12 MiB capture limit.");
    }
    chunks.push(item.value);
  }

  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

function base64(bytes: Uint8Array): string {
  const pieces: string[] = [];
  const step = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += step) {
    pieces.push(String.fromCharCode(...bytes.subarray(offset, offset + step)));
  }
  return btoa(pieces.join(""));
}

async function fetchWithTimeout(
  fetchResource: CaptureDependencies["fetchResource"],
  url: string,
): Promise<Response> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), RESOURCE_TIMEOUT_MS);
  try {
    return await fetchResource(url, controller.signal);
  } catch (error) {
    if (controller.signal.aborted) throw new Error("Image request timed out.");
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

function reason(error: unknown): string {
  return error instanceof Error && error.message ? error.message.slice(0, 240) : "Image capture failed.";
}

function failure(url: string, error: unknown): MissingResource {
  const message = reason(error);
  const unsupported = error instanceof UnsupportedImageError
    || (error instanceof NativeHostError && UNSUPPORTED_IMAGE_CODES.has(error.code ?? ""));
  return {
    url,
    reason: message,
    category: unsupported ? "unsupported" : "transient",
    retryable: !unsupported,
  };
}

async function importCaptureAssets(
  images: readonly CapturedImage[],
  dependencies: CaptureDependencies,
): Promise<{
  assetIds: string[];
  assetsByKey: Record<string, string>;
  missing: MissingResource[];
}> {
  const assetIds: string[] = [];
  const assetsByKey: Record<string, string> = {};
  const missing: MissingResource[] = [];
  const assetsByUrl = new Map<string, string>();
  const missingUrls = new Set<string>();
  let uniqueResources = 0;

  for (const image of images) {
    const resourceKey = image.url || `unresolved:${image.key}`;
    const existingAsset = assetsByUrl.get(resourceKey);
    if (existingAsset) {
      assetsByKey[image.key] = existingAsset;
      continue;
    }
    if (missingUrls.has(resourceKey)) continue;
    missingUrls.add(resourceKey);

    if (image.failure) {
      missing.push({
        url: image.url,
        reason: image.failure.reason,
        category: "unsupported",
        retryable: false,
      });
      continue;
    }

    if (uniqueResources >= MAX_ASSETS) {
      missing.push({
        url: image.url,
        reason: "Skipped because one capture supports at most 32 images.",
        category: "unsupported",
        retryable: false,
      });
      continue;
    }
    uniqueResources += 1;
    if (!isPublicHttpUrl(image.url)) {
      missing.push({
        url: image.url,
        reason: "Local and private network image URLs are not captured.",
        category: "unsupported",
        retryable: false,
      });
      continue;
    }
    if (!(await dependencies.hasOriginAccess(image.url))) {
      missing.push({
        url: image.url,
        reason: "Permission is required to retry this public image origin.",
        category: "permission",
        retryable: true,
      });
      continue;
    }

    try {
      const response = await fetchWithTimeout(dependencies.fetchResource, image.url);
      const mime = (response.headers.get("content-type") ?? "").split(";", 1)[0]!.trim().toLowerCase();
      if (!SAFE_IMAGE_TYPES.has(mime)) throw new UnsupportedImageError("Resource is not a supported image type.");
      const bytes = await readBounded(response);
      const imported = record(
        await dependencies.native({
          op: "asset.import",
          data: base64(bytes),
          mime,
          name: image.name,
        }),
        "asset.import",
      );
      const assetId = safeId(imported.id, "asset ID");
      assetIds.push(assetId);
      assetsByUrl.set(resourceKey, assetId);
      assetsByKey[image.key] = assetId;
    } catch (error) {
      missing.push(failure(image.url, error));
    }
  }

  return { assetIds, assetsByKey, missing };
}

function initialMissing(images: readonly CapturedImage[]): MissingResource[] {
  const seen = new Set<string>();
  const missing: MissingResource[] = [];
  for (const image of images) {
    const resourceKey = image.url || `unresolved:${image.key}`;
    if (seen.has(resourceKey)) continue;
    seen.add(resourceKey);
    missing.push(image.failure
      ? {
          url: image.url,
          reason: image.failure.reason,
          category: "unsupported",
          retryable: false,
        }
      : {
          url: image.url,
          reason: "Image import has not completed yet.",
          category: "transient",
          retryable: true,
        });
  }
  return missing;
}

export async function submitArticleCapture(
  capture: ArticleCapture | SelectionCapture,
  dependencies: CaptureDependencies,
): Promise<CaptureSubmission> {
  const checked = record(
    await dependencies.native({
      op: "capture.check",
      sourceUrl: capture.sourceUrl,
      scope: capture.scope,
    }),
    "capture.check",
  );
  const existing = checked.existing;
  if (existing && typeof existing === "object" && dependencies.duplicateAction === undefined) {
    return duplicate(existing as Record<string, unknown>);
  }

  const updateId = dependencies.duplicateAction === "update"
    ? dependencies.updateTargetId
      ? safeId(dependencies.updateTargetId, "update target ID")
      : existing && typeof existing === "object"
        ? safeId((existing as Record<string, unknown>).id, "update ID")
        : undefined
    : undefined;
  const recoverableHtml = await dependencies.finalize(capture.html, {});
  const beginRequest: NativeRequestMessage = {
    op: "capture.begin",
    title: capture.title,
    sourceUrl: capture.sourceUrl,
    scope: capture.scope,
    html: recoverableHtml,
    text: capture.text,
    missing: initialMissing(capture.images),
  };
  if (updateId) beginRequest.updateId = updateId;

  const begun = record(await dependencies.native(beginRequest), "capture.begin");
  if (begun.existing && typeof begun.existing === "object") {
    return duplicate(begun.existing as Record<string, unknown>);
  }
  const captureId = safeId(begun.captureId, "capture ID");

  try {
    const { assetIds, assetsByKey, missing } = await importCaptureAssets(capture.images, dependencies);

    const html = await dependencies.finalize(capture.html, assetsByKey);
    const clip = record(
      await dependencies.native({
        op: "capture.finish",
        captureId,
        html,
        text: capture.text,
        assetIds,
        missing,
      }),
      "capture.finish",
    );
    return { kind: "saved", clip, missing };
  } catch (error) {
    try {
      await dependencies.native({ op: "capture.abort", captureId, reason: reason(error) });
    } catch {
      // A disconnected host still retains capture.begin for recovery.
    }
    throw error;
  }
}

export async function submitSelectionCapture(
  capture: SelectionCapture,
  dependencies: CaptureDependencies,
): Promise<CaptureSubmission> {
  return submitArticleCapture(capture, dependencies);
}
