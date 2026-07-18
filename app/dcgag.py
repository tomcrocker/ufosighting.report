"""Gag interstitial for Washington-DC visitors — a wink at the "declassify the
files" bit, NOT a real block: one click lets anyone straight through.

Detection uses Cloudflare's "Add visitor location headers" managed transform,
which adds CF-Region-Code (e.g. "DC") + CF-IPCountry. If that transform is off
the header is absent, is_dc() is False, and the whole thing is inert — so it is
safe to ship before the header exists. Gated behind DC_GAG_ENABLED either way.
"""
from starlette.responses import HTMLResponse, RedirectResponse

from app.config import get_settings

BYPASS_COOKIE = "dc_bypass"
BYPASS_PATH = "/dc/reveal"
PREVIEW_PATH = "/dc/preview"   # always renders the gag (for sharing/QA), ungated
_BYPASS_TTL = 30 * 24 * 3600


def is_dc(headers) -> bool:
    return (headers.get("cf-ipcountry", "").upper() == "US"
            and headers.get("cf-region-code", "").upper() == "DC")


def should_gag(request) -> bool:
    if not get_settings().dc_gag_enabled or request.method != "GET":
        return False
    path = request.url.path
    if path == BYPASS_PATH or path.startswith(("/static/", "/api/")):
        return False
    if request.cookies.get(BYPASS_COOKIE):
        return False
    return is_dc(request.headers)


async def middleware(request, call_next):
    if request.url.path == PREVIEW_PATH:      # see the bit without being in DC
        return HTMLResponse(_PAGE, status_code=200)
    if get_settings().dc_gag_enabled and request.url.path == BYPASS_PATH:
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(BYPASS_COOKIE, "1", max_age=_BYPASS_TTL,
                        httponly=True, samesite="lax")
        return resp
    if should_gag(request):
        return HTMLResponse(_PAGE, status_code=200)
    return await call_next(request)


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Restricted region — ufosighting.report</title>
<meta name="robots" content="noindex">
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center;
    background: radial-gradient(1200px 600px at 50% -10%, #16203a 0%, #0b0e14 55%);
    color: #dbe2f4; font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
    padding: 28px; }
  .card { max-width: 560px; text-align: center; }
  .saucer { width: 96px; height: 96px; margin: 0 auto 18px; display: block; }
  .beam { transform-origin: 50% 40%; animation: pulse 2.4s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { opacity: .35 } 50% { opacity: .8 } }
  @media (prefers-reduced-motion: reduce) { .beam { animation: none } }
  .eyebrow { font-size: .72rem; letter-spacing: .22em; text-transform: uppercase;
    color: #ff6a33; font-weight: 700; margin: 0 0 10px; }
  h1 { font-size: 1.9rem; margin: 0 0 14px; letter-spacing: -.01em; }
  p { color: #aab4d0; line-height: 1.65; margin: 0 0 14px; font-size: 1.02rem; }
  strong { color: #dbe2f4; }
  .btn { display: inline-block; margin-top: 8px; padding: 11px 20px; border-radius: 10px;
    background: #6ee7a0; color: #05240f; font-weight: 700; text-decoration: none; }
  .btn:hover { filter: brightness(1.08); }
  .fine { margin-top: 18px; font-size: .82rem; color: #6b7597; }
</style></head><body>
<div class="card">
  <svg class="saucer" viewBox="0 0 32 32" fill="none" aria-hidden="true">
    <polygon class="beam" points="11,15 21,15 26,32 6,32" fill="#6ee7a0" opacity=".5"/>
    <ellipse cx="16" cy="10" rx="6" ry="4.5" fill="#233350" stroke="#7aa2ff" stroke-width="1.4"/>
    <ellipse cx="16" cy="13" rx="12" ry="4" fill="#171d2e" stroke="#6ee7a0" stroke-width="1.6"/>
    <circle cx="9" cy="14" r="1.1" fill="#6ee7a0"/><circle cx="16" cy="15" r="1.1" fill="#7aa2ff"/>
    <circle cx="23" cy="14" r="1.1" fill="#6ee7a0"/>
  </svg>
  <p class="eyebrow">Region restricted</p>
  <h1>Nice try, Washington&nbsp;D.C. \U0001F6F8</h1>
  <p>Our sensors show you're connecting from <strong>Washington, D.C.</strong>,
     which means there's a decent chance you can get your hands on files we would
     <em>really</em> like to see.</p>
  <p>Declassify the UFO documents and the entire archive is yours. Until then,
     this page is as far as you go. \U0001F47D</p>
  <a class="btn" href="/dc/reveal">I don't work for the government, I swear &rarr;</a>
  <p class="fine">(It's a bit. Click the button and come on in.)</p>
</div></body></html>"""
