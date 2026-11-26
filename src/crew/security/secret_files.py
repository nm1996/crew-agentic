"""Filename patterns for secret-bearing files.

Two lists, because the consequences differ:

* ``SECRET_GLOBS`` (broad) is used to hide files from agent context and to deny
  ``Read`` access. A false positive only hides a file.
* ``COMMIT_BLOCK_GLOBS`` (narrow) is used to keep *newly created* files out of
  CREW commits. A false positive would drop real work, so it lists only
  unambiguous key/credential formats.
"""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import PurePosixPath

SECRET_GLOBS: tuple[str, ...] = (
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/*.pfx",
    "**/*.kdbx",
    "**/id_rsa*",
    "**/id_ed25519*",
    "**/.netrc",
    "**/.npmrc",
    "**/.pypirc",
    "**/credentials*",
    "**/*secret*",
)

COMMIT_BLOCK_GLOBS: tuple[str, ...] = (
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/*.pfx",
    "**/*.kdbx",
    "**/id_rsa",
    "**/id_ed25519",
    "**/.netrc",
    "**/.pypirc",
)

# .env.example and friends are conventional, safe templates.
_TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist")


def matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    name = PurePosixPath(path).name
    for pattern in patterns:
        bare = pattern.removeprefix("**/")
        if fnmatch(path, pattern) or fnmatch(name, bare) or fnmatch(path, bare):
            return True
    return False


def is_excluded(path: str, extra: tuple[str, ...] = ()) -> bool:
    """True if ``path`` must be hidden from agents."""
    return matches_any(path, (*SECRET_GLOBS, *extra))


def is_commit_blocked(path: str) -> bool:
    """True if a newly created file must not be committed by CREW."""
    if path.endswith(_TEMPLATE_SUFFIXES):
        return False
    return matches_any(path, COMMIT_BLOCK_GLOBS)
