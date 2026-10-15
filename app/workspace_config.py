"""Workspace-local Git configuration checks: commit identity and legacy credentials.

Pure planning helpers; ``app.droid_client`` runs the resulting Git commands.

Earlier versions stored a token in a workspace's ``.git/config`` (for example a
``https://x-access-token:<token>@github.com/...`` origin). A clean agent
environment does not remove credentials already on disk, so before an existing
workspace is resumed its local configuration is rewritten from the task's
repository: origin becomes the canonical credential-free URL, userinfo is
stripped from other remote URLs, and auth headers, URL rewrites carrying
credentials and local credential helpers are removed. Nothing outside the
local config file is touched, and the result is re-read and verified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.redaction import redact_text

_URL_USERINFO = re.compile(r"(?i)\b((?:https?|ssh|git)://)[^/\s@]+@")
_URL_KEYS = frozenset({"url", "pushurl"})
IDENTITY_VARS = ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT")
_IDENT = re.compile(r"^(?P<name>.*?) <(?P<email>[^>]*)> \d+ [+-]\d{4}$")
_SECTION_HEADER = re.compile(r"^\s*\[([^\]]*)\]")


@dataclass(frozen=True)
class ConfigEntry:
    key: str
    value: str


def parse_config_list(output: str) -> list[ConfigEntry]:
    """Parse ``git config --null --list`` output (``key\\nvalue`` records separated by NUL)."""
    entries = []
    for record in output.split("\0"):
        if not record.strip():
            continue
        key, _sep, value = record.partition("\n")
        entries.append(ConfigEntry(key.strip(), value))
    return entries


def _split_key(key: str) -> tuple[str, str | None, str]:
    """``section.sub.section.variable`` -> (section, subsection or None, variable)."""
    section, _, rest = key.partition(".")
    subsection, dot, variable = rest.rpartition(".")
    if not dot:
        return section.lower(), None, rest.lower()
    return section.lower(), subsection, variable.lower()


def strip_userinfo(url: str) -> str:
    return _URL_USERINFO.sub(lambda m: m.group(1), url)


def carries_credentials(text: str) -> bool:
    """URL userinfo, a known configured secret, or any credential-shaped substring."""
    return bool(_URL_USERINFO.search(text)) or redact_text(text) != text


def credential_findings(entries: list[ConfigEntry], canonical_remote: str) -> list[str]:
    """Keys that still hold credentials (or a non-canonical origin); empty when clean."""
    found = []
    for entry in entries:
        section, subsection, variable = _split_key(entry.key)
        if entry.key.lower() == "remote.origin.url":
            if entry.value != canonical_remote:
                found.append(entry.key)
        elif (
            section == "credential"
            or variable == "extraheader"
            or carries_credentials(entry.value)
            or (subsection is not None and carries_credentials(subsection))
        ):
            found.append(entry.key)
    return found


def strip_credential_sections(text: str) -> str:
    """Drop config sections whose header itself carries credentials (e.g. ``[url "https://u:t@host/"]``).

    Done on the file text so the credential never appears in a Git command line.
    """
    kept: list[str] = []
    skipping = False
    for line in text.splitlines(keepends=True):
        header = _SECTION_HEADER.match(line)
        if header:
            skipping = carries_credentials(header.group(1))
        if not skipping:
            kept.append(line)
    return "".join(kept)


def credential_cleanup_commands(entries: list[ConfigEntry], canonical_remote: str) -> list[list[str]]:
    """``git config --local`` commands that remove credentials while keeping all other settings.

    Sections whose header carries credentials are handled by ``strip_credential_sections``.
    """
    grouped: dict[str, list[str]] = {}
    for entry in entries:
        grouped.setdefault(entry.key, []).append(entry.value)

    sections: list[str] = []
    commands: list[list[str]] = []
    for key, values in grouped.items():
        section, subsection, variable = _split_key(key)
        if subsection is not None and carries_credentials(subsection):
            continue
        if key.lower() == "remote.origin.url":
            if values != [canonical_remote]:
                commands.append(["config", "--local", "--replace-all", key, canonical_remote])
            continue
        if section == "credential":
            name = section if subsection is None else f"{section}.{subsection}"
            if name not in sections:
                sections.append(name)
            continue
        if variable != "extraheader" and not any(carries_credentials(value) for value in values):
            continue
        commands.append(["config", "--local", "--unset-all", key])
        if section in {"remote", "submodule"} and variable in _URL_KEYS:
            cleaned = [strip_userinfo(value) for value in values]
            if not any(carries_credentials(value) for value in cleaned):
                commands.extend(["config", "--local", "--add", key, value] for value in cleaned)
    return [["config", "--local", "--remove-section", name] for name in sections] + commands


def usable_identity(ident: str) -> bool:
    """True for a ``git var GIT_*_IDENT`` line with a non-blank name and email."""
    match = _IDENT.match(ident.strip())
    return bool(match and match.group("name").strip() and match.group("email").strip())
