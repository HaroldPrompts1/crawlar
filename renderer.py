"""Render headless (Playwright/Chromium) solo como fallback para SPAs.

Thread-local: cada worker tiene su propio navegador (Playwright sync
no puede usarse desde un thread distinto al que lo creó).
"""
import threading

from playwright.sync_api import sync_playwright

from config import RENDER_TIMEOUT, USER_AGENT

log = __import__("logging").getLogger(__name__)

RENDER_SETTLE_MS = 1000

_local = threading.local()


def _browser():
    b = getattr(_local, "browser", None)
    if b is None:
        _local.pw = sync_playwright().start()
        b = _local.pw.chromium.launch(args=["--no-sandbox"])
        _local.browser = b
    return b


def render(url, settle_ms=RENDER_SETTLE_MS):
    """Devuelve (html_final, error). Nunca lanza excepción."""
    html, err, _, _ = render_with_api(url, settle_ms)
    return html, err


def render_with_api(url, settle_ms=RENDER_SETTLE_MS, max_blobs=8,
                    max_blob_bytes=512 * 1024):
    """Devuelve (html, error, blobs_json, inner_text).

    inner_text es lo que el navegador muestra al usuario: la fuente
    primaria de verdad. El HTML se usa para estructura (links, headings,
    imágenes, JSON-LD), no para adivinar el contenido visible.
    """
    blobs = []
    visible = ""
    skip_hosts = ("google-analytics", "googletagmanager", "doubleclick",
                  "facebook.com/tr", "hotjar", "analytics")
    try:
        page = _browser().new_page(user_agent=USER_AGENT)
        try:
            def on_response(resp):
                try:
                    if len(blobs) >= max_blobs:
                        return
                    ct = resp.headers.get("content-type", "")
                    if "json" not in ct:
                        return
                    if any(s in resp.url for s in skip_hosts):
                        return
                    body = resp.body()
                    if 200 < len(body) < max_blob_bytes:
                        blobs.append(body.decode("utf-8", errors="replace"))
                except Exception:
                    pass
            page.on("response", on_response)
            page.goto(url, timeout=RENDER_TIMEOUT * 1000, wait_until="domcontentloaded")
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(settle_ms)
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(500)
            except Exception:
                pass
            try:
                visible = page.inner_text("body")
            except Exception:
                visible = ""
            return page.content(), None, blobs, visible
        finally:
            page.close()
    except Exception as e:
        log.warning("render falló para %s: %s", url, e)
        return None, f"render:{type(e).__name__}", blobs, visible


def close():
    for attr in ("browser", "pw"):
        try:
            obj = getattr(_local, attr, None)
            if obj:
                obj.close() if attr == "browser" else obj.stop()
        except Exception:
            pass
        finally:
            if hasattr(_local, attr):
                delattr(_local, attr)
