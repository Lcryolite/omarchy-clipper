export class NativeHostError extends Error {
  constructor(message: string, readonly code?: string) {
    super(message);
    this.name = "NativeHostError";
  }
}

export function unwrapNativeResponse(response: unknown): Record<string, any> {
  if (!response || typeof response !== "object") throw new Error("The native host returned an invalid response.");
  const envelope = response as { ok?: unknown; result?: unknown; error?: unknown; errorCode?: unknown };
  if (envelope.ok !== true) {
    throw new NativeHostError(
      typeof envelope.error === "string" ? envelope.error : "The native host rejected the request.",
      typeof envelope.errorCode === "string" ? envelope.errorCode : undefined,
    );
  }
  if (!envelope.result || typeof envelope.result !== "object" || Array.isArray(envelope.result)) {
    throw new Error("The native host returned an invalid result.");
  }
  return envelope.result as Record<string, any>;
}
