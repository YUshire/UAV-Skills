"""Redirect hardcoded ``/tmp`` paths to a configured directory.

Agent-generated code very frequently hardcodes ``/tmp/...`` paths — e.g. it
calls ``write_file`` with ``path="/tmp/read_xlsx.py"`` and then ``exec`` runs
``python /tmp/read_xlsx.py``.  On shared machines we cannot bind-mount ``/tmp``
(it would affect every user) and this kernel forbids per-process mount
namespaces (``unshare`` fails), so we transparently rewrite ``/tmp`` references
at the tool layer instead.

Both the filesystem tools (main process) and the exec tool (subprocess) import
from here so that a *write* and a later *read/exec* of the same ``/tmp`` path
resolve to the same real file.  There is a single source of truth: the redirect
target set via :func:`set_redirect` (driven by ``exec.tmpdir`` config) or the
``NANOBOT_TMP_REDIRECT`` env override.
"""

import os
import re
from pathlib import Path

# Single source of truth for where /tmp is redirected. May be overridden per
# process via env, and is normally set from exec.tmpdir config at bot startup.
TMP_REDIRECT: str = os.environ.get(
    "NANOBOT_TMP_REDIRECT",
    "/data02/shengyang6/data/generation/tmp",
)


def set_redirect(target: str | None) -> None:
    """Point the /tmp redirect at *target* (from config). Empty/None disables."""
    global TMP_REDIRECT
    # An explicit env override always wins so ops can force a path.
    if os.environ.get("NANOBOT_TMP_REDIRECT"):
        return
    TMP_REDIRECT = target or ""


def _ensure_target() -> None:
    if TMP_REDIRECT:
        try:
            os.makedirs(TMP_REDIRECT, exist_ok=True)
        except OSError:
            pass


def remap_path(path: str) -> str:
    """Rewrite an absolute ``/tmp`` path to the redirect target.

    ``/tmp`` -> target, ``/tmp/x`` -> target/x. Everything else (including
    ``/var/tmp`` and ``/tmpfoo``) passes through untouched.
    """
    if not TMP_REDIRECT or not path:
        return path
    if path == "/tmp":
        _ensure_target()
        return TMP_REDIRECT
    if path.startswith("/tmp/"):
        _ensure_target()
        return str(Path(TMP_REDIRECT) / path[len("/tmp/"):])
    return path


# Match ``/tmp`` only as a whole path token: not preceded by a word char or
# ``/`` (so ``/var/tmp`` is skipped), and followed by ``/`` or a shell token
# boundary (so ``/tmpfoo`` is skipped).
_TMP_RE = re.compile(r"(?<![\w/])/tmp(?=/|[\s\"';:|&)>]|$)")


def remap_command(command: str) -> str:
    """Rewrite ``/tmp`` path tokens inside a shell command string."""
    if not TMP_REDIRECT or not command:
        return command
    if "/tmp" not in command:
        return command
    _ensure_target()
    return _TMP_RE.sub(TMP_REDIRECT, command)
