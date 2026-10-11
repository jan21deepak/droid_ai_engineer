"""Clean child environments for agent sessions, setup commands and app-owned Git.

The service process holds publishing and operator credentials (GitHub token,
webhook secret, operator token). None of them may reach a Droid agent or the
commands it runs:

- ``clean_child_environment`` builds an allowlisted environment. Anything not
  named here is dropped, so new secrets are excluded by default.
- The SDK's ``Runtime.env`` is *merged into* ``os.environ`` and cannot remove
  variables, so agent sessions are spawned through ``agent_launcher.py``,
  which replaces itself (``execve``) with ``droid`` keeping only allowlisted
  names. The connected transport is handed to the SDK via
  ``Runtime(transport=...)``.
- App-owned Git operations get the GitHub credential transiently through
  process-scoped ``GIT_CONFIG_*`` variables: never in a remote URL, command
  line, ``.git/config`` or a credential helper, and scoped to one repository URL.

This keeps credentials out of the agent's reach through its environment and
Git configuration. It is not a sandbox: the agent still runs as the same OS
account and can read whatever that account can (for example other local
credential stores). Forge assumes a trusted workload.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

from droid_sdk import Runtime
from droid_sdk.transport import ProcessTransport

from app.redaction import git_basic_auth_value

LAUNCHER_PATH = Path(__file__).resolve().with_name("agent_launcher.py")

# SSH_AUTH_SOCK, GIT_ASKPASS and credential-helper settings are deliberately
# absent: each would hand the agent a way to publish.
SAFE_ENV_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LANGUAGE",
        "TERM",
        "COLORTERM",
        "TZ",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "all_proxy",
        # Existing Git identity is preserved rather than invented.
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "EMAIL",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_NOSYSTEM",
    }
)
SAFE_ENV_PREFIXES = ("LC_", "XDG_")
# Producer attribution the SDK adds to every droid process.
SDK_ATTRIBUTION_NAMES = ("FACTORY_UPSTREAM_CLIENT_TYPE", "FACTORY_UPSTREAM_SDK")
FACTORY_API_KEY_NAME = "FACTORY_API_KEY"
DROID_EXEC_ARGS = ("exec", "--input-format", "stream-jsonrpc", "--output-format", "stream-jsonrpc")


def _is_safe_name(name: str) -> bool:
    return name in SAFE_ENV_NAMES or name.startswith(SAFE_ENV_PREFIXES)


def _config_entries(entries: list[tuple[str, str]]) -> dict[str, str]:
    env = {"GIT_CONFIG_COUNT": str(len(entries))}
    for index, (key, value) in enumerate(entries):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def clean_child_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Allowlisted copy of ``source`` (default ``os.environ``) with no credentials."""
    origin = os.environ if source is None else source
    return {name: value for name, value in origin.items() if _is_safe_name(name)}


def agent_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for agent sessions and setup commands.

    Credential helpers are reset so Git inside the agent cannot borrow stored
    credentials through the inherited global configuration, and Git never
    prompts.
    """
    env = clean_child_environment(source)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.update(_config_entries([("credential.helper", "")]))
    return env


def git_environment(
    *,
    token: str | None = None,
    credential_url: str | None = None,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Environment for one app-owned Git command.

    Hooks and fsmonitor are disabled because the agent can write them into the
    workspace and they would otherwise run with this process's credential.
    With ``token``, an ``Authorization`` header is scoped to ``credential_url``.
    """
    env = clean_child_environment(source)
    env["GIT_TERMINAL_PROMPT"] = "0"
    entries = [("core.hooksPath", os.devnull), ("core.fsmonitor", "false")]
    if token and credential_url:
        entries.append(("credential.helper", ""))
        entries.append((f"http.{credential_url}.extraheader", f"AUTHORIZATION: basic {git_basic_auth_value(token)}"))
    env.update(_config_entries(entries))
    return env


def resolve_droid_executable() -> str | None:
    return shutil.which("droid")


def build_agent_transport(*, cwd: Path, api_key: str | None, droid_path: str) -> ProcessTransport:
    """A ``droid exec`` transport whose child environment is rebuilt from the allowlist.

    ``ProcessTransport`` passes ``os.environ`` to the launcher, which then
    replaces itself with droid keeping only the names listed on its command line.
    """
    extra = agent_environment()
    if api_key:
        extra[FACTORY_API_KEY_NAME] = api_key
    keep = sorted({*extra, *SDK_ATTRIBUTION_NAMES})
    return ProcessTransport(
        exec_path=sys.executable,
        exec_args=["-I", str(LAUNCHER_PATH), *keep, "--", droid_path, *DROID_EXEC_ARGS],
        cwd=str(cwd),
        env=extra,
    )


async def open_agent_runtime(*, cwd: Path, api_key: str | None) -> Runtime:
    """Spawn a clean-environment droid process and wrap it as an SDK runtime."""
    droid_path = resolve_droid_executable()
    if not droid_path:
        raise FileNotFoundError("droid CLI not found on PATH")
    cwd.mkdir(parents=True, exist_ok=True)
    transport = build_agent_transport(cwd=cwd, api_key=api_key, droid_path=droid_path)
    await transport.connect()
    return Runtime(transport=transport)


async def close_runtime(runtime: Runtime | None) -> None:
    transport = runtime.transport if runtime is not None else None
    if transport is not None:
        await transport.close()
