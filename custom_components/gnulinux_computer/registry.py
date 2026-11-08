# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eduard Vidal Tulsà
"""Check container registries for newer image digests (HEAD requests only)."""

from __future__ import annotations

import asyncio
import logging
import re

import aiohttp

_LOGGER = logging.getLogger(__name__)

ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
    ]
)
_AUTH_RE = re.compile(r'(\w+)="([^"]*)"')
TIMEOUT = aiohttp.ClientTimeout(total=20)


def parse_reference(ref: str) -> tuple[str, str, str] | None:
    """Split an image reference into (registry, repository, tag)."""
    if "@" in ref:
        return None  # pinned by digest, never "updates"
    name, tag = ref, "latest"
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        name, tag = ref.rsplit(":", 1)
    parts = name.split("/", 1)
    if len(parts) == 2 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        registry, repo = parts
    else:
        registry, repo = "docker.io", name
    if registry in ("docker.io", "index.docker.io"):
        registry = "registry-1.docker.io"
        if "/" not in repo:
            repo = f"library/{repo}"
    if registry.startswith("localhost"):
        return None
    return registry, repo, tag


class RegistryClient:
    """Minimal OCI distribution client with anonymous token auth."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialize."""
        self._session = session
        self._tokens: dict[tuple[str, str], str] = {}

    async def _token(self, header: str) -> str | None:
        params = dict(_AUTH_RE.findall(header))
        realm = params.pop("realm", None)
        if not realm:
            return None
        async with self._session.get(realm, params=params, timeout=TIMEOUT) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
        return data.get("token") or data.get("access_token")

    async def remote_digest(self, ref: str) -> str | None:
        """Return the digest the registry serves for this reference."""
        parsed = parse_reference(ref)
        if parsed is None:
            return None
        registry, repo, tag = parsed
        url = f"https://{registry}/v2/{repo}/manifests/{tag}"
        headers = {"Accept": ACCEPT}
        if token := self._tokens.get((registry, repo)):
            headers["Authorization"] = f"Bearer {token}"
        for _ in range(2):
            async with self._session.head(url, headers=headers, timeout=TIMEOUT) as resp:
                if resp.status == 401 and "Authorization" not in headers:
                    auth = resp.headers.get("WWW-Authenticate", "")
                    if not auth.lower().startswith("bearer"):
                        raise RegistryError(f"{registry}: authentication required")
                    token = await self._token(auth)
                    if not token:
                        raise RegistryError(f"{registry}: could not get token")
                    self._tokens[(registry, repo)] = token
                    headers["Authorization"] = f"Bearer {token}"
                    continue
                if resp.status == 401:
                    self._tokens.pop((registry, repo), None)
                if resp.status != 200:
                    raise RegistryError(f"{registry}/{repo}:{tag}: HTTP {resp.status}")
                return resp.headers.get("Docker-Content-Digest")
        return None


class RegistryError(Exception):
    """Registry lookup failed."""


async def check_digests(
    session: aiohttp.ClientSession, refs: set[str]
) -> dict[str, str | Exception | None]:
    """Look up many references with limited concurrency."""
    client = RegistryClient(session)
    sem = asyncio.Semaphore(4)
    results: dict[str, str | Exception | None] = {}

    async def one(ref: str) -> None:
        async with sem:
            try:
                results[ref] = await client.remote_digest(ref)
            except (aiohttp.ClientError, TimeoutError, RegistryError, ValueError) as err:
                _LOGGER.debug("Registry check failed for %s: %s", ref, err)
                results[ref] = err

    await asyncio.gather(*(one(r) for r in refs))
    return results
