"""OAuth 2.0 client-credentials token acquisition (for OAuth-gated FHIR Plan-Net payers).

Most national payers (Aetna, Anthem/Elevance, …) gate their public Provider Directory
behind OAuth 2.0 client-credentials — CMS permits requiring app registration. This is the
shared, cached token provider used by both the live source (async) and the validator
(sync), so a payer with `auth: oauth2` works in serving and in the trust gate alike.

Secrets never need to live in source: a registry/payers.json entry names `client_id_env`
/ `client_secret_env` and the values are read from the environment. The token is cached
until shortly before expiry; a fetch failure returns None (the caller degrades to
"unknown" — never a fabricated yes).
"""
from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class ClientCredentials:
    token_url: str
    client_id: str
    client_secret: str
    scope: str | None = None
    _token: str | None = field(default=None, repr=False)
    _exp: float = 0.0

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> ClientCredentials | None:
        """Build from a payer config, or None if it isn't OAuth-configured. Secrets come
        from inline values or, preferably, the env vars named by *_env."""
        if cfg.get("auth") not in ("oauth2", "oauth2_client_credentials"):
            return None
        token_url = cfg.get("token_url") or ""
        cid = cfg.get("client_id") or os.environ.get(cfg.get("client_id_env", "") or "", "")
        sec = cfg.get("client_secret") or os.environ.get(cfg.get("client_secret_env", "") or "", "")
        if not (token_url and cid and sec):
            return None
        return cls(token_url, cid, sec, cfg.get("scope") or None)

    def _request(self) -> tuple[dict[str, str], dict[str, str]]:
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        headers = {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        data = {"grant_type": "client_credentials"}
        if self.scope:
            data["scope"] = self.scope
        return headers, data

    def _store(self, payload: dict[str, Any]) -> str | None:
        raw = payload.get("access_token")
        if not raw:
            return None
        tok = str(raw)
        # Refresh 60s early; default to a conservative 5 min if expires_in is absent.
        try:
            ttl = int(payload.get("expires_in", 300))
        except (TypeError, ValueError):
            ttl = 300
        self._token = tok
        self._exp = time.time() + max(0, ttl - 60)
        return tok

    def _cached(self) -> str | None:
        return self._token if (self._token and time.time() < self._exp) else None

    def token_sync(self, client: httpx.Client) -> str | None:
        if (t := self._cached()):
            return t
        headers, data = self._request()
        r = client.post(self.token_url, headers=headers, data=data, timeout=20.0)
        r.raise_for_status()
        return self._store(r.json())

    async def token_async(self, client: httpx.AsyncClient) -> str | None:
        if (t := self._cached()):
            return t
        headers, data = self._request()
        r = await client.post(self.token_url, headers=headers, data=data)
        r.raise_for_status()
        return self._store(r.json())
