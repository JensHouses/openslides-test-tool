"""Minimal async client for the OpenSlides 4 HTTP API.

Every OSClient represents one logged-in user. Many OSClients share one
httpx.AsyncClient (and therefore one connection pool). Cookies are handled
manually per OSClient, because a shared cookie jar would mix up the sessions.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http.cookiejar import CookieJar
from typing import Any

import httpx

LOGIN_PATH = "/system/auth/login"
WHO_AM_I_PATH = "/system/auth/who-am-i"
ACTION_PATH = "/system/action/handle_request"
PRESENTER_PATH = "/system/presenter/handle_request"
AUTOUPDATE_PATH = "/system/autoupdate"


class OpenSlidesError(Exception):
    """Raised when OpenSlides answers with an error."""

    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class _NoCookieJar(CookieJar):
    """Cookie jar that never stores anything."""

    def extract_cookies(self, response, request):  # noqa: D401 - stdlib signature
        return

    def set_cookie(self, cookie):
        return


def make_http_client(
    base_url: str,
    *,
    verify_tls: bool = False,
    timeout: float = 120.0,
    max_connections: int = 1000,
    force_ipv4: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Creates the shared httpx client."""
    limits = httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=max_connections,
    )
    if transport is None:
        transport = httpx.AsyncHTTPTransport(
            verify=verify_tls,
            limits=limits,
            local_address="0.0.0.0" if force_ipv4 else None,
            retries=0,
        )
    return httpx.AsyncClient(
        base_url=base_url,
        transport=transport,
        cookies=_NoCookieJar(),
        # No pool timeout: waiting for a free connection is part of the load.
        timeout=httpx.Timeout(timeout, pool=None),
        follow_redirects=False,
    )


