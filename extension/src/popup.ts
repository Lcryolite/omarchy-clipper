type ExtensionState = {
  status: string;
  message: string;
  captureMode?: "article" | "selection";
  sourceUrl?: string;
  title?: string;
  tabId?: number;
  clipId?: string;
  missing?: Array<{
    url: string;
    reason: string;
    category?: "permission" | "transient" | "unsupported";
    retryable?: boolean;
  }>;
  permissionOrigins?: string[];
  detectedLabel?: string;
  scopes?: Array<{ value: string; label: string }>;
  selectedScope?: string;
  cleaningAvailable?: boolean;
  cleaning?: "default" | "deep";
  scopeNote?: string;
};

type Language = "zh" | "en";

const zh = {
  currentPage: "当前页面",
  readyToInspect: "可以检查此页面。",
  detectedContent: "检测到的内容",
  captureChoices: "捕获选项",
  captureScope: "捕获范围",
  cleaning: "清理",
  standard: "标准",
  deepClean: "深度清理",
  cleaningHelp: "深度清理还会移除识别到的赞助内容、推广和徽章。",
  captureActions: "捕获操作",
  clipActions: "剪藏操作",
  capture: "捕获",
  saveSelection: "保存选区",
  captureRegion: "截取区域",
  readableArticle: "可读文章",
  article: "文章",
  savedClip: "已保存的剪藏",
  existingClip: "已有剪藏",
  saving: "正在保存",
  captureUnavailable: "无法捕获",
  needsAttention: "需要处理",
  openClip: "打开剪藏",
  openExisting: "打开已有剪藏",
  update: "更新",
  newCapture: "新建捕获",
  restore: "恢复",
  retryFromPage: "从此页面重试",
  retry: "重试",
  readyToCapture: (label: string) => `检测到${label}。请查看捕获选项，准备好后保存。`,
} as const;

let language: Language = "en";

const statusElement = document.querySelector<HTMLElement>("#status")!;
const detailElement = document.querySelector<HTMLElement>("#detail")!;
const choicesElement = document.querySelector<HTMLElement>("#choices")!;
const detectedElement = document.querySelector<HTMLElement>("#detected-heading")!;
const stateLabelElement = document.querySelector<HTMLElement>("#state-label")!;
const optionsElement = document.querySelector<HTMLElement>("#capture-options")!;
const scopeFieldset = document.querySelector<HTMLElement>("#scope-fieldset")!;
const scopeOptions = document.querySelector<HTMLElement>("#scope-options")!;
const cleaningFieldset = document.querySelector<HTMLElement>("#cleaning-fieldset")!;
const articleButton = document.querySelector<HTMLButtonElement>("#capture-article")!;
const selectionButton = document.querySelector<HTMLButtonElement>("#capture-selection")!;
const regionButton = document.querySelector<HTMLButtonElement>("#capture-region")!;
let renderedState: ExtensionState | undefined;

async function request<T>(message: unknown): Promise<T> {
  const response = (await chrome.runtime.sendMessage(message)) as {
    ok?: boolean;
    result?: T;
    error?: string;
  };
  if (response?.result) return response.result;
  throw new Error(response?.error || "The extension did not respond.");
}

function translateKnownLabel(value: string | undefined): string | undefined {
  if (language !== "zh" || !value) return value;
  const labels: Record<string, string> = {
    "Readable article": zh.readableArticle,
    Article: zh.article,
    "Visible page index": "可见页面索引",
    "Page index": "页面索引",
    "Repository README": "仓库 README",
    "GitHub README": "GitHub README",
    "Loaded Issue discussion": "已加载的 Issue 讨论",
    "GitHub Issue discussion": "GitHub Issue 讨论",
    "Loaded pull request discussion": "已加载的拉取请求讨论",
    "GitHub pull request discussion": "GitHub 拉取请求讨论",
    "Discuz thread": "Discuz 主题",
    "Discuz replies on this page": "此页的 Discuz 回复",
    "Discuz original post": "Discuz 主帖",
    "Discuz current-page discussion": "Discuz 当前页讨论",
    "Discuz visible replies": "Discuz 可见回复",
    "Main post": "主帖",
    "Main post + current-page replies": "主帖和当前页回复",
    "Main post and current-page replies": "主帖和当前页回复",
    "Visible replies on this page": "此页可见回复",
    "Visible current-page replies": "当前页可见回复",
  };
  return labels[value] ?? value;
}

