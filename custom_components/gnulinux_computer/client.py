# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eduard Vidal Tulsà
"""SSH client used to talk to the managed computer."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import shlex

import asyncssh

_LOGGER = logging.getLogger(__name__)

CONNECT_TIMEOUT = 10

# Every remote command runs under POSIX sh (the login shell may be zsh/fish)
# with a C locale so the output can be parsed. $SUDO / $SUDOFLAGS are defined
# by the preamble depending on how privilege escalation works for the user.
_PREAMBLE_ROOT = 'SUDO=""; SUDOFLAGS=""\n'
_PREAMBLE_NOPASSWD = 'SUDO="sudo -n"; SUDOFLAGS="-n"\n'
# The sudo password arrives on stdin and is handed to sudo through an askpass
# helper, so nested sudo calls (yay, paru...) can also use it.
_PREAMBLE_ASKPASS = """IFS= read -r GLC_PW; export GLC_PW
SUDO_ASKPASS=$(mktemp); export SUDO_ASKPASS
trap 'rm -f "$SUDO_ASKPASS"' EXIT
cat > "$SUDO_ASKPASS" <<'GLCEOF'
#!/bin/sh
printf '%s\\n' "$GLC_PW"
GLCEOF
chmod 700 "$SUDO_ASKPASS"
SUDO="sudo -A"; SUDOFLAGS="-A"
"""


class CannotConnect(Exception):
    """Error connecting to the computer."""


class CommandError(Exception):
    """A remote command failed."""


@dataclass
class RunResult:
    """Result of a remote command."""

    exit_status: int
    stdout: str | bytes
    stderr: str

    @property
    def ok(self) -> bool:
        """Return True if the command succeeded."""
        return self.exit_status == 0


def load_private_key(path: str) -> asyncssh.SSHKey:
    """Load a private key from disk (blocking)."""
    return asyncssh.read_private_key(path)


class LinuxClient:
    """Persistent SSH connection to a GNU/Linux computer."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        *,
        password: str | None = None,
        private_key: asyncssh.SSHKey | None = None,
        sudo_password: str | None = None,
        host_key: str | None = None,
    ) -> None:
        """Initialize the client."""
        self.host = host
        self.port = port
        self.username = username
        self._password = password
        self._private_key = private_key
        self._sudo_password = sudo_password or password
        self._host_key = host_key
        self._conn: asyncssh.SSHClientConnection | None = None
        self._lock = asyncio.Lock()
        # Set once we know whether sudo needs a password.
        self.sudo_nopasswd: bool | None = None

    @property
    def is_root(self) -> bool:
        """Return True if we log in as root."""
        return self.username == "root"

    async def _connect(self) -> asyncssh.SSHClientConnection:
        async with self._lock:
            if self._conn is not None and not self._conn.is_closed():
                return self._conn
            known_hosts = None
            if self._host_key:
                known_hosts = ([asyncssh.import_public_key(self._host_key)], [], [])
            try:
                self._conn = await asyncio.wait_for(
                    asyncssh.connect(
                        self.host,
                        port=self.port,
                        username=self.username,
                        password=self._password,
                        client_keys=[self._private_key] if self._private_key else (),
                        known_hosts=known_hosts,
                        agent_path=None,
                        config=None,
                        connect_timeout=CONNECT_TIMEOUT,
                        keepalive_interval=15,
                        keepalive_count_max=2,
                    ),
                    CONNECT_TIMEOUT + 5,
                )
            except (OSError, asyncssh.Error, TimeoutError) as err:
                self._conn = None
                raise CannotConnect(f"{self.host}: {err}") from err
            return self._conn

    async def close(self) -> None:
        """Close the connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _preamble(self) -> tuple[str, str | None]:
        if self.is_root:
            return _PREAMBLE_ROOT, None
        if self.sudo_nopasswd is False and self._sudo_password:
            return _PREAMBLE_ASKPASS, self._sudo_password + "\n"
        return _PREAMBLE_NOPASSWD, None

    async def run(
        self, script: str, *, timeout: float = 60, binary: bool = False
    ) -> RunResult:
        """Run a POSIX sh script on the computer."""
        if self.sudo_nopasswd is None and not self.is_root:
            await self.detect_sudo()
        preamble, stdin = self._preamble()
        full = "export LC_ALL=C LANG=C\n" + preamble + script
        command = "sh -c " + shlex.quote(full)
        conn = await self._connect()
        try:
            result = await conn.run(
                command,
                input=stdin.encode() if (binary and stdin) else stdin,
                check=False,
                timeout=timeout,
                encoding=None if binary else "utf-8",
                errors="replace",
            )
        except asyncssh.TimeoutError as err:
            raise CommandError(f"Timeout after {timeout}s") from err
        except (OSError, asyncssh.Error) as err:
            await self.close()
            raise CannotConnect(f"{self.host}: {err}") from err
        stderr = result.stderr or ""
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        return RunResult(
            exit_status=result.exit_status if result.exit_status is not None else -1,
            stdout=result.stdout if result.stdout is not None else (b"" if binary else ""),
            stderr=stderr,
        )

    async def run_raw(self, command: str, *, timeout: float = 30) -> RunResult:
        """Run a command as-is, without sh wrapper or sudo (e.g. RouterOS)."""
        conn = await self._connect()
        try:
            result = await conn.run(
                command, check=False, timeout=timeout, encoding="utf-8", errors="replace"
            )
        except asyncssh.TimeoutError as err:
            raise CommandError(f"Timeout after {timeout}s") from err
        except (OSError, asyncssh.Error) as err:
            await self.close()
            raise CannotConnect(f"{self.host}: {err}") from err
        return RunResult(
            exit_status=result.exit_status if result.exit_status is not None else -1,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
        )

    async def detect_sudo(self) -> bool:
        """Detect whether sudo works without a password. Return True if sudo works."""
        if self.is_root:
            self.sudo_nopasswd = True
            return True
        conn = await self._connect()
        try:
            res = await conn.run("sudo -n true", check=False, timeout=15)
        except (OSError, asyncssh.Error) as err:
            await self.close()
            raise CannotConnect(str(err)) from err
        if res.exit_status == 0:
            self.sudo_nopasswd = True
            return True
        self.sudo_nopasswd = False
        if not self._sudo_password:
            return False
        res = await conn.run(
            "sudo -S -p '' -k true", input=self._sudo_password + "\n", check=False, timeout=15
        )
        return res.exit_status == 0


async def fetch_host_key(host: str, port: int) -> str:
    """Return the server host key in OpenSSH format."""
    try:
        key = await asyncio.wait_for(
            asyncssh.get_server_host_key(host, port), CONNECT_TIMEOUT
        )
    except (OSError, asyncssh.Error, TimeoutError) as err:
        raise CannotConnect(str(err)) from err
    if key is None:
        raise CannotConnect("No host key")
    return key.export_public_key().decode().strip()