def _jwt_payload(token: str) -> dict[str, Any]:
    """Decodes the payload of a JWT without validating it."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except (IndexError, ValueError):
        return {}


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(body, dict):
        return str(body.get("message") or body.get("error") or body)[:500]
    return str(body)[:500]


class OSClient:
    """One logged-in OpenSlides user."""

    def __init__(self, http: httpx.AsyncClient, username: str = "", password: str = ""):
        self.http = http
        self.username = username
        self.password = password
        self.token: str | None = None
        self.refresh_cookie: str | None = None
        self.user_id: int | None = None
        self.token_exp: float = 0.0
        self._refresh_lock = asyncio.Lock()

    # ------------------------------------------------------------------ auth

    @property
    def logged_in(self) -> bool:
        return self.token is not None

    def _set_token(self, token: str) -> None:
        self.token = token
        payload = _jwt_payload(token.removeprefix("bearer ").removeprefix("Bearer "))
        self.user_id = payload.get("userId", self.user_id)
        self.token_exp = float(payload.get("exp", time.time() + 600))

    async def login(self, username: str | None = None, password: str | None = None) -> None:
        if username is not None:
            self.username = username
        if password is not None:
            self.password = password
        response = await self.http.post(
            LOGIN_PATH, json={"username": self.username, "password": self.password}
        )
        if response.status_code != 200:
            raise OpenSlidesError(
                f"login of {self.username!r} failed: {response.status_code} {_error_message(response)}",
                response.status_code,
                response.text,
            )
        token = response.headers.get("authentication")
        if not token:
            raise OpenSlidesError(f"login of {self.username!r}: no token in response")
        self._set_token(token)
        self.refresh_cookie = response.cookies.get("refreshId")

    def token_expires_in(self) -> float:
        return self.token_exp - time.time()

    async def refresh(self) -> None:
        """Fetches a new access token with the refresh cookie."""
        async with self._refresh_lock:
            response = await self.http.post(WHO_AM_I_PATH, headers=self._cookie_header())
            token = response.headers.get("authentication")
            if response.status_code != 200 or not token:
                # Refresh failed (e.g. session expired) -> new login.
                await self.login()
                return
            self._set_token(token)

    async def ensure_fresh(self, margin: float = 60.0) -> bool:
        """Refreshes the token if it expires within `margin` seconds."""
        if self.token is None:
            return False
        if self.token_expires_in() < margin:
            await self.refresh()
            return True
        return False

    def _cookie_header(self) -> dict[str, str]:
        if self.refresh_cookie:
            return {"cookie": f"refreshId={self.refresh_cookie}"}
        return {}

    def auth_headers(self) -> dict[str, str]:
        headers = self._cookie_header()
        if self.token:
            headers["authentication"] = self.token
        return headers

    # -------------------------------------------------------------- requests

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Sends an authenticated request. Retries once after a token refresh on 401."""
        await self.ensure_fresh()
        headers = {**kwargs.pop("headers", {}), **self.auth_headers()}
        response = await self.http.request(method, path, headers=headers, **kwargs)
        if response.status_code == 401 and self.token is not None:
            await self.refresh()
            headers.update(self.auth_headers())
            response = await self.http.request(method, path, headers=headers, **kwargs)
        return response

    async def action(self, name: str, data: list[dict[str, Any]]) -> list[Any]:
        """Calls a backend action and returns the result list of this action."""
        response = await self.request("POST", ACTION_PATH, json=[{"action": name, "data": data}])
        if response.status_code == 202:
            return await self._wait_action_worker(name, response)
        if response.status_code != 200:
            raise OpenSlidesError(
                f"action {name} failed: {response.status_code} {_error_message(response)}",
                response.status_code,
                response.text,
            )
        body = response.json()
        if not body.get("success", True):
            raise OpenSlidesError(f"action {name} failed: {body.get('message')}", 200, response.text)
        results = body.get("results") or [[]]
        return results[0] or []

    async def _wait_action_worker(self, name: str, response: httpx.Response) -> list[Any]:
        """Long running actions return an action_worker; wait until it is done."""
        body = response.json()
        try:
            fqid = body["results"][0][0]["fqid"]
            worker_id = int(fqid.split("/")[1])
        except (KeyError, IndexError, ValueError, TypeError) as err:
            raise OpenSlidesError(f"action {name}: unexpected 202 response {body}") from err
        while True:
            data = await self.get(
                [{"collection": "action_worker", "ids": [worker_id], "fields": {"state": None, "result": None}}]
            )
            state = data.get(f"action_worker/{worker_id}/state")
            if state == "end":
                result = data.get(f"action_worker/{worker_id}/result") or {}
                results = result.get("results") if isinstance(result, dict) else None
                return (results or [[]])[0] or []
            if state == "aborted":
                raise OpenSlidesError(f"action {name}: action worker {worker_id} aborted")
            await asyncio.sleep(0.5)

    async def presenter(self, name: str, data: dict[str, Any]) -> Any:
        """Calls a presenter and returns its result."""
        response = await self.request("POST", PRESENTER_PATH, json=[{"presenter": name, "data": data}])
        if response.status_code != 200:
            raise OpenSlidesError(
                f"presenter {name} failed: {response.status_code} {_error_message(response)}",
                response.status_code,
                response.text,
            )
        body = response.json()
        if not isinstance(body, list) or not body:
            raise OpenSlidesError(f"presenter {name}: unexpected response {str(body)[:200]}")
        return body[0]

    async def get(self, requests: list[dict[str, Any]]) -> dict[str, Any]:
        """Reads data once via the autoupdate service. Returns `collection/id/field -> value`."""
        response = await self.request("POST", AUTOUPDATE_PATH, params={"single": "1"}, json=requests)
        if response.status_code != 200:
            raise OpenSlidesError(
                f"autoupdate failed: {response.status_code} {_error_message(response)}",
                response.status_code,
                response.text,
            )
        return response.json()

    @asynccontextmanager
    async def stream(self, requests: list[dict[str, Any]]) -> AsyncIterator[AsyncIterator[dict[str, Any]]]:
        """Opens a streaming autoupdate connection. Yields an iterator of messages."""
        await self.ensure_fresh(margin=120)
        request = self.http.build_request(
            "POST",
            AUTOUPDATE_PATH,
            json=requests,
            headers=self.auth_headers(),
            timeout=httpx.Timeout(None),
        )
        response = await self.http.send(request, stream=True)
        try:
            if response.status_code != 200:
                await response.aread()
                raise OpenSlidesError(
                    f"autoupdate stream failed: {response.status_code} {_error_message(response)}",
                    response.status_code,
                )

            async def messages() -> AsyncIterator[dict[str, Any]]:
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        message = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(message, dict):
                        yield message

            yield messages()
        finally:
            await response.aclose()


def fqfield(collection: str, id_: int, field: str) -> str:
    return f"{collection}/{id_}/{field}"
