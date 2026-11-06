class HttpError extends Error {
  constructor(
    readonly status: number,
    url: string
  ) {
    super(`Failed to load ${url}: HTTP ${status}`);
  }
}

interface FetchJsonOptions {
  attempts?: number;
  timeoutMs?: number;
  retryDelayMs?: number;
}

// Bound each attempt, including the response body, so a stalled download cannot
// leave a Suspense boundary or the background prefetch waiting forever.
export const fetchJson = async <T>(
  url: string,
  {
    attempts = 3,
    timeoutMs = 15_000,
    retryDelayMs = 500,
  }: FetchJsonOptions = {}
): Promise<T> => {
  for (let attempt = 0; ; attempt += 1) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, { signal: controller.signal });
      if (!response.ok) {
        void response.body?.cancel().catch(() => undefined);
        throw new HttpError(response.status, url);
      }
      return (await response.json()) as T;
    } catch (error) {
      const isTransient =
        controller.signal.aborted ||
        error instanceof TypeError ||
        (error instanceof HttpError &&
          (error.status === 408 ||
            error.status === 429 ||
            error.status >= 500));
      if (!isTransient || attempt + 1 >= attempts) throw error;
    } finally {
      clearTimeout(timeout);
    }
    await new Promise((resolve) =>
      setTimeout(resolve, retryDelayMs * 2 ** attempt)
    );
  }
};
