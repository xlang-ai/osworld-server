import importlib.util
import os
from pathlib import Path

import pytest

_SOURCE = Path(__file__).parents[1] / "src/desktop_session.py"
_SPEC = importlib.util.spec_from_file_location("desktop_session_under_test", _SOURCE)
desktop_session = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(desktop_session)


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop_session, "_PROC", tmp_path)
    monkeypatch.setattr(desktop_session.platform, "system", lambda: "Linux")

    def create(pid, environment, executable="gnome-shell", uid=None):
        process = tmp_path / str(pid)
        process.mkdir()
        (process / "exe").symlink_to("/usr/bin/" + executable)
        uid = os.geteuid() if uid is None else uid
        (process / "status").write_text(f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        (process / "environ").write_bytes(
            b"\0".join(
                os.fsencode(f"{key}={value}") for key, value in environment.items()
            )
            + b"\0"
        )
        return process

    return create


def test_recovers_identity_without_copying_session_secrets(sessions):
    sessions(
        101,
        {
            "DISPLAY": ":0",
            "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
            "XDG_SESSION_TYPE": "x11",
            "TOKEN": "session-secret",
            "DBUS_SESSION_BUS_ADDRESS": "session-bus",
            "PATH": "/session/path",
        },
    )
    original = {"DISPLAY": ":0.0", "PATH": "/caller/path", "TOKEN": "caller-secret"}
    result = desktop_session.desktop_session_environment(original)
    assert result == dict(
        original, XDG_CURRENT_DESKTOP="ubuntu:GNOME", XDG_SESSION_TYPE="x11"
    )
    assert "XDG_CURRENT_DESKTOP" not in original


def test_preserves_nonempty_explicit_values_and_fills_empty_values(sessions):
    sessions(
        102,
        {"DISPLAY": ":0", "XDG_CURRENT_DESKTOP": "GNOME", "DESKTOP_SESSION": "ubuntu"},
    )
    result = desktop_session.desktop_session_environment(
        {"DISPLAY": ":0", "XDG_CURRENT_DESKTOP": "explicit", "DESKTOP_SESSION": ""}
    )
    assert result["XDG_CURRENT_DESKTOP"] == "explicit"
    assert result["DESKTOP_SESSION"] == "ubuntu"


def test_recovers_session_input_method_without_overriding_caller(sessions):
    sessions(
        111,
        {
            "DISPLAY": ":0",
            "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
            "GTK_IM_MODULE": "ibus",
            "QT_IM_MODULE": "ibus",
            "XMODIFIERS": "@im=ibus",
        },
    )
    result = desktop_session.desktop_session_environment(
        {"DISPLAY": ":0", "QT_IM_MODULE": "caller"}
    )
    assert result["GTK_IM_MODULE"] == "ibus"
    assert result["QT_IM_MODULE"] == "caller"
    assert result["XMODIFIERS"] == "@im=ibus"


@pytest.mark.parametrize("candidate", [":1", "localhost:0", ""])
def test_rejects_different_or_missing_session_display(sessions, candidate, caplog):
    sessions(103, {"DISPLAY": candidate, "XDG_CURRENT_DESKTOP": "wrong"})
    original = {"DISPLAY": ":0"}
    assert desktop_session.desktop_session_environment(original) == original
    assert "No same-user desktop session" in caplog.text


def test_missing_caller_display_never_guesses(sessions, caplog):
    sessions(104, {"DISPLAY": ":0", "XDG_CURRENT_DESKTOP": "GNOME"})
    assert desktop_session.desktop_session_environment({}) == {}
    assert "DISPLAY is missing" in caplog.text


def test_rejects_other_user_and_non_session_process(sessions):
    sessions(
        105,
        {"DISPLAY": ":0", "XDG_CURRENT_DESKTOP": "other-user"},
        uid=os.geteuid() + 1,
    )
    sessions(
        106, {"DISPLAY": ":0", "XDG_CURRENT_DESKTOP": "application"}, executable="gedit"
    )
    assert desktop_session.desktop_session_environment({"DISPLAY": ":0"}) == {
        "DISPLAY": ":0"
    }


def test_conflicting_sessions_do_not_guess_identity(sessions, caplog):
    for pid, desktop in [(107, "GNOME"), (108, "XFCE")]:
        sessions(
            pid,
            {
                "DISPLAY": ":0",
                "XDG_CURRENT_DESKTOP": desktop,
                "XDG_SESSION_TYPE": "x11",
            },
        )
    result = desktop_session.desktop_session_environment({"DISPLAY": ":0"})
    assert "XDG_CURRENT_DESKTOP" not in result
    assert result["XDG_SESSION_TYPE"] == "x11"
    assert "disagree" in caplog.text


def test_unreadable_process_does_not_hide_valid_session(sessions):
    vanished = sessions(109, {"DISPLAY": ":0"})
    (vanished / "environ").unlink()
    sessions(
        110,
        {"DISPLAY": ":0", "XDG_CURRENT_DESKTOP": "XFCE"},
        executable="xfce4-session",
    )
    assert (
        desktop_session.desktop_session_environment({"DISPLAY": ":0"})[
            "XDG_CURRENT_DESKTOP"
        ]
        == "XFCE"
    )


def test_non_linux_does_not_read_proc(monkeypatch, caplog):
    monkeypatch.setattr(desktop_session.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(desktop_session, "_PROC", Path("/does-not-exist"))
    original = {"DISPLAY": ":0", "PATH": "/bin"}
    assert desktop_session.desktop_session_environment(original) == original
    assert not caplog.records


def test_inaccessible_proc_is_observable(monkeypatch, caplog):
    monkeypatch.setattr(desktop_session.platform, "system", lambda: "Linux")
    monkeypatch.setattr(desktop_session, "_PROC", Path("/does-not-exist"))
    assert desktop_session.desktop_session_environment({"DISPLAY": ":0"}) == {
        "DISPLAY": ":0"
    }
    assert "cannot read proc" in caplog.text
