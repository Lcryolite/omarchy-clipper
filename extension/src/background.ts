import {
  submitArticleCapture,
  submitSelectionCapture,
  type CaptureDependencies,
  type MissingResource,
  type CaptureSubmission,
  type DuplicateAction,
  type NativeRequest,
} from "./capture.js";
import type { ArticleCapture, PageCapture, PageInspection, SelectionCapture } from "./page.js";
import { unwrapNativeResponse } from "./native-response.js";

const NATIVE_HOST = "com.omarchy.clipper";
const STATUS_KEY = "captureState";
const SETTINGS_TIMEOUT_MS = 500;
const NATIVE_TIMEOUT_MS = 25_000;
const REGION_CAPTURE_TIMEOUT_MS = 190_000;

type CaptureStatus =
  | "idle"
  | "inspecting"
  | "ready-to-capture"
  | "capturing"
  | "fallback"
  | "duplicate-active"
  | "duplicate-trash"
  | "ready"
  | "incomplete"
  | "error";

type ExtensionState = {
  status: CaptureStatus;
  message: string;
  captureMode?: "article" | "selection";
  sourceUrl?: string;
  title?: string;
  tabId?: number;
  clipId?: string;
  missing?: MissingResource[];
  permissionOrigins?: string[];
  detected?: string;
  detectedLabel?: string;
  scopes?: Array<{ value: string; label: string }>;
  selectedScope?: string;
  cleaningAvailable?: boolean;
  cleaning?: "default" | "deep";
  scopeNote?: string;
};

type PopupRequest =
  | { type: "settings.get" }
  | { type: "status.get" }
  | { type: "capture.inspect" }
  | {
      type: "capture.start";
      mode?: "article" | "selection";
      duplicateAction?: DuplicateAction;
      scope?: string;
      cleaning?: "default" | "deep";
    }
  | { type: "capture.region" }
  | { type: "clip.open"; id: string }
  | { type: "clip.restore"; id: string };

type ActiveTab = { id: number; url: string; title: string };

type StoredStates = {
  version: 2;
  pages: Record<string, ExtensionState>;
};

class ScopedOperationError extends Error {
  constructor(message: string, readonly state: ExtensionState) {
    super(message);
  }
}

const ID_PATTERN = /^[A-Za-z0-9._-]{1,128}$/;

async function nativeRequest(request: { op: string; [key: string]: unknown }): Promise<Record<string, any>> {
  return new Promise((resolve, reject) => {
    const port = chrome.runtime.connectNative(NATIVE_HOST);
    const timeoutMs = request.op === "capture.region"
      ? REGION_CAPTURE_TIMEOUT_MS
      : request.op === "settings.get"
        ? SETTINGS_TIMEOUT_MS
        : NATIVE_TIMEOUT_MS;
    let settled = false;
    const finish = (callback: () => void): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      callback();
      port.disconnect();
    };
    const timeout = setTimeout(() => {
      finish(() => reject(new Error(`The native host did not respond within ${timeoutMs / 1_000} seconds.`)));
    }, timeoutMs);

    port.onMessage.addListener((response: unknown) => {
      finish(() => {
        try {
          resolve(unwrapNativeResponse(response));
        } catch (error) {
          reject(error);
        }
      });
    });
    port.onDisconnect.addListener(() => {
      if (settled) return;
      const message = chrome.runtime.lastError?.message;
      finish(() => reject(new Error(message || "The native host disconnected before replying.")));
    });

    try {
      port.postMessage(request);
    } catch (error) {
      finish(() => reject(error));
    }
  });
}

function errorMessage(error: unknown): string {
  return error instanceof Error && error.message ? error.message : "Capture failed.";
}

function badgeFor(status: CaptureStatus): { text: string; color: string } {
  if (status === "capturing") return { text: "…", color: "#6b7280" };
  if (status === "ready") return { text: "✓", color: "#2f855a" };
  if (status === "incomplete" || status === "fallback") return { text: "!", color: "#b7791f" };
  if (status === "error") return { text: "×", color: "#c53030" };
  return { text: "", color: "#6b7280" };
}

const inspectionGenerations = new Map<string, number>();

function invalidateInspection(tab: Pick<ActiveTab, "id" | "url">): number {
  const key = stateKey(tab);
  const generation = (inspectionGenerations.get(key) ?? 0) + 1;
  inspectionGenerations.set(key, generation);
  return generation;
}

