import shlex
import subprocess
import time
from typing import Any, Dict, Tuple

from .platform_runtime import subprocess_creation_flags, get_pyautogui


def extract_action_parameters(action: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    if not isinstance(action, dict):
        raise ValueError("Action payload must be a dictionary.")

    action_type = action.get("action_type")
    if not action_type:
        raise ValueError("Action payload must include 'action_type'.")

    if "parameters" in action and isinstance(action["parameters"], dict):
        parameters = dict(action["parameters"])
    else:
        parameters = {key: value for key, value in action.items() if key != "action_type"}

    return str(action_type), parameters


def execute_stream_action(action: Dict[str, Any]) -> Dict[str, Any]:
    action_type, parameters = extract_action_parameters(action)
    started_at = time.time()
    pyautogui = get_pyautogui()
    keyboard_keys = list(pyautogui.KEYBOARD_KEYS)

    if action_type in {"WAIT", "FAIL", "DONE"}:
        return {
            "status": "success",
            "started_at": started_at,
            "completed_at": time.time(),
            "result": {"noop": True},
        }

    if action_type == "MOVE_TO":
        if parameters:
            if "x" not in parameters or "y" not in parameters:
                raise ValueError(f"Unknown parameters: {parameters}")
            pyautogui.moveTo(parameters["x"], parameters["y"])
        else:
            pyautogui.moveTo()

    elif action_type == "CLICK":
        click_kwargs: Dict[str, Any] = {}
        if "button" in parameters:
            click_kwargs["button"] = parameters["button"]
        if "x" in parameters and "y" in parameters:
            click_kwargs["x"] = parameters["x"]
            click_kwargs["y"] = parameters["y"]
        elif "x" in parameters or "y" in parameters:
            raise ValueError(f"Unknown parameters: {parameters}")
        if "num_clicks" in parameters:
            click_kwargs["clicks"] = parameters["num_clicks"]
        pyautogui.click(**click_kwargs)

    elif action_type == "MOUSE_DOWN":
        mouse_down_kwargs = {"button": parameters["button"]} if "button" in parameters else {}
        pyautogui.mouseDown(**mouse_down_kwargs)

    elif action_type == "MOUSE_UP":
        mouse_up_kwargs = {"button": parameters["button"]} if "button" in parameters else {}
        pyautogui.mouseUp(**mouse_up_kwargs)

    elif action_type == "RIGHT_CLICK":
        right_click_kwargs: Dict[str, Any] = {}
        if "x" in parameters and "y" in parameters:
            right_click_kwargs["x"] = parameters["x"]
            right_click_kwargs["y"] = parameters["y"]
        elif "x" in parameters or "y" in parameters:
            raise ValueError(f"Unknown parameters: {parameters}")
        pyautogui.rightClick(**right_click_kwargs)

    elif action_type == "DOUBLE_CLICK":
        double_click_kwargs: Dict[str, Any] = {}
        if "x" in parameters and "y" in parameters:
            double_click_kwargs["x"] = parameters["x"]
            double_click_kwargs["y"] = parameters["y"]
        elif "x" in parameters or "y" in parameters:
            raise ValueError(f"Unknown parameters: {parameters}")
        pyautogui.doubleClick(**double_click_kwargs)

    elif action_type == "DRAG_TO":
        if "x" not in parameters or "y" not in parameters:
            raise ValueError(f"Unknown parameters: {parameters}")
        pyautogui.dragTo(
            parameters["x"],
            parameters["y"],
            duration=parameters.get("duration", 1.0),
            button=parameters.get("button", "left"),
            mouseDownUp=True,
        )

    elif action_type == "SCROLL":
        if "dx" in parameters:
            pyautogui.hscroll(parameters["dx"])
        if "dy" in parameters:
            pyautogui.vscroll(parameters["dy"])
        if "dx" not in parameters and "dy" not in parameters:
            raise ValueError(f"Unknown parameters: {parameters}")

    elif action_type == "TYPING":
        if "text" not in parameters:
            raise ValueError(f"Unknown parameters: {parameters}")
        pyautogui.typewrite(str(parameters["text"]), interval=parameters.get("interval", 0.0))

    elif action_type == "PRESS":
        key = str(parameters.get("key", ""))
        if key.lower() not in keyboard_keys:
            raise ValueError(f"Key must be one of {keyboard_keys}")
        pyautogui.press(key)

    elif action_type == "KEY_DOWN":
        key = str(parameters.get("key", ""))
        if key.lower() not in keyboard_keys:
            raise ValueError(f"Key must be one of {keyboard_keys}")
        pyautogui.keyDown(key)

    elif action_type == "KEY_UP":
        key = str(parameters.get("key", ""))
        if key.lower() not in keyboard_keys:
            raise ValueError(f"Key must be one of {keyboard_keys}")
        pyautogui.keyUp(key)

    elif action_type == "HOTKEY":
        keys = parameters.get("keys")
        if not isinstance(keys, list) or not keys:
            raise ValueError("Keys must be a non-empty list of keys")
        for key in keys:
            if str(key).lower() not in keyboard_keys:
                raise ValueError(f"Key must be one of {keyboard_keys}")
        pyautogui.hotkey(*keys)

    elif action_type == "EXECUTE":
        command = parameters.get("command")
        if command is None:
            raise ValueError(f"Unknown parameters: {parameters}")
        timeout = parameters.get("timeout", 30)
        shell = parameters.get("shell", isinstance(command, str))
        working_dir = parameters.get("working_dir")

        if isinstance(command, str) and not shell:
            command = shlex.split(command)

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=shell,
            text=True,
            timeout=timeout,
            creationflags=subprocess_creation_flags(),
            cwd=working_dir,
        )
        return {
            "status": "success" if result.returncode == 0 else "error",
            "started_at": started_at,
            "completed_at": time.time(),
            "result": {
                "output": result.stdout,
                "error": result.stderr,
                "returncode": result.returncode,
            },
        }

    else:
        raise ValueError(f"Unknown action type: {action_type}")

    return {
        "status": "success",
        "started_at": started_at,
        "completed_at": time.time(),
        "result": {},
    }
