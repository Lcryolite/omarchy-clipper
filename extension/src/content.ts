import {
  capturePage,
  finalizeArticleHtml,
  inspectPage,
  type CaptureMode,
  type CleaningMode,
} from "./page.js";

type ContentRequest =
  | { type: "page.inspect" }
  | { type: "page.capture"; mode: CaptureMode; scope?: string; cleaning?: CleaningMode }
  | { type: "page.finalize"; html: string; assetIds: Record<string, string> };

declare global {
  interface Window {
    __omarchyClipperContentReady?: boolean;
  }
}

if (!window.__omarchyClipperContentReady) {
  window.__omarchyClipperContentReady = true;
  chrome.runtime.onMessage.addListener((message: unknown, _sender, sendResponse) => {
    const request = message as Partial<ContentRequest>;
    try {
      if (request.type === "page.inspect") {
        sendResponse({ ok: true, result: inspectPage(document) });
        return false;
      }
      if (request.type === "page.capture" && (request.mode === "article" || request.mode === "selection")) {
        sendResponse({
          ok: true,
          result: capturePage(document, {
            mode: request.mode,
            ...(typeof request.scope === "string" ? { scope: request.scope } : {}),
            cleaning: request.cleaning === "deep" ? "deep" : "default",
          }),
        });
        return false;
      }
      if (request.type === "page.finalize" && typeof request.html === "string" && request.assetIds) {
        sendResponse({ ok: true, result: finalizeArticleHtml(document, request.html, request.assetIds) });
        return false;
      }
      sendResponse({ ok: false, error: "Unsupported page command." });
    } catch (error) {
      sendResponse({ ok: false, error: error instanceof Error ? error.message : "Page capture failed." });
    }
    return false;
  });
}