async function inspectActivePage(): Promise<ExtensionState> {
  const tab = await activeTab();
  const key = stateKey(tab);
  const generation = invalidateInspection(tab);
  await setState({
    status: "inspecting",
    message: "Inspecting this page…",
    sourceUrl: tab.url,
    title: tab.title,
    tabId: tab.id,
  });
  let inspection: PageInspection;
  try {
    inspection = await sendPageCommand<PageInspection>(tab.id, { type: "page.inspect" });
  } catch (error) {
    if (inspectionGenerations.get(key) !== generation || (await getState(tab)).status !== "inspecting") {
      return getState(tab);
    }
    throw error;
  }
  const current = await getState(tab);
  if (inspectionGenerations.get(key) !== generation || current.status !== "inspecting") return current;
  if (inspection.kind === "fallback") {
    return setState({
      status: "fallback",
      message: inspection.reason,
      sourceUrl: inspection.sourceUrl,
      title: inspection.title,
      tabId: tab.id,
    });
  }
  return setState({
    status: "ready-to-capture",
    message: `${inspection.label} detected. Review the capture choices, then save when ready.`,
    captureMode: "article",
    sourceUrl: inspection.sourceUrl,
    title: inspection.title,
    tabId: tab.id,
    detected: inspection.detected,
    detectedLabel: inspection.label,
    scopes: inspection.scopes,
    selectedScope: inspection.defaultScope,
    cleaningAvailable: inspection.cleaningAvailable,
    cleaning: "default",
    ...(inspection.note ? { scopeNote: inspection.note } : {}),
  });
}

function stateKey(tab: Pick<ActiveTab, "id" | "url">): string {
  return `${tab.id}\n${tab.url}`;
}

function defaultState(tab?: ActiveTab): ExtensionState {
  return {
    status: "idle",
    message: "Ready to capture this page.",
    ...(tab ? { tabId: tab.id, sourceUrl: tab.url, title: tab.title } : {}),
  };
}

function storedPages(value: unknown): Record<string, ExtensionState> {
  if (value && typeof value === "object" && (value as Partial<StoredStates>).version === 2) {
    const pages = (value as Partial<StoredStates>).pages;
    if (pages && typeof pages === "object") return { ...pages };
  }
  const legacy = value as ExtensionState | undefined;
  if (legacy && typeof legacy === "object" && typeof legacy.tabId === "number" && legacy.sourceUrl) {
    return { [stateKey({ id: legacy.tabId, url: legacy.sourceUrl })]: legacy };
  }
  return {};
}

let stateWrite = Promise.resolve();

async function setState(state: ExtensionState): Promise<ExtensionState> {
  const write = async (): Promise<void> => {
    const stored = await chrome.storage.session.get(STATUS_KEY);
    const pages = storedPages(stored[STATUS_KEY]);
    if (typeof state.tabId === "number" && state.sourceUrl) {
      pages[stateKey({ id: state.tabId, url: state.sourceUrl })] = state;
    }
    await chrome.storage.session.set({ [STATUS_KEY]: { version: 2, pages } satisfies StoredStates });
    const badge = badgeFor(state.status);
    if (typeof state.tabId === "number" && state.sourceUrl) {
      let currentUrl: string | undefined;
      try {
        currentUrl = (await chrome.tabs.get(state.tabId)).url;
      } catch {
        // The tab was closed before this capture state settled.
      }
      if (currentUrl === state.sourceUrl) {
        await chrome.action.setBadgeBackgroundColor({ color: badge.color, tabId: state.tabId });
        await chrome.action.setBadgeText({ text: badge.text, tabId: state.tabId });
      }
    } else {
      await chrome.action.setBadgeBackgroundColor({ color: badge.color });
      await chrome.action.setBadgeText({ text: badge.text });
    }
    void chrome.runtime.sendMessage({ type: "status.changed", state }).catch(() => undefined);
  };
  stateWrite = stateWrite.then(write, write);
  await stateWrite;
  return state;
}

async function getState(tab?: ActiveTab): Promise<ExtensionState> {
  const stored = await chrome.storage.session.get(STATUS_KEY);
  const pages = storedPages(stored[STATUS_KEY]);
  if (!tab) return defaultState();
  return pages[stateKey(tab)] ?? defaultState(tab);
}

async function activeTab(): Promise<ActiveTab> {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || typeof tab.id !== "number" || !tab.url) throw new Error("No active webpage is available.");
  const url = new URL(tab.url);
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error("This browser page is restricted. Open a regular HTTP or HTTPS page to capture it.");
  }
  return { id: tab.id, url: tab.url, title: tab.title ?? "" };
}

