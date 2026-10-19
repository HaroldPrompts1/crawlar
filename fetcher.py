"""Descarga HTTP con reintentos, condicionales y tope de tamaño."""
import httpx

from config import MAX_BYTES, REQUEST_TIMEOUT, USER_AGENT

RETRYABLE = {408, 425, 429, 500, 502, 503, 504}


class FetchResult:
    def __init__(self, status, headers, body, not_modified=False, error=None,
                 final_url=None):
        self.status = status
        self.headers = headers
        self.body = body
        self.not_modified = not_modified
        self.error = error
        self.final_url = final_url


def fetch(url, etag=None, last_modified=None):
    headers = {"User-Agent": USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    try:
        with httpx.stream("GET", url, headers=headers,
                           timeout=REQUEST_TIMEOUT,
                           follow_redirects=True) as r:
            final = str(r.url)
            if r.status_code == 304:
                return FetchResult(304, dict(r.headers), b"", not_modified=True,
                                   final_url=final)
            chunks, total = [], 0
            for chunk in r.iter_bytes(65536):
                total += len(chunk)
                if total > MAX_BYTES:
                    return FetchResult(r.status_code, dict(r.headers), b"",
                                       error=f"too_big:{total}", final_url=final)
                chunks.append(chunk)
            return FetchResult(r.status_code, dict(r.headers), b"".join(chunks),
                               final_url=final)
    except httpx.TimeoutException:
        return FetchResult(None, {}, b"", error="timeout")
    except httpx.ConnectError:
        return FetchResult(None, {}, b"", error="connect_error")
    except Exception as e:
        return FetchResult(None, {}, b"", error=f"fetch:{type(e).__name__}")


def is_retryable(result):
    if result.error in ("timeout", "connect_error"):
        return True
    return result.status in RETRYABLE
