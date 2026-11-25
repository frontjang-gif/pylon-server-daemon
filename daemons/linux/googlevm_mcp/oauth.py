"""Single-owner OAuth authorization policy; no workspace or MCP dependencies.

Public clients use authorization code + S256 PKCE. Redirects are explicitly
allowlisted. Refresh reuse revokes the entire authorization grant.
"""
import base64
import hashlib
import re
import secrets
import time
from urllib.parse import urlsplit

from .oauth_store import OAuthStore, token_hash
from .owner_credentials import load_owner_password


class OAuthError(ValueError):
    def __init__(self, error, description, status=400):
        super().__init__(description)
        self.error, self.status = error, status


def validate_url(value, *, origin=False):
    if not isinstance(value, str) or any(ord(c) <= 32 or ord(c) >= 127 for c in value) or "\\" in value:
        raise ValueError("URLs must be ASCII and contain no whitespace or backslashes")
    parsed = urlsplit(value)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.fragment
            or (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")))):
        raise ValueError("URLs require HTTPS (HTTP is allowed only for loopback development)")
    if origin and (parsed.path not in ("", "/") or parsed.query):
        raise ValueError("public_base_url must be an origin without a path or query")
    _ = parsed.port
    return value.rstrip("/") if origin else value


class OAuthProvider:
    scope = "vm"

    def __init__(self, config, root):
        self.issuer = validate_url(config.get("public_base_url", ""), origin=True)
        self.resource = self.issuer + "/mcp"
        self.owner_password = load_owner_password(config, root)
        if len(self.owner_password) < 32:
            raise ValueError("OAuth requires an owner password of at least 32 characters in owner_password_env")
        self.redirects = config.get("allowed_redirect_uris", [])
        if not isinstance(self.redirects, list) or not self.redirects:
            raise ValueError("OAuth requires explicit allowed_redirect_uris")
        for uri in self.redirects:
            validate_url(uri)
        self.access_ttl = max(60, min(int(config.get("access_token_ttl_seconds", 3600)), 86400))
        self.refresh_ttl = max(self.access_ttl, min(int(config.get("refresh_token_ttl_seconds", 2592000)), 7776000))
        path = config.get("state_db_path", "state/oauth.sqlite3")
        self.store = OAuthStore(":memory:" if path == ":memory:" else str(root / path))

    def metadata(self):
        return {"issuer": self.issuer, "authorization_endpoint": self.issuer + "/oauth/authorize",
                "token_endpoint": self.issuer + "/oauth/token", "registration_endpoint": self.issuer + "/oauth/register",
                "revocation_endpoint": self.issuer + "/oauth/revoke", "scopes_supported": [self.scope],
                "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": ["none"], "revocation_endpoint_auth_methods_supported": ["none"],
                "code_challenge_methods_supported": ["S256"], "client_id_metadata_document_supported": False,
                "authorization_response_iss_parameter_supported": True}

    def protected_metadata(self):
        return {"resource": self.resource, "authorization_servers": [self.issuer],
                "scopes_supported": [self.scope], "bearer_methods_supported": ["header"]}

    def register(self, data):
        redirects = data.get("redirect_uris")
        if (not isinstance(redirects, list) or not redirects or len(redirects) > 10
                or any(not isinstance(uri, str) or uri not in self.redirects for uri in redirects)):
            raise OAuthError("invalid_redirect_uri", "Every redirect URI must be explicitly allowed by the owner")
        if data.get("token_endpoint_auth_method", "none") != "none":
            raise OAuthError("invalid_client_metadata", "Only public clients using PKCE are supported")
        grants = data.get("grant_types", ["authorization_code", "refresh_token"])
        if (not isinstance(grants, list) or "authorization_code" not in grants
                or any(g not in ("authorization_code", "refresh_token") for g in grants)
                or data.get("response_types", ["code"]) != ["code"]):
            raise OAuthError("invalid_client_metadata", "Unsupported grant or response type")
        name = data.get("client_name", "MCP client")
        if not isinstance(name, str) or not 1 <= len(name) <= 200:
            raise OAuthError("invalid_client_metadata", "client_name must contain 1-200 characters")
        if data.get("scope", self.scope) != self.scope:
            raise OAuthError("invalid_scope", "Unsupported scope")
        now = time.time()
        client = {"client_id": secrets.token_urlsafe(24), "client_id_issued_at": int(now),
                  "client_name": name, "redirect_uris": redirects, "token_endpoint_auth_method": "none",
                  "grant_types": grants, "response_types": ["code"], "scope": self.scope}
        with self.store.transaction() as db:
            if db.count("client") >= 1000:
                raise OAuthError("temporarily_unavailable", "Client registration capacity reached", 503)
            db.put("client", client["client_id"], client, now + 10 * 365 * 86400)
        return client

    def _client(self, db, client_id):
        client = db.get("client", client_id)
        if not client:
            raise OAuthError("invalid_client", "Unknown client")
        return client

    def begin_authorization(self, data, browser_nonce):
        with self.store.transaction() as db:
            client = self._client(db, data.get("client_id", ""))
            redirect = data.get("redirect_uri", "")
            if redirect not in client["redirect_uris"] or redirect not in self.redirects:
                raise OAuthError("invalid_request", "Unregistered redirect URI")
            if data.get("response_type") != "code":
                raise OAuthError("unsupported_response_type", "Only authorization code is supported")
            if data.get("resource") != self.resource:
                raise OAuthError("invalid_target", "Missing or incorrect MCP resource")
            if data.get("scope", self.scope) != self.scope:
                raise OAuthError("invalid_scope", "Unsupported scope")
            if data.get("code_challenge_method") != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", data.get("code_challenge", "")):
                raise OAuthError("invalid_request", "S256 PKCE challenge is required")
            if db.count("pending") >= 1000:
                raise OAuthError("temporarily_unavailable", "Authorization capacity reached", 503)
            pending = {k: data.get(k, "") for k in ("client_id", "redirect_uri", "resource", "state", "code_challenge")}
            pending.update(scope=self.scope, browser_hash=token_hash(browser_nonce))
            ticket = secrets.token_urlsafe(32)
            db.put("pending", token_hash(ticket), pending, time.time() + 300)
            return ticket, client["client_name"]

    def approve(self, ticket, browser_nonce, password, approved):
        # A global persisted window also bounds attempts behind reverse proxies.
        with self.store.transaction() as db:
            key = token_hash(ticket)
            pending = db.get("pending", key)
            if not pending or not secrets.compare_digest(pending["browser_hash"], token_hash(browser_nonce)):
                return None, OAuthError("invalid_request", "Authorization form expired or browser cookie missing")
            if not approved:
                db.delete("pending", key)
                return (pending, {"error": "access_denied"}), None
            now = time.time()
            rate = db.get("rate", "owner") or {"attempts": 0, "until": now + 60}
            if rate["attempts"] >= 10:
                return None, OAuthError("temporarily_unavailable", "Too many approval attempts; retry after one minute", 429)
            rate["attempts"] += 1
            db.put("rate", "owner", rate, rate["until"])
            if not secrets.compare_digest(password.encode(), self.owner_password.encode()):
                return None, OAuthError("access_denied", "Owner password was not accepted", 401)
            db.delete("pending", key)
            pending.pop("browser_hash")
            code = secrets.token_urlsafe(32)
            db.put("code", token_hash(code), pending, now + 300)
            return (pending, {"code": code}), None

    def _issue(self, db, record, family=None):
        now = time.time()
        if family is None:
            family = secrets.token_urlsafe(24)
            expires = now + self.refresh_ttl
            db.put("family", family, {"expires": expires}, expires)
        else:
            expires = db.get("family", family)["expires"]
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        value = {"client_id": record["client_id"], "resource": self.resource, "scope": self.scope, "family": family}
        ttl = min(self.access_ttl, max(1, int(expires - now)))
        db.put("access", token_hash(access), value, now + ttl)
        result = {"access_token": access, "token_type": "Bearer", "expires_in": ttl, "scope": self.scope}
        if "refresh_token" in self._client(db, record["client_id"])["grant_types"]:
            db.put("refresh", token_hash(refresh), {**value, "used": False, "expires": expires}, expires)
            result["refresh_token"] = refresh
        return result

    def exchange(self, data):
        with self.store.transaction() as db:
            client_id = data.get("client_id", "")
            self._client(db, client_id)
            if data.get("resource") != self.resource:
                raise OAuthError("invalid_target", "Missing or incorrect MCP resource")
            if data.get("scope", self.scope) != self.scope:
                raise OAuthError("invalid_scope", "Unsupported scope")
            grant = data.get("grant_type")
            if grant == "authorization_code":
                key = token_hash(data.get("code", ""))
                record = db.get("code", key)
                verifier = data.get("code_verifier", "")
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
                if (not record or record["client_id"] != client_id or record["resource"] != self.resource
                        or record["redirect_uri"] != data.get("redirect_uri") or record["redirect_uri"] not in self.redirects
                        or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier)
                        or not secrets.compare_digest(record["code_challenge"], challenge)):
                    raise OAuthError("invalid_grant", "Invalid code, redirect URI, or PKCE verifier")
                db.delete("code", key)
                return self._issue(db, record), None
            if grant == "refresh_token":
                key = token_hash(data.get("refresh_token", ""))
                record = db.get("refresh", key)
                if not record or record["client_id"] != client_id or record["resource"] != self.resource or not db.get("family", record["family"]):
                    raise OAuthError("invalid_grant", "Invalid or expired refresh token")
                if record["used"]:
                    db.delete("family", record["family"])
                    # Return the error after committing the revocation.
                    return None, OAuthError("invalid_grant", "Refresh token reuse revoked this authorization")
                record["used"] = True
                db.put("refresh", key, record, record["expires"])
                return self._issue(db, record, record["family"]), None
            raise OAuthError("unsupported_grant_type", "Unsupported grant type")

    def authenticate(self, token):
        with self.store.transaction() as db:
            record = db.get("access", token_hash(token))
            if record and record["resource"] == self.resource and record["scope"] == self.scope and db.get("family", record["family"]):
                return "oauth:" + record["client_id"]
        return None

    def revoke(self, data):
        with self.store.transaction() as db:
            client_id = data.get("client_id", "")
            self._client(db, client_id)
            key = token_hash(data.get("token", ""))
            for kind in ("access", "refresh"):
                record = db.get(kind, key)
                if record and record["client_id"] == client_id:
                    db.delete("family", record["family"])

    def close(self):
        self.store.close()
