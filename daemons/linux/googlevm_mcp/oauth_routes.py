"""OAuth HTTP endpoints and the browser owner-approval form."""
import asyncio
from html import escape
import secrets
import time
from urllib.parse import urlencode, urlsplit

from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from .http_utils import read_form, read_json, unique_parameters
from .oauth import OAuthError


SECURITY_HEADERS = {
    "Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
}


class OAuthRoutes:
    cookie_name = "googlevm_oauth_browser"

    def __init__(self, provider):
        self.provider = provider

    def routes(self):
        return [Route("/.well-known/oauth-protected-resource", self.protected),
                Route("/.well-known/oauth-protected-resource/mcp", self.protected),
                Route("/.well-known/oauth-authorization-server", self.metadata),
                Route("/oauth/register", self.register, methods=["POST"]),
                Route("/oauth/authorize", self.authorize, methods=["GET", "POST"]),
                Route("/oauth/token", self.token, methods=["POST"]),
                Route("/oauth/revoke", self.revoke, methods=["POST"])]

    @staticmethod
    def error(exc):
        if not isinstance(exc, OAuthError):
            exc = OAuthError("invalid_request", str(exc))
        return JSONResponse({"error": exc.error, "error_description": str(exc)},
                            status_code=exc.status, headers=SECURITY_HEADERS)

    def throttle(self, bucket, limit=120):
        # Do not trust X-Forwarded-For to bypass abuse limits.
        now = time.time()
        with self.provider.store.transaction() as db:
            rate = db.get("rate", bucket) or {"count": 0, "until": now + 60}
            if rate["count"] >= limit:
                raise OAuthError("temporarily_unavailable", "Rate limit exceeded; retry after one minute", 429)
            rate["count"] += 1
            db.put("rate", bucket, rate, rate["until"])

    async def protected(self, request):
        return JSONResponse(self.provider.protected_metadata())

    async def metadata(self, request):
        return JSONResponse(self.provider.metadata())

    async def register(self, request):
        try:
            await asyncio.to_thread(self.throttle, "register", 20)
            data = await read_json(request, 16384)
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object")
            return JSONResponse(await asyncio.to_thread(self.provider.register, data), status_code=201, headers=SECURITY_HEADERS)
        except (ValueError, UnicodeError) as exc:
            return self.error(exc)

    async def authorize(self, request):
        try:
            await asyncio.to_thread(self.throttle, "authorize")
            if request.method == "GET":
                if len(request.url.query) > 16384:
                    raise ValueError("Authorization query exceeds size limit")
                data = unique_parameters(request.query_params.multi_items())
                nonce = request.cookies.get(self.cookie_name) or secrets.token_urlsafe(32)
                ticket, client_name = await asyncio.to_thread(self.provider.begin_authorization, data, nonce)
                html = f'''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect Google VM MCP</title><style>
body{{font:16px system-ui;background:#f4f6f8;color:#17212b;margin:0}}
main{{max-width:480px;margin:10vh auto;padding:32px;background:white;border-radius:12px}}
input,button{{font:inherit;padding:10px;box-sizing:border-box;margin-top:12px}}
input{{width:100%}}button{{cursor:pointer}}p{{line-height:1.5}}code{{overflow-wrap:anywhere}}
</style><main><h1>Connect Google VM MCP</h1>
<p><strong>{escape(client_name)}</strong> is requesting access to your Google VM.</p>
<p>This allows reading and changing files and running commands with your VM user permissions.</p>
<p>Server: <code>{escape(self.provider.resource)}</code></p>
<p>Return to: <code>{escape(data['redirect_uri'])}</code></p>
<form method="post" action="/oauth/authorize">
<input type="hidden" name="ticket" value="{escape(ticket, quote=True)}">
<label for="password">Owner password</label>
<input id="password" name="owner_password" type="password" autocomplete="current-password">
<button name="decision" value="approve">Approve connection</button>
<button name="decision" value="deny">Deny</button></form></main></html>'''
                # no-referrer turns the Origin of a browser form POST into null.
                # Preserve it for this same-origin form, but retain no-referrer
                # on OAuth redirects so authorization details do not leak.
                callback = urlsplit(data["redirect_uri"])
                callback_origin = f"{callback.scheme}://{callback.netloc}"
                # Chromium applies form-action to the redirect chain too. The
                # URI has already passed exact registered-callback validation.
                policy = SECURITY_HEADERS["Content-Security-Policy"].replace(
                    "form-action 'self'", f"form-action 'self' {callback_origin}")
                response = HTMLResponse(html, headers={**SECURITY_HEADERS, "Referrer-Policy": "same-origin",
                                                       "Content-Security-Policy": policy})
                response.set_cookie(self.cookie_name, nonce, max_age=300, httponly=True,
                                    secure=self.provider.issuer.startswith("https:"), samesite="lax", path="/oauth/authorize")
                return response
            origin = request.headers.get("origin")
            if origin and origin != self.provider.issuer:
                raise OAuthError("invalid_request", "Approval origin is not allowed", 403)
            data = await read_form(request)
            if data.get("decision") not in ("approve", "deny"):
                raise ValueError("Choose approve or deny")
            result, error = await asyncio.to_thread(self.provider.approve, data.get("ticket", ""), request.cookies.get(self.cookie_name, ""),
                                                  data.get("owner_password", ""), data["decision"] == "approve")
            if error:
                raise error
            pending, parameters = result
            parameters["iss"] = self.provider.issuer
            if pending.get("state"):
                parameters["state"] = pending["state"]
            redirect = pending["redirect_uri"]
            return RedirectResponse(redirect + ("&" if "?" in redirect else "?") + urlencode(parameters),
                                    status_code=303, headers=SECURITY_HEADERS)
        except (ValueError, UnicodeError) as exc:
            return self.error(exc)

    async def token(self, request):
        try:
            await asyncio.to_thread(self.throttle, "token")
            data = await read_form(request)
            if request.headers.get("authorization") or data.get("client_secret"):
                raise OAuthError("invalid_client", "Only public client authentication is supported")
            result, error = await asyncio.to_thread(self.provider.exchange, data)
            if error:
                raise error
            return JSONResponse(result, headers=SECURITY_HEADERS)
        except (ValueError, UnicodeError) as exc:
            return self.error(exc)

    async def revoke(self, request):
        try:
            await asyncio.to_thread(self.throttle, "revoke")
            await asyncio.to_thread(self.provider.revoke, await read_form(request))
            return JSONResponse({}, headers=SECURITY_HEADERS)
        except (ValueError, UnicodeError) as exc:
            return self.error(exc)
