"""Repository reference parsing: exact GitHub hosts, slugs, and path-safe components."""

from __future__ import annotations

import pytest

from app.droid_client import review_workspace_dir, task_workspace_dir, workspace_root
from app.repos import parse_repository_ref


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://github.com/acme/widgets", "acme/widgets"),
        ("https://github.com/acme/widgets/", "acme/widgets"),
        ("https://github.com/acme/widgets.git", "acme/widgets"),
        ("http://github.com/acme/widgets", "acme/widgets"),
        ("https://www.github.com/acme/widgets.git", "acme/widgets"),
        ("HTTPS://GitHub.COM/acme/widgets", "acme/widgets"),
        ("https://WWW.GITHUB.COM/acme/widgets", "acme/widgets"),
        ("github.com/acme/widgets", "acme/widgets"),
        ("GitHub.com/acme/widgets.git", "acme/widgets"),
        ("www.github.com/acme/widgets", "acme/widgets"),
        ("git@github.com:acme/widgets.git", "acme/widgets"),
        ("git@GitHub.com:acme/widgets", "acme/widgets"),
        ("ssh://git@github.com/acme/widgets.git", "acme/widgets"),
        ("acme/widgets", "acme/widgets"),
        ("acme/widgets/", "acme/widgets"),
        ("  acme/widgets  ", "acme/widgets"),
        ("acme/widgets.git", "acme/widgets.git"),
        ("acme/.github", "acme/.github"),
        ("github.com/a/.git", "a/.git"),
        ("https://github.com/a/.git", "a/.git"),
        ("acme/my.repo-name_2", "acme/my.repo-name_2"),
        ("www.example/widgets", "www.example/widgets"),
    ],
)
def test_supported_forms_parse_consistently(value: str, expected: str) -> None:
    assert parse_repository_ref(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "not a repo",
        "acme",
        "github.com/acme",
        "https://github.com/acme",
        "www.github.com/acme",
        "github.com/a/b/c",
        "https://github.com/a/b/c",
        "https://github.com//acme/widgets",
        "acme//widgets",
    ],
)
def test_malformed_references_reject(value: str) -> None:
    assert parse_repository_ref(value) is None


@pytest.mark.parametrize(
    "value",
    [
        "../widgets",
        "acme/..",
        "./widgets",
        "acme/.",
        "../..",
        "https://github.com/../widgets",
        "https://github.com/acme/..",
        "https://github.com/acme/..git",
        "github.com/../widgets",
        "github.com/acme/.",
        "git@github.com:../widgets.git",
        "ssh://git@github.com/acme/..",
        "acme/%2e%2e",
        "https://github.com/acme/%2e%2e",
    ],
)
def test_dot_segment_components_reject(value: str) -> None:
    assert parse_repository_ref(value) is None


@pytest.mark.parametrize(
    "value",
    [
        "https://github.com.evil.example/acme/widgets",
        "https://evilgithub.com/acme/widgets",
        "https://github.com@evil.example/acme/widgets",
        "https://evil.example/github.com/acme/widgets",
        "https://evil.example/?github.com/acme/widgets",
        "https://evil.example#@github.com/acme/widgets",
        "https://evil.example\\@github.com/acme/widgets",
        "https://user:secret@github.com/acme/widgets",
        "https://token@github.com/acme/widgets",
        "ssh://git:secret@github.com/acme/widgets",
        "ssh://root@github.com/acme/widgets",
        "git@evil.example:acme/widgets.git",
        "root@github.com:acme/widgets.git",
        "git@evil.example@github.com:acme/widgets",
        "https://github.com:8443/acme/widgets",
        "https://git\thub.com/acme/widgets",
        "https://git\nhub.com/acme/widgets",
        "ftp://github.com/acme/widgets",
        "file://github.com/acme/widgets",
        "https://gist.github.com/acme/widgets",
        "https://github.com/acme/widgets?tab=readme",
        "https://github.com/acme/widgets#readme",
        "evil.example/acme/widgets",
        "github.com.evil.example/acme/widgets",
        "github.com:acme/widgets",
    ],
)
def test_deceptive_hosts_and_userinfo_reject(value: str) -> None:
    assert parse_repository_ref(value) is None


def test_adversarial_input_is_rejected_quickly() -> None:
    import time

    started = time.perf_counter()
    assert parse_repository_ref("https://github.com/a/" + "-" * 20_000 + "!") is None
    assert parse_repository_ref("git@github.com:" + "/" * 20_000) is None
    assert time.perf_counter() - started < 1


@pytest.mark.parametrize(
    "value",
    [
        "acme/widgets",
        "https://GITHUB.com/acme/widgets.git",
        "git@github.com:acme/.github.git",
        "acme/.git",
    ],
)
def test_accepted_references_stay_inside_workspace_root(value: str) -> None:
    full_name = parse_repository_ref(value)
    assert full_name is not None
    root = workspace_root()
    for directory in (task_workspace_dir(full_name, 1), review_workspace_dir(full_name, 1)):
        resolved = directory.resolve()
        assert resolved.is_relative_to(root)
        assert resolved.parent.parent.parent == root