function translateMessage(state: ExtensionState): string {
  if (language !== "zh") return state.message;
  const detectedLabel = translateKnownLabel(state.detectedLabel);
  if (state.status === "ready-to-capture" && detectedLabel) return zh.readyToCapture(detectedLabel);
  const messages: Record<string, string> = {
    "Ready to capture this page.": "可以捕获此页面。",
    "Inspecting this page…": "正在检查此页面…",
    "A capture for this page is already in progress.": "此页面的捕获正在进行中。",
    "This page is already saved with missing images.": "此页面已保存，但缺少部分图片。",
    "This page is already saved. Open it or update it explicitly.": "此页面已保存。可以打开或明确更新它。",
    "A matching clip is in Trash. Restore it or create a new capture explicitly.": "废纸篓中有匹配的剪藏。可以恢复它或明确新建捕获。",
    "Saved for offline reading.": "已保存，可离线阅读。",
    "Saved incomplete because an image format or source is unsupported.": "已保存为不完整状态，因为图片格式或来源不受支持。",
    "The previous readable clip is safe. Reopen this source page to retry the incomplete update.": "之前的可读剪藏保持完好。请重新打开来源页面以重试未完成的更新。",
    "Saved with missing images. Reopen this source page to retry them.": "已保存，但缺少部分图片。请重新打开来源页面重试。",
    "Saving the selected content…": "正在保存所选内容…",
    "Capturing readable content…": "正在捕获可读内容…",
    "Choose a screen region. Cancelling keeps everything unchanged.": "请选择屏幕区域。取消不会更改任何内容。",
    "Region saved with its source page.": "区域已连同来源页面保存。",
    "Opened the saved clip.": "已打开保存的剪藏。",
    "Restored the clip from Trash.": "已从废纸篓恢复剪藏。",
    "No active webpage is available.": "没有可用的活动网页。",
    "This browser page is restricted. Open a regular HTTP or HTTPS page to capture it.": "此浏览器页面受限。请打开普通的 HTTP 或 HTTPS 页面进行捕获。",
    "The page did not return capture content.": "页面未返回捕获内容。",
    "Reopen the original source page before retrying this capture.": "重试捕获前，请重新打开原始来源页面。",
    "The requested capture mode does not match the saved clip.": "请求的捕获模式与已保存的剪藏不匹配。",
    "The saved clip is no longer available for update.": "已保存的剪藏已无法更新。",
    "The clip ID is invalid.": "剪藏 ID 无效。",
    "This saved clip does not belong to the current page.": "此已保存剪藏不属于当前页面。",
    "No readable article was found. Save a selection or capture a region instead.": "未找到可读文章。请改为保存选区或截取区域。",
    "The rendered repository README could not be identified.": "无法识别渲染后的仓库 README。",
    "No readable posts were found on the current forum page.": "当前论坛页面未找到可读帖子。",
    "Select visible page content before choosing Save selection.": "选择“保存选区”前，请先选择页面上的可见内容。",
    "The original post is not available on this forum page. Capture an explicit selection or choose the visible current-page replies scope.": "此论坛页面没有原始帖子。请明确选择内容，或选择当前页可见回复范围。",
    "Image access was not granted. The saved text remains available.": "未授予图片访问权限。已保存的文本仍然可用。",
    "Region selection was cancelled.": "已取消区域截取。",
  };
  const translated = messages[state.message];
  if (translated) return translated;
  const timeout = state.message.match(/did not respond within ([0-9]+) seconds/i);
  if (timeout) return `原生主机未在 ${timeout[1] ?? ""} 秒内响应。`;
  if (/native messaging host .*before replying|native host disconnected/i.test(state.message)) {
    return "原生消息主机在回复前断开了连接。";
  }
  if (state.status === "fallback") return "无法捕获此页面。请保存选区或截取区域。";
  if (state.status === "error") return "捕获失败。请重试。";
  return state.message;
}

function translateScopeNote(note: string | undefined): string {
  if (language !== "zh" || !note) return note ?? "";
  const notes: Record<string, string> = {
    "The original post is not available on this page; it will not be inferred from a reply.": "此页面没有原始帖子；不会根据回复推断主帖内容。",
    "Only the description and discussion already loaded on this page will be captured; file diffs and hidden entries are excluded.": "仅捕获此页面已加载的描述和讨论；不包括文件差异和隐藏条目。",
  };
  if (notes[note]) return notes[note];
  const scope = note.match(/^Scope: (.+)\. Cleaning: (Deep clean|Standard)\.$/);
  if (!scope) return note;
  return `范围：${translateKnownLabel(scope[1])}。清理：${scope[2] === "Deep clean" ? zh.deepClean : zh.standard}。`;
}

function stateLabel(status: string): string {
  if (status === "ready" || status === "incomplete") return language === "zh" ? zh.savedClip : "Saved clip";
  if (status === "duplicate-active" || status === "duplicate-trash") return language === "zh" ? zh.existingClip : "Existing clip";
  if (status === "capturing") return language === "zh" ? zh.saving : "Saving";
  if (status === "fallback") return language === "zh" ? zh.captureUnavailable : "Capture unavailable";
  if (status === "error") return language === "zh" ? zh.needsAttention : "Needs attention";
  return language === "zh" ? zh.detectedContent : "Detected content";
}

