import { Readability } from "@mozilla/readability";
import createDOMPurify, { type WindowLike } from "dompurify";

export type CapturedImage = {
  key: string;
  url: string;
  alt: string;
  name: string;
  failure?: {
    category: "unresolved";
    reason: string;
  };
};

export type ArticleCapture = {
  kind: "article";
  title: string;
  sourceUrl: string;
  scope:
    | "article"
    | "github-readme"
    | "github-issue-loaded"
    | "github-pr-loaded"
    | "discuz-main-post"
    | "discuz-current-page"
    | "discuz-current-page-replies"
    | "forum-current-page"
    | "page-index";
  html: string;
  text: string;
  images: CapturedImage[];
};

export type SelectionCapture = {
  kind: "selection";
  title: string;
  sourceUrl: string;
  scope: `selection-${string}`;
  html: string;
  text: string;
  images: CapturedImage[];
};

export type FallbackCapture = {
  kind: "fallback";
  title: string;
  sourceUrl: string;
  reason: string;
};

export type PageCapture = ArticleCapture | SelectionCapture | FallbackCapture;
export type CaptureMode = "article" | "selection";
export type CleaningMode = "default" | "deep";
export type CaptureOptions = {
  mode?: CaptureMode;
  scope?: string;
  cleaning?: CleaningMode;
};

export type PageInspection =
  | {
      kind: "supported";
      title: string;
      sourceUrl: string;
      detected: "article" | "github-readme" | "github-issue" | "github-pr" | "discuz" | "page-index";
      label: string;
      scopes: Array<{ value: string; label: string }>;
      defaultScope: string;
      cleaningAvailable: boolean;
      note?: string;
    }
  | FallbackCapture;

const FORBIDDEN_TAGS = [
  "script",
  "style",
  "iframe",
  "object",
  "embed",
  "form",
  "input",
  "button",
  "textarea",
  "select",
  "option",
  "video",
  "audio",
  "canvas",
  "svg",
  "math",
];

const FORBIDDEN_ATTRIBUTES = [
  "style",
  "srcset",
  "sizes",
  "autoplay",
  "formaction",
];

const SAFE_DATA_ATTRIBUTES = [
  "data-clipper-image",
  "data-clipper-source",
  "data-clipper-scope",
  "data-clipper-cleaning",
  "data-clipper-post",
  "data-clipper-role",
  "data-clipper-author",
  "data-clipper-scope-notice",
  "data-clipper-missing-image",
];