async function sendPageCommand<T>(tabId: number, message: unknown): Promise<T> {
  await chrome.scripting.executeScript({ target: { tabId }, files: ["dist/content.js"] });
  const response = (await chrome.tabs.sendMessage(tabId, message)) as {
    ok?: unknown;
    result?: T;
    error?: unknown;
  };
  if (response?.ok !== true) {
    throw new Error(typeof response?.error === "string" ? response.error : "The page did not return capture content.");
  }
  return response.result as T;
}

function permissionOrigins(missing: MissingResource[]): string[] {
  const origins = new Set<string>();
  for (const item of missing) {
    if (item.category !== "permission" && !item.reason.startsWith("Permission is required")) continue;
    try {
      const url = new URL(item.url);
      if (url.protocol === "https:" || url.protocol === "http:") origins.add(`${url.origin}/*`);
    } catch {
      // The local boundary also validates URLs; invalid missing entries are not permission candidates.
    }
  }
  return [...origins];
}

function dependencies(
  tabId: number,
  sourceUrl: string,
  duplicateAction?: DuplicateAction,
  updateTargetId?: string,
): CaptureDependencies {
  const sourceOrigin = new URL(sourceUrl).origin;
  const result: CaptureDependencies = {
    native: nativeRequest as NativeRequest,
    fetchResource: (url, signal) =>
      fetch(url, {
        method: "GET",
        credentials: "omit",
        cache: "no-store",
        redirect: "follow",
        referrerPolicy: "no-referrer",
        signal,
      }),
    hasOriginAccess: async (url) => {
      const origin = new URL(url).origin;
      return origin === sourceOrigin || chrome.permissions.contains({ origins: [`${origin}/*`] });
    },
    finalize: (html, assetIds) =>
      sendPageCommand<string>(tabId, { type: "page.finalize", html, assetIds }),
  };
  if (duplicateAction) result.duplicateAction = duplicateAction;
  if (updateTargetId) result.updateTargetId = updateTargetId;
  return result;
}

async function stateFromSubmission(
  tab: ActiveTab,
  capture: ArticleCapture | SelectionCapture,
  submission: CaptureSubmission | Awaited<ReturnType<typeof submitSelectionCapture>>,
  cleaning?: "default" | "deep",
): Promise<ExtensionState> {
  const scopeLabels: Partial<Record<ArticleCapture["scope"], string>> = {
    article: "Readable article",
    "page-index": "Visible page index",
    "github-readme": "Repository README",
    "github-issue-loaded": "Loaded Issue discussion",
    "github-pr-loaded": "Loaded pull request discussion",
    "discuz-main-post": "Main post",
    "discuz-current-page": "Main post and current-page replies",
    "discuz-current-page-replies": "Visible current-page replies",
  };
  const siteScope = capture.kind === "article" ? scopeLabels[capture.scope] : undefined;
  const base = {
    sourceUrl: capture.sourceUrl,
    title: capture.title,
    tabId: tab.id,
    selectedScope: capture.scope,
    ...(cleaning ? { cleaning } : {}),
    ...(siteScope
      ? { scopeNote: `Scope: ${siteScope}. Cleaning: ${cleaning === "deep" ? "Deep clean" : "Standard"}.` }
      : {}),
  };
  const captureMode = capture.kind === "selection" ? "selection" : "article";
  if (submission.kind === "duplicate") {
    if (submission.state === "capturing") {
      return setState({
        ...base,
        captureMode,
        status: "capturing",
        message: "A capture for this page is already in progress.",
        clipId: submission.clipId,
      });
    }
    if (submission.state === "incomplete") {
      return setState({
        ...base,
        captureMode,
        status: "incomplete",
        message: "This page is already saved with missing images.",
        clipId: submission.clipId,
        missing: submission.missing,
        permissionOrigins: permissionOrigins(submission.missing),
      });
    }
    return setState({
      ...base,
      captureMode,
      status: submission.state === "active" ? "duplicate-active" : "duplicate-trash",
      message: submission.state === "active"
        ? "This page is already saved. Open it or update it explicitly."
        : "A matching clip is in Trash. Restore it or create a new capture explicitly.",
      clipId: submission.clipId,
    });
  }

  const nativeStatus = submission.clip.updateStatus ?? submission.clip.status;
  const status = submission.missing.length > 0 || nativeStatus === "incomplete" ? "incomplete" : "ready";
  const unsupportedOnly = submission.missing.length > 0 && submission.missing.every((item) => item.retryable === false);
  const clipId = typeof submission.clip.id === "string" && ID_PATTERN.test(submission.clip.id)
    ? submission.clip.id
    : undefined;
  return setState({
    ...base,
    captureMode,
    status,
    message: status === "ready"
      ? "Saved for offline reading."
      : unsupportedOnly
        ? "Saved incomplete because an image format or source is unsupported."
      : submission.clip.updateStatus === "incomplete"
        ? "The previous readable clip is safe. Reopen this source page to retry the incomplete update."
        : "Saved with missing images. Reopen this source page to retry them.",
    ...(clipId ? { clipId } : {}),
    missing: submission.missing,
    permissionOrigins: permissionOrigins(submission.missing),
  });
}

