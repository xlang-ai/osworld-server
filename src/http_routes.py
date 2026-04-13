import ctypes
import io
import os
import platform
import shlex
import subprocess
import tempfile
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, List, Optional

import requests
import Xlib
from Xlib import X
from flask import Flask, abort, jsonify, request, send_file

from .accessibility import build_accessibility_tree, get_terminal_output
from .capture import capture_screen_image, encode_image_bytes
from .platform_runtime import (
    TIMEOUT,
    build_ffmpeg_capture_input_args,
    get_machine_architecture,
    get_screen_size,
    managed_x_display,
    platform_name,
    subprocess_creation_flags,
)

recording_process = None
recording_path = str(Path(tempfile.gettempdir()) / "osworld-server-recording.mp4")


def _append_event(*args, **kwargs) -> None:
    return None


def register_http_routes(app: Flask) -> None:
    @app.route("/setup/execute", methods=["POST"])
    @app.route("/execute", methods=["POST"])
    def execute_command():
        data = request.json
        shell = data.get("shell", False)
        command = data.get("command", "" if shell else [])
        timeout = data.get("timeout", 120)

        if isinstance(command, str) and not shell:
            command = shlex.split(command)

        for i, arg in enumerate(command):
            if arg.startswith("~/"):
                command[i] = os.path.expanduser(arg)

        try:
            flags = subprocess.CREATE_NO_WINDOW if platform_name == "Windows" else 0
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=shell,
                text=True,
                timeout=timeout,
                creationflags=flags,
            )
            return jsonify(
                {
                    "status": "success",
                    "output": result.stdout,
                    "error": result.stderr,
                    "returncode": result.returncode,
                }
            )
        except Exception as exc:
            return jsonify({"status": "error", "message": str(exc)}), 500

    @app.route("/setup/execute_with_verification", methods=["POST"])
    @app.route("/execute_with_verification", methods=["POST"])
    def execute_command_with_verification():
        data = request.json
        shell = data.get("shell", False)
        command = data.get("command", "" if shell else [])
        verification = data.get("verification", {})
        max_wait_time = data.get("max_wait_time", 10)
        check_interval = data.get("check_interval", 1)

        if isinstance(command, str) and not shell:
            command = shlex.split(command)

        for i, arg in enumerate(command):
            if arg.startswith("~/"):
                command[i] = os.path.expanduser(arg)

        try:
            flags = subprocess.CREATE_NO_WINDOW if platform_name == "Windows" else 0
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=shell,
                text=True,
                timeout=120,
                creationflags=flags,
            )

            if not verification:
                return jsonify(
                    {
                        "status": "success",
                        "output": result.stdout,
                        "error": result.stderr,
                        "returncode": result.returncode,
                    }
                )

            start_time = time.time()
            while time.time() - start_time < max_wait_time:
                verification_passed = True

                if "window_exists" in verification:
                    window_name = verification["window_exists"]
                    try:
                        if platform_name == "Linux":
                            wmctrl_result = subprocess.run(
                                ["wmctrl", "-l"],
                                capture_output=True,
                                text=True,
                                check=True,
                            )
                            if window_name.lower() not in wmctrl_result.stdout.lower():
                                verification_passed = False
                        elif platform_name in ["Windows", "Darwin"]:
                            import pygetwindow as gw

                            windows = gw.getWindowsWithTitle(window_name)
                            if not windows:
                                verification_passed = False
                    except Exception:
                        verification_passed = False

                if "command_success" in verification:
                    verify_cmd = verification["command_success"]
                    try:
                        verify_result = subprocess.run(
                            verify_cmd,
                            shell=True,
                            capture_output=True,
                            text=True,
                            timeout=5,
                        )
                        if verify_result.returncode != 0:
                            verification_passed = False
                    except Exception:
                        verification_passed = False

                if verification_passed:
                    return jsonify(
                        {
                            "status": "success",
                            "output": result.stdout,
                            "error": result.stderr,
                            "returncode": result.returncode,
                            "verification": "passed",
                            "wait_time": time.time() - start_time,
                        }
                    )

                time.sleep(check_interval)

            return jsonify(
                {
                    "status": "verification_failed",
                    "output": result.stdout,
                    "error": result.stderr,
                    "returncode": result.returncode,
                    "verification": "failed",
                    "wait_time": max_wait_time,
                }
            ), 500
        except Exception as exc:
            return jsonify({"status": "error", "message": str(exc)}), 500

    @app.route("/setup/launch", methods=["POST"])
    def launch_app():
        data = request.json
        shell = data.get("shell", False)
        command: List[str] = data.get("command", "" if shell else [])

        if isinstance(command, str) and not shell:
            command = shlex.split(command)

        for i, arg in enumerate(command):
            if arg.startswith("~/"):
                command[i] = os.path.expanduser(arg)

        try:
            if "google-chrome" in command and get_machine_architecture() == "arm":
                index = command.index("google-chrome")
                command[index] = "chromium"
            subprocess.Popen(command, shell=shell)
            return f"{command if shell else ' '.join(command)} launched successfully"
        except Exception as exc:
            return jsonify({"status": "error", "message": str(exc)}), 500

    @app.route("/screenshot", methods=["GET"])
    def capture_screen_with_cursor():
        image = capture_screen_image()
        image_bytes, _ = encode_image_bytes(image, image_format="PNG")
        return send_file(io.BytesIO(image_bytes), mimetype="image/png", download_name="screenshot.png")

    @app.route("/terminal", methods=["GET"])
    def terminal_output():
        try:
            output = get_terminal_output()
            return jsonify({"output": output, "status": "success"})
        except NotImplementedError as exc:
            return str(exc), 500
        except Exception as exc:
            app.logger.error("Failed to get terminal output. Error: %s", exc)
            return jsonify({"status": "error", "message": str(exc)}), 500

    @app.route("/accessibility", methods=["GET"])
    def accessibility_tree():
        try:
            return jsonify({"AT": build_accessibility_tree()})
        except NotImplementedError as exc:
            return str(exc), 500
        except Exception as exc:
            app.logger.error("Failed to get accessibility tree. Error: %s", exc)
            return jsonify({"status": "error", "message": str(exc)}), 500

    @app.route("/screen_size", methods=["POST"])
    def screen_size():
        width, height = get_screen_size()
        return jsonify({"width": width, "height": height})

    @app.route("/window_size", methods=["POST"])
    def window_size():
        if "app_class_name" in request.form:
            app_class_name = request.form["app_class_name"]
        else:
            return jsonify({"error": "app_class_name is required"}), 400

        with managed_x_display() as d:
            root = d.screen().root
            window_ids = root.get_full_property(d.intern_atom("_NET_CLIENT_LIST"), X.AnyPropertyType).value

            for window_id in window_ids:
                try:
                    window = d.create_resource_object("window", window_id)
                    wm_class = window.get_wm_class()

                    if wm_class is None:
                        continue

                    if app_class_name.lower() in [name.lower() for name in wm_class]:
                        geom = window.get_geometry()
                        return jsonify({"width": geom.width, "height": geom.height})
                except Xlib.error.XError:
                    continue
        return None

    @app.route("/desktop_path", methods=["POST"])
    def desktop_path():
        home_directory = str(Path.home())
        desktop_path_value = {
            "Windows": os.path.join(home_directory, "Desktop"),
            "Darwin": os.path.join(home_directory, "Desktop"),
            "Linux": os.path.join(home_directory, "Desktop"),
        }.get(platform.system(), None)

        if desktop_path_value and os.path.exists(desktop_path_value):
            return jsonify(desktop_path=desktop_path_value)
        return jsonify(error="Unsupported operating system or desktop path not found"), 404

    @app.route("/wallpaper", methods=["POST"])
    def wallpaper():
        def get_wallpaper_windows():
            spi_get_deskwallpaper = 0x73
            max_path = 260
            buffer = ctypes.create_unicode_buffer(max_path)
            ctypes.windll.user32.SystemParametersInfoW(spi_get_deskwallpaper, max_path, buffer, 0)
            return buffer.value

        def get_wallpaper_macos():
            script = """
            tell application "System Events" to tell every desktop to get picture
            """
            process = subprocess.Popen(["osascript", "-e", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            output, error = process.communicate()
            if error:
                app.logger.error("Error: %s", error.decode("utf-8"))
                return None
            return output.strip().decode("utf-8")

        def get_wallpaper_linux():
            try:
                output = subprocess.check_output(
                    ["gsettings", "get", "org.gnome.desktop.background", "picture-uri"],
                    stderr=subprocess.PIPE,
                )
                return output.decode("utf-8").strip().replace("file://", "").replace("'", "")
            except subprocess.CalledProcessError as exc:
                app.logger.error("Error: %s", exc)
                return None

        os_name = platform.system()
        wallpaper_path = None
        if os_name == "Windows":
            wallpaper_path = get_wallpaper_windows()
        elif os_name == "Darwin":
            wallpaper_path = get_wallpaper_macos()
        elif os_name == "Linux":
            wallpaper_path = get_wallpaper_linux()
        else:
            app.logger.error("Unsupported OS: %s", os_name)
            abort(400, description="Unsupported OS")

        if wallpaper_path:
            try:
                return send_file(wallpaper_path, mimetype="image/png")
            except Exception as exc:
                app.logger.error("An error occurred while serving the wallpaper file: %s", exc)
                abort(500, description="Unable to serve the wallpaper file")
        else:
            abort(404, description="Wallpaper file not found")

    @app.route("/list_directory", methods=["POST"])
    def directory_tree():
        def list_dir_contents(directory):
            tree = {"type": "directory", "name": os.path.basename(directory), "children": []}
            try:
                for entry in os.listdir(directory):
                    full_path = os.path.join(directory, entry)
                    if os.path.isdir(full_path):
                        tree["children"].append(list_dir_contents(full_path))
                    else:
                        tree["children"].append({"type": "file", "name": entry})
            except OSError as exc:
                tree = {"error": str(exc)}
            return tree

        data = request.get_json()
        if "path" not in data:
            return jsonify(error="Missing 'path' parameter"), 400

        start_path = data["path"]
        if not os.path.isdir(start_path):
            return jsonify(error="The provided path is not a directory"), 400

        return jsonify(directory_tree=list_dir_contents(start_path))

    @app.route("/file", methods=["POST"])
    def get_file():
        if "file_path" in request.form:
            file_path = os.path.expandvars(os.path.expanduser(request.form["file_path"]))
        else:
            return jsonify({"error": "file_path is required"}), 400

        try:
            if not os.path.exists(file_path):
                return jsonify({"error": "File not found"}), 404

            file_size = os.path.getsize(file_path)
            app.logger.info("Serving file: %s (%s bytes)", file_path, file_size)
            return send_file(file_path, as_attachment=True)
        except FileNotFoundError:
            return jsonify({"error": "File not found"}), 404
        except Exception as exc:
            app.logger.error("Error serving file %s: %s", file_path, exc)
            return jsonify({"error": f"Failed to serve file: {str(exc)}"}), 500

    @app.route("/setup/upload", methods=["POST"])
    def upload_file():
        if "file_path" in request.form and "file_data" in request.files:
            file_path = os.path.expandvars(os.path.expanduser(request.form["file_path"]))
            file = request.files["file_data"]

            try:
                target_dir = os.path.dirname(file_path)
                if target_dir:
                    os.makedirs(target_dir, exist_ok=True)

                file.save(file_path)
                uploaded_size = os.path.getsize(file_path)

                app.logger.info("File uploaded successfully: %s (%s bytes)", file_path, uploaded_size)
                return f"File Uploaded: {uploaded_size} bytes"
            except Exception as exc:
                app.logger.error("Error uploading file to %s: %s", file_path, exc)
                if os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except Exception:
                        pass
                return jsonify({"error": f"Failed to upload file: {str(exc)}"}), 500
        return jsonify({"error": "file_path and file_data are required"}), 400

    @app.route("/platform", methods=["GET"])
    def get_platform():
        return platform.system()

    @app.route("/cursor_position", methods=["GET"])
    def cursor_position():
        import pyautogui

        pos = pyautogui.position()
        return jsonify(pos.x, pos.y)

    @app.route("/setup/change_wallpaper", methods=["POST"])
    def change_wallpaper():
        data = request.json
        path = data.get("path", None)

        if not path:
            return "Path not supplied!", 400

        path_obj = Path(os.path.expandvars(os.path.expanduser(path)))

        if not path_obj.exists():
            return f"File not found: {path_obj}", 404

        try:
            user_platform = platform.system()
            if user_platform == "Windows":
                ctypes.windll.user32.SystemParametersInfoW(20, 0, str(path_obj), 3)
            elif user_platform == "Linux":
                subprocess.run(["gsettings", "set", "org.gnome.desktop.background", "picture-uri", f"file://{path_obj}"])
            elif user_platform == "Darwin":
                subprocess.run(["osascript", "-e", f'tell application "Finder" to set desktop picture to POSIX file "{path_obj}"'])
            return "Wallpaper changed successfully"
        except Exception as exc:
            return f"Failed to change wallpaper. Error: {exc}", 500

    @app.route("/setup/download_file", methods=["POST"])
    def download_file():
        data = request.json
        url = data.get("url", None)
        path = data.get("path", None)

        if not url or not path:
            return "Path or URL not supplied!", 400

        path_obj = Path(os.path.expandvars(os.path.expanduser(path)))
        path_obj.parent.mkdir(parents=True, exist_ok=True)

        max_retries = 3
        error: Optional[Exception] = None

        for i in range(max_retries):
            try:
                app.logger.info("Download attempt %s/%s for %s", i + 1, max_retries, url)
                response = requests.get(url, stream=True, timeout=300)
                response.raise_for_status()

                total_size = int(response.headers.get("content-length", 0))
                if total_size > 0:
                    app.logger.info("Expected file size: %.2f MB", total_size / (1024 * 1024))

                downloaded_size = 0
                with open(path_obj, "wb") as file:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            file.write(chunk)
                            downloaded_size += len(chunk)
                            if total_size > 0 and downloaded_size % (1024 * 1024) == 0:
                                progress = (downloaded_size / total_size) * 100
                                app.logger.info("Download progress: %.1f%%", progress)

                actual_size = os.path.getsize(path_obj)
                if total_size > 0 and actual_size != total_size:
                    raise Exception(f"Download incomplete. Expected {total_size} bytes, got {actual_size} bytes")

                app.logger.info("File downloaded successfully: %s (%s bytes)", path_obj, actual_size)
                return f"File downloaded successfully: {actual_size} bytes"
            except (requests.RequestException, Exception) as exc:
                error = exc
                app.logger.error("Failed to download %s: %s. Retrying... (%s attempts left)", url, exc, max_retries - i - 1)
                if path_obj.exists():
                    try:
                        path_obj.unlink()
                    except Exception:
                        pass

        return f"Failed to download {url}. No retries left. Error: {error}", 500

    @app.route("/setup/open_file", methods=["POST"])
    def open_file():
        data = request.json
        path = data.get("path", None)

        if not path:
            return "Path not supplied!", 400

        path_obj = Path(os.path.expandvars(os.path.expanduser(path)))
        is_file_path = path_obj.exists()

        if not is_file_path:
            import shutil

            if not shutil.which(path):
                return f"Application/file not found: {path}", 404

        try:
            if is_file_path:
                if platform.system() == "Windows":
                    os.startfile(path_obj)
                else:
                    open_cmd = "open" if platform.system() == "Darwin" else "xdg-open"
                    subprocess.Popen([open_cmd, str(path_obj)])
                file_name = path_obj.name
                file_name_without_ext, _ = os.path.splitext(file_name)
            else:
                if platform.system() == "Windows":
                    subprocess.Popen([path])
                else:
                    subprocess.Popen([path])
                file_name = path
                file_name_without_ext = path

            start_time = time.time()
            window_found = False

            while time.time() - start_time < TIMEOUT:
                os_name = platform.system()
                if os_name in ["Windows", "Darwin"]:
                    import pygetwindow as gw

                    windows = gw.getWindowsWithTitle(file_name)
                    if not windows:
                        windows = gw.getWindowsWithTitle(file_name_without_ext)

                    if windows:
                        windows[0].activate()
                        window_found = True
                        break
                elif os_name == "Linux":
                    try:
                        result = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True, check=True)
                        window_list = result.stdout.strip().split("\n")
                        if result.stdout.strip():
                            for window in window_list:
                                if file_name in window or file_name_without_ext in window:
                                    window_id = window.split()[0]
                                    subprocess.run(["wmctrl", "-i", "-a", window_id], check=True)
                                    window_found = True
                                    break
                            if window_found:
                                break
                    except (subprocess.CalledProcessError, FileNotFoundError):
                        if "wmctrl_failed_once" not in locals():
                            app.logger.warning("wmctrl command is not ready, will keep retrying...")
                            wmctrl_failed_once = True

                time.sleep(1)

            if window_found:
                return "File opened and window activated successfully"
            return f"Failed to find window for {file_name} within {TIMEOUT} seconds.", 500
        except Exception as exc:
            return f"Failed to open {path}. Error: {exc}", 500

    @app.route("/setup/activate_window", methods=["POST"])
    def activate_window():
        data = request.json
        window_name = data.get("window_name", None)
        if not window_name:
            return "window_name required", 400
        strict: bool = data.get("strict", False)
        by_class_name: bool = data.get("by_class", False)

        os_name = platform.system()

        if os_name == "Windows":
            import pygetwindow as gw

            if by_class_name:
                return "Get window by class name is not supported on Windows currently.", 500
            windows: List[gw.Window] = gw.getWindowsWithTitle(window_name)

            window = None
            if len(windows) == 0:
                return f"Window {window_name} not found (empty results)", 404
            elif strict:
                for wnd in windows:
                    if wnd.title == wnd:
                        window = wnd
                if window is None:
                    return f"Window {window_name} not found (strict mode).", 404
            else:
                window = windows[0]
            window.activate()

        elif os_name == "Darwin":
            import pygetwindow as gw

            if by_class_name:
                return "Get window by class name is not supported on macOS currently.", 500
            windows = gw.getWindowsWithTitle(window_name)

            window = None
            if len(windows) == 0:
                return f"Window {window_name} not found (empty results)", 404
            elif strict:
                for wnd in windows:
                    if wnd.title == wnd:
                        window = wnd
                if window is None:
                    return f"Window {window_name} not found (strict mode).", 404
            else:
                window = windows[0]

            window.unminimize()
            window.activate()

        elif os_name == "Linux":
            subprocess.run(
                ["wmctrl", f"-{'x' if by_class_name else ''}{'F' if strict else ''}a", window_name]
            )
        else:
            return f"Operating system {os_name} not supported.", 400

        return "Window activated successfully", 200

    @app.route("/setup/close_window", methods=["POST"])
    def close_window():
        data = request.json
        if "window_name" not in data:
            return "window_name required", 400
        window_name: str = data["window_name"]
        strict: bool = data.get("strict", False)
        by_class_name: bool = data.get("by_class", False)

        os_name = platform.system()
        if os_name == "Windows":
            import pygetwindow as gw

            if by_class_name:
                return "Get window by class name is not supported on Windows currently.", 500
            windows: List[gw.Window] = gw.getWindowsWithTitle(window_name)

            window = None
            if len(windows) == 0:
                return f"Window {window_name} not found (empty results)", 404
            elif strict:
                for wnd in windows:
                    if wnd.title == wnd:
                        window = wnd
                if window is None:
                    return f"Window {window_name} not found (strict mode).", 404
            else:
                window = windows[0]
            window.close()
        elif os_name == "Linux":
            subprocess.run(
                ["wmctrl", f"-{'x' if by_class_name else ''}{'F' if strict else ''}c", window_name]
            )
        elif os_name == "Darwin":
            return "Currently not supported on macOS.", 500
        else:
            return f"Not supported platform {os_name}", 500

        return "Window closed successfully.", 200

    @app.route("/start_recording", methods=["POST"])
    def start_recording():
        global recording_process
        if recording_process and recording_process.poll() is None:
            return jsonify({"status": "error", "message": "Recording is already in progress."}), 400

        if os.path.exists(recording_path):
            try:
                os.remove(recording_path)
            except OSError as exc:
                app.logger.error("Error removing old recording file: %s", exc)
                return jsonify({"status": "error", "message": f"Failed to remove old recording file: {exc}"}), 500

        screen_width, screen_height = get_screen_size()
        start_command = [
            "ffmpeg",
            "-y",
            *build_ffmpeg_capture_input_args(screen_width, screen_height, 30, draw_mouse=True),
            "-c:v",
            "libx264",
            "-r",
            "30",
            recording_path,
        ]

        recording_process = subprocess.Popen(
            start_command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=subprocess_creation_flags(),
        )

        try:
            recording_process.wait(timeout=2)
            error_output = recording_process.stderr.read()
            return jsonify(
                {
                    "status": "error",
                    "message": f"Failed to start recording. ffmpeg terminated unexpectedly. Error: {error_output}",
                }
            ), 500
        except subprocess.TimeoutExpired:
            return jsonify({"status": "success", "message": "Started recording successfully."})

    @app.route("/end_recording", methods=["POST"])
    def end_recording():
        global recording_process

        if not recording_process or recording_process.poll() is not None:
            recording_process = None
            return jsonify({"status": "error", "message": "No recording in progress to stop."}), 400

        error_output = ""
        try:
            if recording_process.stdin is not None:
                recording_process.stdin.write("q\n")
                recording_process.stdin.flush()
            _, error_output = recording_process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            app.logger.error("ffmpeg did not exit after a graceful stop request, killing the process.")
            recording_process.kill()
            _, error_output = recording_process.communicate()
            recording_process = None
            return jsonify(
                {
                    "status": "error",
                    "message": f"Recording process was unresponsive and had to be killed. Stderr: {error_output}",
                }
            ), 500
        except OSError as exc:
            app.logger.error("Failed to send stop request to ffmpeg, killing the process. Error: %s", exc)
            recording_process.kill()
            _, error_output = recording_process.communicate()
            recording_process = None
            return jsonify(
                {
                    "status": "error",
                    "message": f"Failed to stop recording gracefully. Error: {exc}. Stderr: {error_output}",
                }
            ), 500

        recording_process = None

        if os.path.exists(recording_path) and os.path.getsize(recording_path) > 0:
            return send_file(recording_path, as_attachment=True)

        app.logger.error("Recording failed. The output file is missing or empty. ffmpeg stderr: %s", error_output)
        return abort(
            500,
            description=f"Recording failed. The output file is missing or empty. ffmpeg stderr: {error_output}",
        )

    @app.route("/run_python", methods=["POST"])
    def run_python():
        data = request.json
        code = data.get("code", None)

        if not code:
            return jsonify({"status": "error", "message": "Code not supplied!"}), 400

        temp_filename = f"/tmp/python_exec_{uuid.uuid4().hex}.py"

        try:
            with open(temp_filename, "w") as file:
                file.write(code)

            result = subprocess.run(
                ["/usr/bin/python3", temp_filename],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
            )

            try:
                os.remove(temp_filename)
            except Exception:
                pass

            output = result.stdout
            error_output = result.stderr

            combined_message = output
            if error_output:
                combined_message += ("\n" + error_output) if output else error_output

            if result.returncode != 0:
                status = "error"
                if not error_output:
                    error_output = f"Process exited with code {result.returncode}"
                    combined_message = combined_message + "\n" + error_output if combined_message else error_output
            else:
                status = "success"

            return jsonify(
                {
                    "status": status,
                    "message": combined_message,
                    "need_more": False,
                    "output": output,
                    "error": error_output,
                    "return_code": result.returncode,
                }
            )
        except subprocess.TimeoutExpired:
            try:
                os.remove(temp_filename)
            except Exception:
                pass

            return jsonify(
                {
                    "status": "error",
                    "message": "Execution timeout: Code took too long to execute",
                    "error": "TimeoutExpired",
                    "need_more": False,
                    "output": None,
                }
            ), 500
        except Exception as exc:
            try:
                os.remove(temp_filename)
            except Exception:
                pass

            return jsonify(
                {
                    "status": "error",
                    "message": f"Execution error: {str(exc)}",
                    "error": traceback.format_exc(),
                    "need_more": False,
                    "output": None,
                }
            ), 500

    @app.route("/run_bash_script", methods=["POST"])
    def run_bash_script():
        data = request.json
        script = data.get("script", None)
        timeout = data.get("timeout", 100)
        working_dir = data.get("working_dir", None)

        if not script:
            return jsonify({"status": "error", "output": "Script not supplied!", "error": "", "returncode": -1}), 400

        if working_dir:
            working_dir = os.path.expanduser(working_dir)
            if not os.path.exists(working_dir):
                return jsonify(
                    {
                        "status": "error",
                        "output": f"Working directory does not exist: {working_dir}",
                        "error": "",
                        "returncode": -1,
                    }
                ), 400

        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as tmp_file:
            if "#!/bin/bash" not in script:
                script = "#!/bin/bash\n\n" + script
            tmp_file.write(script)
            tmp_file_path = tmp_file.name

        try:
            os.chmod(tmp_file_path, 0o755)

            if platform_name == "Windows":
                flags = subprocess.CREATE_NO_WINDOW
                result = subprocess.run(
                    ["bash", tmp_file_path],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=timeout,
                    cwd=working_dir,
                    creationflags=flags,
                    shell=False,
                )
            else:
                flags = 0
                result = subprocess.run(
                    ["/bin/bash", tmp_file_path],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=timeout,
                    cwd=working_dir,
                    creationflags=flags,
                    shell=False,
                )

            _append_event(
                "BashScript",
                {"script": script, "output": result.stdout, "error": "", "returncode": result.returncode},
                ts=time.time(),
            )

            return jsonify(
                {
                    "status": "success" if result.returncode == 0 else "error",
                    "output": result.stdout,
                    "error": "",
                    "returncode": result.returncode,
                }
            )
        except subprocess.TimeoutExpired:
            return jsonify(
                {
                    "status": "error",
                    "output": f"Script execution timed out after {timeout} seconds",
                    "error": "",
                    "returncode": -1,
                }
            ), 500
        except FileNotFoundError:
            try:
                result = subprocess.run(
                    ["sh", tmp_file_path],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=timeout,
                    cwd=working_dir,
                    shell=False,
                )

                _append_event(
                    "BashScript",
                    {"script": script, "output": result.stdout, "error": "", "returncode": result.returncode},
                    ts=time.time(),
                )

                return jsonify(
                    {
                        "status": "success" if result.returncode == 0 else "error",
                        "output": result.stdout,
                        "error": "",
                        "returncode": result.returncode,
                    }
                )
            except Exception as exc:
                return jsonify(
                    {"status": "error", "output": f"Failed to execute script: {str(exc)}", "error": "", "returncode": -1}
                ), 500
        except Exception as exc:
            return jsonify(
                {"status": "error", "output": f"Failed to execute script: {str(exc)}", "error": "", "returncode": -1}
            ), 500
        finally:
            try:
                os.unlink(tmp_file_path)
            except Exception:
                pass