function compactText(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

function nodePath(node: Node): string {
  const parts: number[] = [];
  let current: Node | null = node;
  while (current?.parentNode) {
    parts.push(Array.prototype.indexOf.call(current.parentNode.childNodes, current) as number);
    current = current.parentNode;
  }
  return parts.reverse().join(".");
}

function selectionScope(range: Range, html: string): `selection-${string}` {
  const identity = [
    nodePath(range.startContainer),
    range.startOffset,
    nodePath(range.endContainer),
    range.endOffset,
    html,
  ].join(":");
  let hash = 0x811c9dc5;
  for (let index = 0; index < identity.length; index += 1) {
    hash ^= identity.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return `selection-${(hash >>> 0).toString(36)}`;
}

function pageTitle(document: Document): string {
  return compactText(document.title) || document.location.hostname || "Untitled page";
}

function absoluteHttpUrl(value: string, base: string): string | null {
  if (!value.trim()) return null;
  try {
    const url = new URL(value, base);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}

const LAZY_IMAGE_ATTRIBUTES = [
  "data-src",
  "data-lazy-src",
  "data-original",
  "data-url",
  "data-image",
  "data-cfsrc",
] as const;

function srcsetCandidates(value: string): string[] {
  return value.split(",").map((candidate) => candidate.trim().split(/\s+/, 1)[0] ?? "").filter(Boolean).reverse();
}

function isPlaceholderImageUrl(url: string): boolean {
  try {
    return /(?:^|[._/-])(blank|loading|placeholder|spacer|transparent|pixel)(?:[._/-]|$)/i.test(
      new URL(url).pathname,
    );
  } catch {
    return true;
  }
}

function imageUrl(image: HTMLImageElement, base: string): string | null {
  const rawCandidates: string[] = [];
  for (const attribute of LAZY_IMAGE_ATTRIBUTES) {
    const value = image.getAttribute(attribute);
    if (value) rawCandidates.push(value);
  }
  for (const attribute of ["data-srcset", "srcset"] as const) {
    const value = image.getAttribute(attribute);
    if (value) rawCandidates.push(...srcsetCandidates(value));
  }
  for (const source of image.parentElement?.tagName === "PICTURE"
    ? image.parentElement.querySelectorAll<HTMLSourceElement>("source")
    : []) {
    for (const attribute of ["data-srcset", "srcset"] as const) {
      const value = source.getAttribute(attribute);
      if (value) rawCandidates.push(...srcsetCandidates(value));
    }
  }
  rawCandidates.push(image.currentSrc, image.getAttribute("src") ?? "");

  const candidates = rawCandidates
    .map((candidate) => absoluteHttpUrl(candidate, base))
    .filter((candidate): candidate is string => Boolean(candidate));
  return candidates.find((candidate) => !isPlaceholderImageUrl(candidate)) ?? null;
}

const INTERFACE_IMAGE_TOKEN = /(?:^|[\s_-])(action|actions|avatar|button|control|controls|form|icon|menu|modal|popover|toolbar|tooltip)(?:$|[\s_-])/i;

function isInterfaceImage(image: HTMLImageElement): boolean {
  if (image.closest("figure, picture")) return false;
  let current: HTMLElement | null = image;
  while (current && !current.matches("article, main, [role='article']")) {
    const semanticIdentifier = `${current.id} ${current.getAttribute("class") ?? ""}`;
    if (INTERFACE_IMAGE_TOKEN.test(semanticIdentifier)) return true;
    if (current.matches("[role='button'], [role='menu'], [role='menubar'], [role='toolbar'], [role='tooltip']")) {
      return true;
    }
    current = current.parentElement;
  }
  return false;
}

function isMeaningfulImage(image: HTMLImageElement, url: string | null): boolean {
  if (image.getAttribute("aria-hidden") === "true" || image.getAttribute("role") === "presentation") return false;
  if (image.closest("nav, aside, footer, [role='navigation'], [role='banner'], [role='complementary']")) return false;
  const width = Number.parseInt(image.getAttribute("width") ?? "", 10);
  const height = Number.parseInt(image.getAttribute("height") ?? "", 10);
  if ((Number.isFinite(width) && width <= 2) || (Number.isFinite(height) && height <= 2)) return false;
  return Boolean(url || compactText(image.alt) || image.closest("figure, picture"));
}

function imageName(url: string): string {
  try {
    const name = new URL(url).pathname.split("/").filter(Boolean).at(-1);
    return name ? decodeURIComponent(name).slice(0, 160) : "image";
  } catch {
    return "image";
  }
}

function isHiddenElement(element: HTMLElement): boolean {
  if (element.hidden || element.hasAttribute("inert") || element.getAttribute("aria-hidden") === "true") {
    return true;
  }
  const style = element.ownerDocument.defaultView?.getComputedStyle(element) ?? element.style;
  if (style.opacity === "0") return true;
  if (style.display === "none" || style.visibility === "hidden" || style.contentVisibility === "hidden") {
    return true;
  }
  if ((style.position === "fixed" || style.position === "absolute")) {
    const left = Number.parseFloat(style.left);
    const top = Number.parseFloat(style.top);
    if ((Number.isFinite(left) && left <= -1000) || (Number.isFinite(top) && top <= -1000)) return true;
  }
  return false;
}

function descendantElements(root: ParentNode): HTMLElement[] {
  const descendants = [...root.querySelectorAll<HTMLElement>("*")];
  return root.nodeType === 1 ? [root as HTMLElement, ...descendants] : descendants;
}

const NON_CONTENT_SELECTOR = "form, dialog, template, noscript, [role='dialog'], [aria-modal='true'], [aria-label='advertisement' i], [aria-label='advertisements' i]";
const SITE_CLUTTER_SELECTOR = [
  ".ad",
  ".ads",
  ".advert",
  ".advertisement",
  "[data-ad]",
  "[data-advertisement]",
  "[aria-label='advertisement' i]",
  "[aria-label='advertisements' i]",
].join(",");
const DEEP_CLEAN_SELECTOR = [
  ".sponsor",
  ".sponsorship",
  ".promotion",
  ".promotional",
  ".promo",
  ".badge",
  "[data-sponsor]",
  "[data-promotion]",
  "[data-badge]",
].join(",");

function cleanSiteContent(root: ParentNode, cleaning: CleaningMode): void {
  for (const element of root.querySelectorAll<HTMLElement>(SITE_CLUTTER_SELECTOR)) element.remove();
  if (cleaning === "deep") {
    for (const element of root.querySelectorAll<HTMLElement>(DEEP_CLEAN_SELECTOR)) element.remove();
  }
}

function isVisiblePageContent(element: HTMLElement): boolean {
  let current: HTMLElement | null = element;
  while (current) {
    if (isHiddenElement(current)) return false;
    if (current.matches(NON_CONTENT_SELECTOR)) return false;
    current = current.parentElement;
  }
  return true;
}

function pruneNonContent(root: ParentNode, visibilitySource?: ParentNode): void {
  if (visibilitySource) {
    const targets = descendantElements(root);
    const sources = descendantElements(visibilitySource);
    for (let index = 0; index < Math.min(targets.length, sources.length); index += 1) {
      const target = targets[index];
      const source = sources[index];
      if (target && source && isHiddenElement(source)) target.remove();
    }
  }
  for (const element of root.querySelectorAll<HTMLElement>(NON_CONTENT_SELECTOR)) {
    element.remove();
  }
  for (const element of root.querySelectorAll<HTMLElement>("[hidden], [inert], [aria-hidden='true'], [style]")) {
    if (isHiddenElement(element)) element.remove();
  }
  for (const image of root.querySelectorAll<HTMLImageElement>("img")) {
    if (isInterfaceImage(image)) image.remove();
  }
}

function prepareFragment(
  document: Document,
  fragment: DocumentFragment | Element,
): CapturedImage[] {
  const images: CapturedImage[] = [];
  const seen = new Map<string, string>();
  const root = fragment as ParentNode;

  for (const anchor of root.querySelectorAll<HTMLAnchorElement>("a[href]")) {
    const href = absoluteHttpUrl(anchor.getAttribute("href") ?? "", document.baseURI);
    if (href) {
      anchor.setAttribute("href", href);
    } else {
      anchor.removeAttribute("href");
    }
    anchor.removeAttribute("target");
    anchor.removeAttribute("rel");
  }

  for (const image of root.querySelectorAll<HTMLImageElement>("img")) {
    const url = imageUrl(image, document.baseURI);
    image.removeAttribute("src");
    image.removeAttribute("srcset");
    image.removeAttribute("sizes");
    image.removeAttribute("data-srcset");
    for (const attribute of LAZY_IMAGE_ATTRIBUTES) image.removeAttribute(attribute);
    if (!isMeaningfulImage(image, url)) {
      image.remove();
      continue;
    }

    let key = url ? seen.get(url) : undefined;
    if (!key) {
      key = `image-${images.length + 1}`;
      if (url) seen.set(url, key);
      const captured: CapturedImage = {
        key,
        url: url ?? "",
        alt: compactText(image.alt),
        name: url ? imageName(url) : "image",
      };
      if (!url) {
        captured.failure = {
          category: "unresolved",
          reason: "No usable image source was found in the captured scope.",
        };
      }
      images.push(captured);
    }
    image.setAttribute("data-clipper-image", key);
    if (url) image.setAttribute("data-clipper-source", url);
    image.setAttribute("loading", "lazy");
  }

  return images;
}

function sanitize(document: Document, html: string): string {
  const window = document.defaultView;
  if (!window) throw new Error("The page has no browser window.");
  const purifier = createDOMPurify(window as unknown as WindowLike);
  return purifier.sanitize(html, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: FORBIDDEN_TAGS,
    FORBID_ATTR: FORBIDDEN_ATTRIBUTES,
    ALLOW_DATA_ATTR: false,
    ADD_ATTR: SAFE_DATA_ATTRIBUTES,
  });
}

function textFromHtml(document: Document, html: string): string {
  const textDocument = document.implementation.createHTMLDocument("");
  textDocument.body.innerHTML = html;
  for (const block of textDocument.body.querySelectorAll(
    "address, article, aside, blockquote, br, dd, div, dl, dt, figcaption, figure, footer, h1, h2, h3, h4, h5, h6, header, hr, li, main, nav, p, pre, section, table, td, th, tr",
  )) {
    block.append(" ");
  }
  return compactText(textDocument.body.textContent ?? "");
}

export function finalizeArticleHtml(
  document: Document,
  html: string,
  assetIds: Readonly<Record<string, string>>,
): string {
  const container = document.createElement("div");
  container.innerHTML = html;

  for (const image of container.querySelectorAll<HTMLImageElement>("img[data-clipper-image]")) {
    const key = image.dataset.clipperImage ?? "";
    const assetId = assetIds[key];
    image.removeAttribute("data-clipper-image");
    image.removeAttribute("data-clipper-source");
    if (assetId && /^[A-Za-z0-9._-]{1,128}$/.test(assetId)) {
      image.setAttribute("src", `clipper://asset/${assetId}`);
      continue;
    }

    const placeholder = document.createElement("span");
    placeholder.setAttribute("data-clipper-missing-image", "1");
    placeholder.textContent = image.alt ? `[Missing image: ${image.alt}]` : "[Missing image]";
    image.replaceWith(placeholder);
  }

  const window = document.defaultView;
  if (!window) throw new Error("The page has no browser window.");
  const purifier = createDOMPurify(window as unknown as WindowLike);
  return purifier.sanitize(container.innerHTML, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: FORBIDDEN_TAGS,
    FORBID_ATTR: FORBIDDEN_ATTRIBUTES,
    ALLOW_DATA_ATTR: false,
    ADD_ATTR: SAFE_DATA_ATTRIBUTES,
    ALLOWED_URI_REGEXP: /^(?:(?:https?|mailto):|clipper:\/\/asset\/[A-Za-z0-9._-]{1,128}$)/i,
  });
}

function resultFromElement(
  document: Document,
  element: Element | DocumentFragment,
  scope: ArticleCapture["scope"],
  title = pageTitle(document),
): ArticleCapture {
  const container = document.createElement("div");
  const cloned = element.cloneNode(true) as DocumentFragment | Element;
  pruneNonContent(cloned, element);
  container.append(cloned);
  const images = prepareFragment(document, container);
  const html = sanitize(document, container.innerHTML);

  return {
    kind: "article",
    title,
    sourceUrl: document.location.href,
    scope,
    html,
    text: textFromHtml(document, html),
    images,
  };
}

function genericCaptureScope(
  document: Document,
  content: Element,
  scopeName: "article" | "page-index",
  cleaning: CleaningMode,
): HTMLElement {
  const scope = document.createElement("section");
  scope.setAttribute("data-clipper-scope", scopeName);
  scope.setAttribute("data-clipper-cleaning", cleaning);
  const notice = document.createElement("aside");
  notice.setAttribute("data-clipper-scope-notice", "1");
  notice.textContent = `${scopeName === "article" ? "Captured readable article" : "Captured visible page index"}. ${cleaning === "deep" ? "Deep" : "Standard"} cleaning was applied.`;
  scope.append(notice, content);
  return scope;
}

function genericResultFromElement(
  document: Document,
  element: Element,
  scopeName: "article" | "page-index",
  cleaning: CleaningMode,
  title = pageTitle(document),
): ArticleCapture {
  const captured = element.cloneNode(true) as HTMLElement;
  pruneNonContent(captured, element);
  cleanSiteContent(captured, cleaning);
  return resultFromElement(document, genericCaptureScope(document, captured, scopeName, cleaning), scopeName, title);
}

function capturePageIndex(document: Document, cleaning: CleaningMode): ArticleCapture | null {
  const groups = new Map<Element, HTMLElement[]>();
  const meaningfulArticles: HTMLElement[] = [];
  const eligibleEntries: HTMLElement[] = [];
  for (const source of document.querySelectorAll<HTMLElement>("article, [role='listitem']")) {
    if (source.parentElement?.closest("article, [role='listitem']")) continue;
    if (!isVisiblePageContent(source)) continue;
    if (source.closest("nav, aside, footer, form, dialog, [role='navigation'], [role='complementary']")) continue;
    const candidate = source.cloneNode(true) as HTMLElement;
    pruneNonContent(candidate, source);
    cleanSiteContent(candidate, cleaning);
    const linkedHeading = [...candidate.querySelectorAll<HTMLAnchorElement>("h1 a[href], h2 a[href], h3 a[href]")]
      .some((anchor) => {
        const target = absoluteHttpUrl(anchor.getAttribute("href") ?? "", document.baseURI);
        return target !== null && target.split("#", 1)[0] !== document.location.href.split("#", 1)[0];
      });
    if (source.matches("article") && !linkedHeading && hasMeaningfulContent(candidate)) meaningfulArticles.push(source);
    const text = compactText(candidate.textContent ?? "");
    if (text.length < 20 || !candidate.querySelector("a[href], h1, h2, h3")) continue;
    const parent = source.parentElement;
    if (!parent) continue;
    eligibleEntries.push(source);
    const peers = groups.get(parent) ?? [];
    peers.push(source);
    groups.set(parent, peers);
  }

  const peerGroups = [...groups.values()].filter((peers) => peers.length >= 2);
  const main = [...document.querySelectorAll<HTMLElement>("main, [role='main']")]
    .find((candidate) => isVisiblePageContent(candidate));
  const selectedGroups = main
    ? peerGroups.filter((peers) => peers.every((entry) => main.contains(entry)))
    : peerGroups;
  if (selectedGroups.length === 0) return null;

  const groupedEntries = new Set(selectedGroups.flat());
  const standaloneArticles = meaningfulArticles.filter((entry) =>
    (!main || main.contains(entry)) && !groupedEntries.has(entry));
  if (standaloneArticles.length === 1)
    return genericResultFromElement(document, standaloneArticles[0]!, "article", cleaning);
  if (standaloneArticles.length > 1) return null;
  const entries = eligibleEntries.filter((entry) => !main || main.contains(entry));

  const scope = document.createElement("section");
  for (const source of entries) {
    const candidate = source.cloneNode(true) as HTMLElement;
    pruneNonContent(candidate, source);
    cleanSiteContent(candidate, cleaning);
    scope.append(candidate);
  }
  return resultFromElement(
    document,
    genericCaptureScope(document, scope, "page-index", cleaning),
    "page-index",
  );
}

function captureGitHubReadme(document: Document, cleaning: CleaningMode = "default"): ArticleCapture | null {
  const pathParts = document.location.pathname.split("/").filter(Boolean);
  if (pathParts.length !== 2) return null;
  const readme = document.querySelector<HTMLElement>(
    "#readme article.markdown-body, [data-testid='readme'] article, article.markdown-body[itemprop='text']",
  );
  if (!readme || compactText(readme.textContent ?? "").length < 10) return null;
  const captured = readme.cloneNode(true) as HTMLElement;
  cleanSiteContent(captured, cleaning);
  const scope = document.createElement("section");
  scope.setAttribute("data-clipper-scope", "github-readme");
  scope.setAttribute("data-clipper-cleaning", cleaning);
  const notice = document.createElement("aside");
  notice.setAttribute("data-clipper-scope-notice", "1");
  notice.textContent = `Captured the repository README. ${cleaning === "deep" ? "Deep" : "Standard"} cleaning was applied.`;
  scope.append(notice, captured);
  return resultFromElement(document, scope, "github-readme");
}

function githubEntry(
  document: Document,
  source: HTMLElement,
  role: "original" | "reply",
  cleaning: CleaningMode,
): HTMLElement | null {
  const body = source.matches(".markdown-body, .comment-body")
    ? source
    : source.querySelector<HTMLElement>(".comment-body, .markdown-body");
  if (!body || !compactText(body.textContent ?? "")) return null;
  const capturedBody = body.cloneNode(true) as HTMLElement;
  cleanSiteContent(capturedBody, cleaning);
  const entry = document.createElement("article");
  entry.setAttribute("data-clipper-post", "1");
  entry.setAttribute("data-clipper-role", role);
  const header = document.createElement("header");
  const author = source.querySelector<HTMLElement>(
    ".comment-header .author, [data-testid='comment-header'] .author, [data-hovercard-type='user'], .author",
  );
  if (author) {
    const strong = document.createElement("strong");
    strong.setAttribute("data-clipper-author", "1");
    strong.textContent = compactText(author.textContent ?? "");
    header.append(strong);
  }
  const sourceTime = source.querySelector<HTMLElement>("relative-time[datetime], time[datetime], relative-time, time");
  if (sourceTime) {
    const time = document.createElement("time");
    const datetime = sourceTime.getAttribute("datetime");
    if (datetime) time.setAttribute("datetime", datetime);
    time.textContent = compactText(sourceTime.textContent ?? "") || datetime || "";
    header.append(" ", time);
  }
  entry.append(header, capturedBody);
  return entry;
}

function captureGitHubDiscussion(document: Document, cleaning: CleaningMode): ArticleCapture | null {
  const parts = document.location.pathname.split("/").filter(Boolean);
  const type = parts[2];
  if (parts.length < 4 || (type !== "issues" && type !== "pull")) return null;
  const description = document.querySelector<HTMLElement>(
    "[data-testid='issue-body'], .js-issue-body, main > article.markdown-body",
  );
  if (!description) return null;
  const scopeName = type === "pull" ? "github-pr-loaded" : "github-issue-loaded";
  const scope = document.createElement("section");
  scope.setAttribute("data-clipper-scope", scopeName);
  scope.setAttribute("data-clipper-cleaning", cleaning);
  const notice = document.createElement("aside");
  notice.setAttribute("data-clipper-scope-notice", "1");
  const omitted = Boolean(document.querySelector(
    "[data-testid='deferred-content'], [data-clipper-unloaded-discussion], .ajax-pagination-form, .js-discussion-more",
  ));
  notice.textContent = omitted
    ? `Captured the description and loaded discussion only. More discussion is present but not loaded. ${cleaning === "deep" ? "Deep" : "Standard"} cleaning was applied.`
    : `Captured the description and loaded discussion only; hidden or unloaded entries are not included. ${cleaning === "deep" ? "Deep" : "Standard"} cleaning was applied.`;
  scope.append(notice);
  const original = githubEntry(document, description, "original", cleaning);
  if (!original) return null;
  scope.append(original);
  const seenBodies = new Set<Element>([description.querySelector(".comment-body, .markdown-body") ?? description]);
  for (const source of document.querySelectorAll<HTMLElement>(
    "[data-testid='issue-comment'], .timeline-comment, .js-timeline-item",
  )) {
    if (source === description || description.contains(source)) continue;
    const sourceBody = source.matches(".comment-body, .markdown-body")
      ? source
      : source.querySelector<HTMLElement>(".comment-body, .markdown-body");
    if (!sourceBody || seenBodies.has(sourceBody)) continue;
    seenBodies.add(sourceBody);
    const reply = githubEntry(document, source, "reply", cleaning);
    if (reply) scope.append(reply);
  }
  return resultFromElement(document, scope, scopeName);
}

function discuzOriginalPost(sourcePosts: readonly HTMLElement[]): HTMLElement | null {
  for (const sourcePost of sourcePosts) {
    const wrapper = sourcePost.closest<HTMLElement>("[id^='post_']") ?? sourcePost;
    if (
      wrapper.matches("[data-original-post], .first-post, [data-floor='1'], [data-post-number='1']") ||
      wrapper.querySelector("[data-original-post], .first-post, [data-floor='1'], [data-post-number='1']")
    ) {
      return sourcePost;
    }
    const floor = sourcePost.querySelector<HTMLElement>(
      ".pi strong a, .post-number, .floor, [data-testid='post-floor']",
    );
    if (/^\s*#?1\s*(?:#|楼|f(?:loor)?)/i.test(compactText(floor?.textContent ?? ""))) {
      return sourcePost;
    }
  }
  return null;
}

function discuzVisiblePosts(document: Document): HTMLElement[] {
  return [...document.querySelectorAll<HTMLElement>("#postlist table[id^='pid']")]
    .filter((post) => isVisiblePageContent(post));
}

function captureDiscuzPage(
  document: Document,
  requestedScope?: string,
  cleaning: CleaningMode = "default",
): ArticleCapture | FallbackCapture | null {
  const sourcePosts = discuzVisiblePosts(document);
  if (sourcePosts.length === 0) return null;

  const originalPost = discuzOriginalPost(sourcePosts);
  const originalAvailable = originalPost !== null;
  const includeCurrentPage = requestedScope === "discuz-current-page";
  const repliesOnly = requestedScope === "discuz-current-page-replies";
  if (!originalAvailable && !repliesOnly) {
    return {
      kind: "fallback",
      title: pageTitle(document),
      sourceUrl: document.location.href,
      reason: "The original post is not available on this forum page. Capture an explicit selection or choose the visible current-page replies scope.",
    };
  }

  const posts = document.createElement("section");
  const scope = repliesOnly
    ? "discuz-current-page-replies"
    : includeCurrentPage
      ? "discuz-current-page"
      : "discuz-main-post";
  posts.setAttribute("data-clipper-scope", scope);
  posts.setAttribute("data-clipper-cleaning", cleaning);
  const notice = document.createElement("aside");
  notice.setAttribute("data-clipper-scope-notice", "1");
  notice.textContent = repliesOnly
    ? `Captured visible replies from this page only; the original post was not available. ${cleaning === "deep" ? "Deep" : "Standard"} cleaning was applied.`
    : includeCurrentPage
      ? `Captured the original post and replies visible on this page only. ${cleaning === "deep" ? "Deep" : "Standard"} cleaning was applied.`
      : `Captured the original post only. ${cleaning === "deep" ? "Deep" : "Standard"} cleaning was applied.`;
  posts.append(notice);

  const selectedPosts = includeCurrentPage || repliesOnly
    ? sourcePosts
    : originalPost
      ? [originalPost]
      : [];
  for (const [index, sourcePost] of selectedPosts.entries()) {
    const body = sourcePost.querySelector<HTMLElement>("[id^='postmessage_']");
    if (!body || compactText(body.textContent ?? "").length === 0) continue;

    const capturedBody = body.cloneNode(true) as HTMLElement;
    cleanSiteContent(capturedBody, cleaning);
    for (const clutter of capturedBody.querySelectorAll<HTMLElement>(
      ".sign, .signature, .avatar, .userinfo, .member-info, .reply-control, .pagination, .pg, .a_pr, .a_pt, [id^='ad_']",
    )) clutter.remove();
    for (const panel of capturedBody.querySelectorAll<HTMLElement>(".blockcode")) {
      const lines = [...panel.querySelectorAll<HTMLLIElement>("ol > li")];
      if (lines.length === 0) continue;
      const pre = document.createElement("pre");
      const code = document.createElement("code");
      code.textContent = lines.map((line) => line.textContent ?? "").join("\n");
      pre.append(code);
      panel.replaceWith(pre);
    }

    const post = document.createElement("article");
    post.setAttribute("data-clipper-post", "1");
    post.setAttribute("data-clipper-role", sourcePost === originalPost ? "original" : "reply");
    const header = document.createElement("header");
    const author = sourcePost.querySelector<HTMLElement>(".authi .xw1, .authi a");
    const time = sourcePost.querySelector<HTMLElement>(".authi em, .authi time");
    if (author) {
      const strong = document.createElement("strong");
      strong.setAttribute("data-clipper-author", "1");
      strong.textContent = compactText(author.textContent ?? "");
      header.append(strong);
    }
    if (time) {
      const timeElement = document.createElement("time");
      const datetime = time.getAttribute("datetime");
      if (datetime) timeElement.setAttribute("datetime", datetime);
      timeElement.textContent = compactText(time.textContent ?? "");
      header.append(" ", timeElement);
    }
    post.append(header, capturedBody);
    posts.append(post);
  }

  if (posts.querySelectorAll("article").length === 0) return null;
  return resultFromElement(document, posts, scope);
}

function hasMeaningfulContent(element: ParentNode): boolean {
  const text = compactText(element.textContent ?? "");
  if (!text) return false;
  if (compactText(element.querySelector("pre, code")?.textContent ?? "")) return true;
  if (element.querySelectorAll("li").length >= 2) return true;
  if (element.querySelector("table td, table th")) return true;
  const paragraphText = [...element.querySelectorAll("p")]
    .map((paragraph) => compactText(paragraph.textContent ?? ""))
    .join(" ");
  return paragraphText.length >= 24 || Boolean(element.querySelector("h1, h2, h3") && paragraphText);
}

function captureReadableArticle(document: Document, cleaning: CleaningMode): ArticleCapture | null {
  const cloned = document.cloneNode(true) as Document;
  pruneNonContent(cloned, document);
  cleanSiteContent(cloned, cleaning);
  let parsed: ReturnType<Readability["parse"]>;
  try {
    parsed = new Readability(cloned, { charThreshold: 1, maxElemsToParse: 20_000 }).parse();
  } catch {
    return null;
  }
  if (!parsed?.content) return null;

  const container = document.createElement("article");
  container.innerHTML = parsed.content;
  cleanSiteContent(container, cleaning);
  if (!hasMeaningfulContent(container)) return null;
  return resultFromElement(
    document,
    genericCaptureScope(document, container, "article", cleaning),
    "article",
    compactText(parsed.title ?? "") || pageTitle(document),
  );
}

function captureSemanticContent(document: Document, cleaning: CleaningMode): ArticleCapture | null {
  const source = document.querySelector<HTMLElement>("main > article, body > article, [role='article'], main");
  if (!source) return null;
  const candidate = source.cloneNode(true) as HTMLElement;
  pruneNonContent(candidate, source);
  cleanSiteContent(candidate, cleaning);
  if (!hasMeaningfulContent(candidate)) return null;
  return resultFromElement(
    document,
    genericCaptureScope(document, candidate, "article", cleaning),
    "article",
  );
}

function captureSelection(document: Document): SelectionCapture | FallbackCapture {
  const selection = document.defaultView?.getSelection();
  if (!selection || selection.rangeCount === 0 || selection.isCollapsed) {
    return {
      kind: "fallback",
      title: pageTitle(document),
      sourceUrl: document.location.href,
      reason: "Select visible page content before choosing Save selection.",
    };
  }

  const fragment = selection.getRangeAt(0).cloneContents();
  const container = document.createElement("div");
  container.append(fragment);
  const images = prepareFragment(document, container);
  const html = sanitize(document, container.innerHTML);

  return {
    kind: "selection",
    title: pageTitle(document),
    sourceUrl: document.location.href,
    scope: selectionScope(selection.getRangeAt(0), html),
    html,
    text: textFromHtml(document, html),
    images,
  };
}

export function capturePage(document: Document, options: CaptureMode | CaptureOptions = "article"): PageCapture {
  const mode = typeof options === "string" ? options : options.mode ?? "article";
  const requestedScope = typeof options === "string" ? undefined : options.scope;
  const cleaning = typeof options === "string" ? "default" : options.cleaning ?? "default";
  if (mode === "selection") return captureSelection(document);

  const url = new URL(document.location.href);
  if (url.hostname === "github.com") {
    return (
      captureGitHubDiscussion(document, cleaning) ?? captureGitHubReadme(document, cleaning) ?? {
        kind: "fallback",
        title: pageTitle(document),
        sourceUrl: url.href,
        reason: "The rendered repository README could not be identified.",
      }
    );
  }

  if (discuzVisiblePosts(document).length > 0) {
    return (
      captureDiscuzPage(document, requestedScope, cleaning) ?? {
        kind: "fallback",
        title: pageTitle(document),
        sourceUrl: url.href,
        reason: "No readable posts were found on the current forum page.",
      }
    );
  }

  const pageIndex = capturePageIndex(document, cleaning);
  if (pageIndex) return pageIndex;

  return (
    captureReadableArticle(document, cleaning) ?? captureSemanticContent(document, cleaning) ?? {
      kind: "fallback",
      title: pageTitle(document),
      sourceUrl: url.href,
      reason: "No readable article was found. Save a selection or capture a region instead.",
    }
  );
}

export function inspectPage(document: Document): PageInspection {
  if (discuzVisiblePosts(document).length > 0) {
    const sourcePosts = discuzVisiblePosts(document);
    const originalAvailable = discuzOriginalPost(sourcePosts) !== null;
    return {
      kind: "supported",
      title: pageTitle(document),
      sourceUrl: document.location.href,
      detected: "discuz",
      label: originalAvailable ? "Discuz thread" : "Discuz replies on this page",
      scopes: originalAvailable
        ? [
            { value: "discuz-main-post", label: "Main post" },
            { value: "discuz-current-page", label: "Main post + current-page replies" },
          ]
        : [{ value: "discuz-current-page-replies", label: "Visible replies on this page" }],
      defaultScope: originalAvailable ? "discuz-main-post" : "discuz-current-page-replies",
      cleaningAvailable: true,
      ...(originalAvailable ? {} : { note: "The original post is not available on this page; it will not be inferred from a reply." }),
    };
  }
  const capture = capturePage(document);
  if (capture.kind === "fallback") return capture;
  if (capture.kind !== "article") {
    throw new Error("Page inspection unexpectedly returned a selection capture.");
  }
  const labels: Record<ArticleCapture["scope"], string> = {
    article: "Readable article",
    "github-readme": "GitHub README",
    "github-issue-loaded": "GitHub Issue discussion",
    "github-pr-loaded": "GitHub pull request discussion",
    "discuz-main-post": "Discuz original post",
    "discuz-current-page": "Discuz current-page discussion",
    "discuz-current-page-replies": "Discuz visible replies",
    "forum-current-page": "Discuz thread",
    "page-index": "Page index",
  };
  const detected = capture.scope === "github-readme"
    ? "github-readme"
    : capture.scope === "github-issue-loaded"
      ? "github-issue"
      : capture.scope === "github-pr-loaded"
        ? "github-pr"
    : capture.scope === "forum-current-page" || capture.scope.startsWith("discuz-")
      ? "discuz"
      : capture.scope === "page-index"
        ? "page-index"
        : "article";
  return {
    kind: "supported",
    title: capture.title,
    sourceUrl: capture.sourceUrl,
    detected,
    label: labels[capture.scope],
    scopes: [{ value: capture.scope, label: capture.scope === "article" ? "Article" : labels[capture.scope] }],
    defaultScope: capture.scope,
    cleaningAvailable: true,
    ...(capture.scope === "github-issue-loaded" || capture.scope === "github-pr-loaded"
      ? { note: "Only the description and discussion already loaded on this page will be captured; file diffs and hidden entries are excluded." }
      : {}),
  };
}