const captureRequests = new Map<string, Promise<ExtensionState>>();
const captureSubmissions = new Map<string, Promise<CaptureSubmission>>();

async function performCapture(
  tab: ActiveTab,
  mode: "article" | "selection" = "article",
  duplicateAction?: DuplicateAction,
  updateTargetId?: string,
  selectedScope?: string,
  cleaning: "default" | "deep" = "default",
): Promise<ExtensionState> {
  await setState({
    status: "capturing",
    message: mode === "selection" ? "Saving the selected content…" : "Capturing readable content…",
    captureMode: mode,
    sourceUrl: tab.url,
    title: tab.title,
    tabId: tab.id,
    ...(selectedScope ? { selectedScope } : {}),
    cleaning,
  });

  const capture = await sendPageCommand<PageCapture>(tab.id, {
    type: "page.capture",
    mode,
    ...(selectedScope ? { scope: selectedScope } : {}),
    cleaning,
  });
  if (capture.kind === "fallback") {
    return setState({
      status: "fallback",
      message: capture.reason,
      captureMode: mode,
      sourceUrl: capture.sourceUrl,
      title: capture.title,
      tabId: tab.id,
      ...(selectedScope ? { selectedScope } : {}),
      cleaning,
    });
  }

  const deps = dependencies(tab.id, capture.sourceUrl, duplicateAction, updateTargetId);
  const submissionKey = `${capture.sourceUrl}\n${capture.scope}\n${cleaning}\n${duplicateAction ?? "check"}\n${updateTargetId ?? ""}`;
  let pending = captureSubmissions.get(submissionKey);
  if (!pending) {
    pending = capture.kind === "selection"
      ? submitSelectionCapture(capture, deps)
      : submitArticleCapture(capture, deps);
    captureSubmissions.set(submissionKey, pending);
    void pending.finally(() => captureSubmissions.delete(submissionKey)).catch(() => undefined);
  }
  const submission = await pending;
  return stateFromSubmission(tab, capture, submission, cleaning);
}

async function captureFailure(tab: ActiveTab, error: unknown): Promise<never> {
  const previous = await getState(tab);
  const state = await setState({
    ...previous,
    status: "error",
    message: errorMessage(error),
    tabId: tab.id,
    sourceUrl: tab.url,
    title: tab.title,
  });
  throw new ScopedOperationError(state.message, state);
}

async function startCapture(
  mode: "article" | "selection" = "article",
  duplicateAction?: DuplicateAction,
  selectedScope?: string,
  cleaning: "default" | "deep" = "default",
): Promise<ExtensionState> {
  const tab = await activeTab();
  invalidateInspection(tab);
  const previous = await getState(tab);
  let updateTargetId: string | undefined;
  if (duplicateAction === "update") {
    if (previous.sourceUrl !== tab.url) {
      throw new Error("Reopen the original source page before retrying this capture.");
    }
    if (previous.captureMode !== mode) {
      throw new Error("The requested capture mode does not match the saved clip.");
    }
    if (!previous.clipId) {
      throw new Error("The saved clip is no longer available for update.");
    }
    updateTargetId = previous.clipId;
    cleaning = previous.cleaning ?? cleaning;
  }

  const requestKey = `${stateKey(tab)}\n${mode}\n${duplicateAction ?? "check"}\n${updateTargetId ?? ""}\n${selectedScope ?? ""}\n${cleaning}`;
  const existing = captureRequests.get(requestKey);
  if (existing) return existing;

  const pending = performCapture(tab, mode, duplicateAction, updateTargetId, selectedScope, cleaning)
    .catch((error) => captureFailure(tab, error));
  captureRequests.set(requestKey, pending);
  void pending.finally(() => captureRequests.delete(requestKey)).catch(() => undefined);
  return pending;
}

