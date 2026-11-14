// Thin fetch wrapper: JSON in and out, timeout from server settings, structured errors (never fake data).
let timeoutMs = null;

export function setTimeoutMs(value) {
  timeoutMs = Number.isFinite(value) && value > 0 ? value : null;
}

export class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

function describeFailure(payload, status, path) {
  const err = payload && typeof payload === 'object' ? payload.error : null;
  if (err && typeof err === 'object') {
    const parts = [err.code, err.message, err.file, err.detail].filter((part) => typeof part === 'string' && part);
    return `${path} failed (HTTP ${status}): ${parts.join(' | ')}`;
  }
  return `${path} failed with HTTP ${status}`;
}

export async function api(path, { method = 'GET', body } = {}) {
  const init = { method, headers: { Accept: 'application/json' }, cache: 'no-store', credentials: 'same-origin' };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  if (timeoutMs !== null) {
    init.signal = AbortSignal.timeout(timeoutMs);
  }
  const started = performance.now();
  let response;
  try {
    response = await fetch(path, init);
  } catch (err) {
    const reason = err && err.name === 'TimeoutError' ? `timed out after ${timeoutMs} ms` : 'network error (session expired or server unreachable)';
    throw new ApiError(`${path}: ${reason}`, 0, null);
  }
  const clientMs = Math.round(performance.now() - started);
  let payload;
  try {
    payload = await response.json();
  } catch (err) {
    throw new ApiError(`${path}: response was not JSON (HTTP ${response.status})`, response.status, null);
  }
  if (!response.ok) {
    throw new ApiError(describeFailure(payload, response.status, path), response.status, payload);
  }
  return { data: payload, clientMs, traceId: response.headers.get('X-Trace-Id') || '' };
}
