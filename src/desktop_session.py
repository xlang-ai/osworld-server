"""Recover desktop identity missing from service-launched application environments."""

import logging
import os
import platform
import re
from pathlib import Path
from typing import Mapping, Optional

_PROC = Path("/proc")
_SESSION_EXECUTABLES = {
    "gnome-shell",
    "gnome-session",
    "gnome-session-binary",
    "xfce4-session",
    "plasmashell",
    "ksmserver",
    "mate-session",
    "cinnamon-session",
    "lxqt-session",
    "lxsession",
}
_DESKTOP_KEYS = (
    "XDG_CURRENT_DESKTOP",
    "XDG_SESSION_DESKTOP",
    "DESKTOP_SESSION",
    "XDG_SESSION_TYPE",
    "GDMSESSION",
    "GTK_IM_MODULE",
    "QT_IM_MODULE",
    "XMODIFIERS",
)


def _display_server(value: str) -> str:
    # X11 screens on the same display share the desktop session.
    return re.sub(r"(:\d+)\.\d+$", r"\1", value)


def desktop_session_environment(
    environ: Optional[Mapping[str, str]] = None,
    logger: Optional[logging.Logger] = None,
) -> dict:
    """Fill missing identity keys from same-user sessions on the selected display.

    Existing values take precedence. Never inherit credentials, bus addresses,
    or arbitrary environment variables from another process.
    """
    result = dict(os.environ if environ is None else environ)
    missing = [key for key in _DESKTOP_KEYS if not result.get(key)]
    if platform.system() != "Linux" or not missing:
        return result

    log = logger if logger is not None else logging.getLogger(__name__)
    display = result.get("DISPLAY", "")
    if not display:
        log.warning("Desktop environment discovery skipped: DISPLAY is missing")
        return result

    values = {key: set() for key in missing}
    try:
        processes = list(_PROC.iterdir())
    except OSError as exc:
        log.warning("Desktop environment discovery cannot read proc: %s", exc)
        return result

    unreadable = 0
    matched = 0
    uid = os.geteuid()
    for process in processes:
        if not process.name.isdecimal():
            continue
        try:
            if process.stat().st_uid != uid:
                continue
            executable = Path(os.readlink(process / "exe")).name
            if executable not in _SESSION_EXECUTABLES:
                continue
            status = (process / "status").read_text()
            uid_line = next(
                (
                    line.split()[1:]
                    for line in status.splitlines()
                    if line.startswith("Uid:")
                ),
                [],
            )
            if len(uid_line) < 2 or any(value != str(uid) for value in uid_line[:2]):
                continue
            raw = (process / "environ").read_bytes()
            session = {}
            for entry in raw.split(b"\0"):
                key, sep, value = entry.partition(b"=")
                if sep:
                    session[os.fsdecode(key)] = os.fsdecode(value)
            session_display = session.get("DISPLAY", "")
            if not session_display or _display_server(
                session_display
            ) != _display_server(display):
                continue
            matched += 1
            for key in missing:
                if session.get(key):
                    values[key].add(session[key])
        except OSError:
            # Processes may exit during discovery, or proc access may be restricted.
            unreadable += 1

    conflicts = []
    for key, options in values.items():
        if len(options) == 1:
            result[key] = next(iter(options))
        elif len(options) > 1:
            conflicts.append(key)
    if conflicts:
        log.warning(
            "Desktop sessions disagree on identity keys: %s", ", ".join(conflicts)
        )
    if not matched:
        log.warning(
            "No same-user desktop session found for DISPLAY=%s (%d unreadable processes)",
            display,
            unreadable,
        )
    elif not result.get("XDG_CURRENT_DESKTOP"):
        log.warning("Desktop session has no unambiguous XDG_CURRENT_DESKTOP")
    return result