async function captureRegion(): Promise<ExtensionState> {
  const tab = await activeTab();
  invalidateInspection(tab);
  await setState({
    status: "capturing",
    message: "Choose a screen region. Cancelling keeps everything unchanged.",
    sourceUrl: tab.url,
    title: tab.title,
    tabId: tab.id,
  });
  try {
    const clip = await nativeRequest({
      op: "capture.region",
      sourceUrl: tab.url,
      title: tab.title || "Captured region",
    });
    const clipId = typeof clip.id === "string" && ID_PATTERN.test(clip.id) ? clip.id : undefined;
    return setState({
      status: "ready",
      message: "Region saved with its source page.",
      sourceUrl: tab.url,
      title: tab.title,
      tabId: tab.id,
      ...(clipId ? { clipId } : {}),
    });
  } catch (error) {
    return captureFailure(tab, error);
  }
}

async function safeClipOperation(op: "clip.open" | "clip.restore", id: string): Promise<ExtensionState> {
  if (!ID_PATTERN.test(id)) throw new Error("The clip ID is invalid.");
  const tab = await activeTab();
  const previous = await getState(tab);
  if (previous.clipId !== id) throw new Error("This saved clip does not belong to the current page.");
  await nativeRequest({ op, id });
  if (op === "clip.open") {
    return setState({ ...previous, message: "Opened the saved clip.", clipId: id });
  }
  return setState({
    ...previous,
    status: "ready",
    message: "Restored the clip from Trash.",
    clipId: id,
  });
}

chrome.runtime.onMessage.addListener((message: unknown, _sender, sendResponse) => {
  if ((message as { type?: unknown } | null)?.type === "status.changed") return false;
  const request = message as Partial<PopupRequest>;
  if (request.type === "settings.get") {
    nativeRequest({ op: "settings.get" })
      .then(async (settings) => {
        const language = settings.language;
        const resolvedLanguage = settings.resolvedLanguage;
        if ((language !== "system" && language !== "zh" && language !== "en")
          || (resolvedLanguage !== "zh" && resolvedLanguage !== "en")) {
          throw new Error("The native host returned invalid language settings.");
        }
        await chrome.action.setTitle({
          title: resolvedLanguage === "zh" ? "保存到 Omarchy Clipper" : "Save with Omarchy Clipper",
        }).catch(() => undefined);
        sendResponse({ ok: true, result: { language, resolvedLanguage } });
      })
      .catch(async (error) => {
        await chrome.action.setTitle({ title: "Save with Omarchy Clipper" }).catch(() => undefined);
        sendResponse({ ok: false, error: errorMessage(error) });
      });
    return true;
  }
  const operation = (async (): Promise<ExtensionState> => {
    if (request.type === "status.get") {
      const tab = await activeTab();
      return getState(tab);
    }
    if (request.type === "capture.inspect") return inspectActivePage();
    if (request.type === "capture.start") {
      const mode = request.mode === "selection" ? "selection" : "article";
      const action = request.duplicateAction === "update" || request.duplicateAction === "new"
        ? request.duplicateAction
        : undefined;
      const scope = typeof request.scope === "string" ? request.scope : undefined;
      const cleaning = request.cleaning === "deep" ? "deep" : "default";
      return startCapture(mode, action, scope, cleaning);
    }
    if (request.type === "capture.region") return captureRegion();
    if (request.type === "clip.open" && typeof request.id === "string") return safeClipOperation("clip.open", request.id);
    if (request.type === "clip.restore" && typeof request.id === "string") return safeClipOperation("clip.restore", request.id);
    throw new Error("Unsupported extension command.");
  })();

  operation
    .then((state) => sendResponse({ ok: true, result: state }))
    .catch(async (error) => {
      if (error instanceof ScopedOperationError) {
        sendResponse({ ok: false, error: error.message, result: error.state });
        return;
      }
      let tab: ActiveTab | undefined;
      try {
        tab = await activeTab();
      } catch {
        const state: ExtensionState = { status: "error", message: errorMessage(error) };
        sendResponse({ ok: false, error: state.message, result: state });
        return;
      }
      const previous = await getState(tab);
      const state = await setState({
        ...previous,
        status: "error",
        message: errorMessage(error),
        ...(tab ? { tabId: tab.id, sourceUrl: tab.url, title: tab.title } : {}),
      });
      sendResponse({ ok: false, error: state.message, result: state });
    });
  return true;
});

chrome.runtime.onInstalled.addListener(() => {
  void setState({ status: "idle", message: "Ready to capture this page." });
});