function applyLanguage(nextLanguage: Language): void {
  language = nextLanguage;
  document.documentElement.lang = language === "zh" ? "zh-CN" : "en";
  if (language !== "zh") return;
  document.querySelector<HTMLElement>("#header-context")!.textContent = zh.currentPage;
  stateLabelElement.textContent = zh.detectedContent;
  detectedElement.textContent = zh.currentPage;
  statusElement.textContent = zh.readyToInspect;
  document.querySelector<HTMLElement>("#capture-options")!.setAttribute("aria-label", zh.captureChoices);
  document.querySelector<HTMLElement>("#scope-legend")!.textContent = zh.captureScope;
  document.querySelector<HTMLElement>("#cleaning-legend")!.textContent = zh.cleaning;
  document.querySelector<HTMLElement>("#cleaning-standard-label")!.textContent = zh.standard;
  document.querySelector<HTMLElement>("#cleaning-deep-label")!.textContent = zh.deepClean;
  document.querySelector<HTMLElement>("#cleaning-help")!.textContent = zh.cleaningHelp;
  document.querySelector<HTMLElement>(".actions")!.setAttribute("aria-label", zh.captureActions);
  choicesElement.setAttribute("aria-label", zh.clipActions);
  articleButton.textContent = zh.capture;
  selectionButton.textContent = zh.saveSelection;
  regionButton.textContent = zh.captureRegion;
}

async function loadLanguage(): Promise<Language> {
  try {
    const settings = await request<{ resolvedLanguage?: unknown }>({ type: "settings.get" });
    return settings.resolvedLanguage === "zh" ? "zh" : "en";
  } catch {
    // This is a per-open resilience fallback, not an extension-owned preference.
    return "en";
  }
}

function choice(label: string, action: () => Promise<void>): HTMLButtonElement {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "choice";
  button.textContent = label;
  button.addEventListener("click", () => void action());
  return button;
}

function setBusy(busy: boolean): void {
  articleButton.disabled = busy;
  selectionButton.disabled = busy;
  regionButton.disabled = busy;
  for (const button of choicesElement.querySelectorAll<HTMLButtonElement>("button")) button.disabled = busy;
  for (const input of optionsElement.querySelectorAll<HTMLInputElement>("input")) input.disabled = busy;
}

function isUnsupported(item: NonNullable<ExtensionState["missing"]>[number]): boolean {
  return item.category === "unsupported" || item.retryable === false || /not a supported image type/i.test(item.reason);
}

function render(state: ExtensionState): void {
  renderedState = state;
  document.body.dataset.status = state.status;
  stateLabelElement.textContent = stateLabel(state.status);
  const detectedLabel = translateKnownLabel(state.detectedLabel);
  statusElement.textContent = translateMessage(state);
  detectedElement.textContent = detectedLabel || state.title || (language === "zh" ? zh.currentPage : "Current page");
  const missing = state.missing ?? [];
  const unsupported = missing.filter(isUnsupported);
  const formatLimited = missing.filter((item) => /not a supported image type/i.test(item.reason));
  if (missing.length === 0) {
    detailElement.textContent = translateScopeNote(state.scopeNote);
  } else if (formatLimited.length === missing.length) {
    detailElement.textContent = language === "zh"
      ? `${missing.length} 张图片的格式不受支持，仍然不可用。`
      : `${missing.length} image${missing.length === 1 ? " has" : "s have"} an unsupported format and remain${missing.length === 1 ? "s" : ""} unavailable.`;
  } else if (unsupported.length === missing.length) {
    detailElement.textContent = language === "zh"
      ? `${missing.length} 张图片无法从当前来源捕获。`
      : `${missing.length} image${missing.length === 1 ? "" : "s"} cannot be captured from ${missing.length === 1 ? "its" : "their"} current source.`;
  } else {
    detailElement.textContent = language === "zh"
      ? `${missing.length} 张图片尚未保存。`
      : `${missing.length} image${missing.length === 1 ? "" : "s"} could not be saved yet.`;
  }
  choicesElement.replaceChildren();
  scopeOptions.replaceChildren();
  const captureReady = state.status === "ready-to-capture";
  optionsElement.hidden = !captureReady;
  scopeFieldset.hidden = !captureReady || (state.scopes?.length ?? 0) === 0;
  cleaningFieldset.hidden = !captureReady || state.cleaningAvailable !== true;
  if (captureReady) {
    for (const scope of state.scopes ?? []) {
      const label = document.createElement("label");
      const input = document.createElement("input");
      input.type = "radio";
      input.name = "scope";
      input.value = scope.value;
      input.checked = scope.value === state.selectedScope;
      label.append(input, translateKnownLabel(scope.label) ?? scope.label);
      scopeOptions.append(label);
    }
    const cleaning = state.cleaning ?? "default";
    for (const input of cleaningFieldset.querySelectorAll<HTMLInputElement>("input[name='cleaning']")) {
      input.checked = input.value === cleaning;
    }
  }
  setBusy(state.status === "capturing" || state.status === "inspecting");
  articleButton.hidden = !captureReady;

  if ((state.status === "duplicate-active" || state.status === "ready") && state.clipId) {
    choicesElement.append(choice(state.status === "ready"
      ? language === "zh" ? zh.openClip : "Open clip"
      : language === "zh" ? zh.openExisting : "Open existing", async () =>
      run({ type: "clip.open", id: state.clipId })));
    if (state.captureMode) {
      choicesElement.append(choice(language === "zh" ? zh.update : "Update", async () => run({
        type: "capture.start",
        mode: state.captureMode,
        duplicateAction: "update",
        scope: state.selectedScope,
        cleaning: state.cleaning,
      })));
    }
    if (state.status === "ready") {
      choicesElement.append(choice(language === "zh" ? zh.newCapture : "New capture", async () => run({ type: "capture.inspect" })));
    }
  } else if (state.status === "incomplete" && state.clipId) {
    choicesElement.append(choice(language === "zh" ? zh.openExisting : "Open existing", async () => run({ type: "clip.open", id: state.clipId })));
  } else if (state.status === "duplicate-trash" && state.clipId) {
    choicesElement.append(
      choice(language === "zh" ? zh.restore : "Restore", async () => run({ type: "clip.restore", id: state.clipId })),
      choice(language === "zh" ? zh.newCapture : "New capture", async () => run({
        type: "capture.start",
        mode: state.captureMode,
        duplicateAction: "new",
        scope: state.selectedScope,
        cleaning: state.cleaning,
      })),
    );
  }

  const origins = state.permissionOrigins ?? [];
  if (state.status === "incomplete" && origins.length > 0) {
    for (const origin of origins) {
      const hostname = new URL(origin.replace(/\*$/, "")).hostname;
      choicesElement.append(choice(language === "zh" ? `允许来自 ${hostname} 的图片` : `Allow images from ${hostname}`, async () => {
        const granted = await chrome.permissions.request({ origins: [origin] });
        if (!granted) {
          render({ ...state, message: "Image access was not granted. The saved text remains available." });
          return;
        }
        await run({
          type: "capture.start",
          mode: state.captureMode,
          duplicateAction: "update",
          scope: state.selectedScope,
          cleaning: state.cleaning,
        });
      }));
    }
  } else if (state.status === "incomplete" && missing.some((item) => item.retryable !== false && !isUnsupported(item))) {
    choicesElement.append(choice(language === "zh" ? zh.retryFromPage : "Retry from this page", async () => {
      await run({
        type: "capture.start",
        mode: state.captureMode,
        duplicateAction: "update",
        scope: state.selectedScope,
        cleaning: state.cleaning,
      });
    }));
  } else if (state.status === "error") {
    choicesElement.append(choice(language === "zh" ? zh.retry : "Retry", async () => {
      if (state.captureMode) {
        await run({
          type: "capture.start",
          mode: state.captureMode,
          scope: state.selectedScope,
          cleaning: state.cleaning,
        });
      } else {
        await run({ type: "capture.inspect" });
      }
    }));
  }
}

async function run(message: unknown): Promise<void> {
  try {
    setBusy(true);
    render(await request<ExtensionState>(message));
  } catch (error) {
    render({ status: "error", message: error instanceof Error ? error.message : "Capture failed." });
  }
}

articleButton.addEventListener("click", () => {
  const scope = document.querySelector<HTMLInputElement>("input[name='scope']:checked")?.value
    ?? renderedState?.selectedScope;
  const cleaning = document.querySelector<HTMLInputElement>("input[name='cleaning']:checked")?.value === "deep"
    ? "deep"
    : "default";
  void run({ type: "capture.start", scope, cleaning });
});
selectionButton.addEventListener("click", () => void run({ type: "capture.start", mode: "selection" }));
regionButton.addEventListener("click", () => void run({ type: "capture.region" }));
chrome.runtime.onMessage.addListener((message: unknown) => {
  const update = message as { type?: string; state?: ExtensionState };
  if (update.type === "status.changed" && update.state) {
    void request<ExtensionState>({ type: "status.get" }).then(render).catch(() => undefined);
  }
});

void (async () => {
  applyLanguage(await loadLanguage());
  const current = await request<ExtensionState>({ type: "status.get" });
  render(current);
  if (current.status === "idle") {
    await run({ type: "capture.inspect" });
  }
})();
