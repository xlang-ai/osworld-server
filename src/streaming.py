import asyncio
import json
import sys
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.wsgi import WSGIMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .actions import execute_stream_action
from .capture import (
    build_stream_frame_payload,
    coerce_stream_float,
    coerce_stream_int,
    normalize_stream_format,
)
from .platform_runtime import (
    DEFAULT_STREAM_FPS,
    DEFAULT_STREAM_QUALITY,
    get_capture_display_name,
    get_linux_screen_size,
    platform_name,
)


async def send_ws_json(websocket: WebSocket, send_lock: asyncio.Lock, payload: Dict[str, Any]) -> None:
    async with send_lock:
        await websocket.send_json(payload)


async def stream_frames_to_websocket(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    session_state: Dict[str, Any],
    stop_event: asyncio.Event,
) -> None:
    frame_id = 0
    while not stop_event.is_set():
        loop_started_at = time.time()
        try:
            frame_payload = await asyncio.to_thread(
                build_stream_frame_payload,
                session_state["session_id"],
                frame_id,
                session_state["image_format"],
                session_state["quality"],
            )
            await send_ws_json(websocket, send_lock, frame_payload)
            frame_id += 1
        except Exception as exc:
            await send_ws_json(
                websocket,
                send_lock,
                {
                    "type": "stream_error",
                    "session_id": session_state["session_id"],
                    "timestamp": time.time(),
                    "error": str(exc),
                },
            )
            await asyncio.sleep(0.5)

        frame_interval = 1.0 / max(float(session_state["fps"]), 0.1)
        elapsed = time.time() - loop_started_at
        await asyncio.sleep(max(0.0, frame_interval - elapsed))


def build_h264_stream_command(
    width: int,
    height: int,
    fps: float,
    bitrate_kbps: int,
    gop: int,
) -> List[str]:
    frame_rate = max(1, min(int(round(fps)), 60))
    keyint = max(1, min(int(gop), 240))
    bitrate = max(250, min(int(bitrate_kbps), 12000))
    display_name = get_capture_display_name()

    return [
        "ffmpeg",
        "-loglevel",
        "error",
        "-nostdin",
        "-fflags",
        "nobuffer",
        "-f",
        "x11grab",
        "-draw_mouse",
        "1",
        "-video_size",
        f"{width}x{height}",
        "-framerate",
        str(frame_rate),
        "-i",
        display_name,
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
        "-profile:v",
        "baseline",
        "-g",
        str(keyint),
        "-keyint_min",
        str(keyint),
        "-sc_threshold",
        "0",
        "-b:v",
        f"{bitrate}k",
        "-maxrate",
        f"{bitrate}k",
        "-bufsize",
        f"{bitrate * 2}k",
        "-muxdelay",
        "0",
        "-muxpreload",
        "0",
        "-f",
        "mpegts",
        "pipe:1",
    ]


def build_fmp4_stream_command(
    width: int,
    height: int,
    fps: float,
    bitrate_kbps: int,
    gop: int,
) -> List[str]:
    frame_rate = max(1, min(int(round(fps)), 60))
    keyint = max(1, min(int(gop), 240))
    bitrate = max(250, min(int(bitrate_kbps), 12000))
    display_name = get_capture_display_name()

    return [
        "ffmpeg",
        "-loglevel",
        "error",
        "-nostdin",
        "-fflags",
        "nobuffer",
        "-f",
        "x11grab",
        "-draw_mouse",
        "1",
        "-video_size",
        f"{width}x{height}",
        "-framerate",
        str(frame_rate),
        "-i",
        display_name,
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
        "-profile:v",
        "baseline",
        "-g",
        str(keyint),
        "-keyint_min",
        str(keyint),
        "-sc_threshold",
        "0",
        "-b:v",
        f"{bitrate}k",
        "-maxrate",
        f"{bitrate}k",
        "-bufsize",
        f"{bitrate * 2}k",
        "-movflags",
        "frag_keyframe+empty_moov+default_base_moof+faststart",
        "-frag_duration",
        "100000",
        "-muxdelay",
        "0",
        "-muxpreload",
        "0",
        "-f",
        "mp4",
        "pipe:1",
    ]


async def terminate_async_process(process: Optional[asyncio.subprocess.Process]) -> None:
    if process is None or process.returncode is not None:
        return

    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=3)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


async def log_subprocess_stderr(
    process: asyncio.subprocess.Process,
    *,
    session_id: str,
    stream_type: str,
    logger,
) -> None:
    if process.stderr is None:
        return

    while True:
        line = await process.stderr.readline()
        if not line:
            return
        logger.warning(
            "[%s:%s] %s",
            stream_type,
            session_id,
            line.decode("utf-8", errors="replace").rstrip(),
        )


async def handle_control_message(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    *,
    session_id: str,
    message: Dict[str, Any],
) -> bool:
    message_type = message.get("type")

    if message_type == "ping":
        await send_ws_json(
            websocket,
            send_lock,
            {"type": "pong", "session_id": session_id, "timestamp": time.time()},
        )
        return True

    if message_type == "close":
        return False

    if message_type != "action":
        await send_ws_json(
            websocket,
            send_lock,
            {
                "type": "error",
                "session_id": session_id,
                "timestamp": time.time(),
                "error": f"Unsupported message type: {message_type}",
            },
        )
        return True

    action_id = str(message.get("action_id") or uuid.uuid4().hex)
    action = message.get("action")

    await send_ws_json(
        websocket,
        send_lock,
        {
            "type": "action_ack",
            "session_id": session_id,
            "action_id": action_id,
            "timestamp": time.time(),
            "status": "received",
        },
    )

    try:
        result = await asyncio.to_thread(execute_stream_action, action)
    except Exception as exc:
        await send_ws_json(
            websocket,
            send_lock,
            {
                "type": "action_result",
                "session_id": session_id,
                "action_id": action_id,
                "timestamp": time.time(),
                "status": "error",
                "error": str(exc),
            },
        )
        return True

    await send_ws_json(
        websocket,
        send_lock,
        {
            "type": "action_result",
            "session_id": session_id,
            "action_id": action_id,
            "timestamp": result["completed_at"],
            "status": result["status"],
            "started_at": result["started_at"],
            "completed_at": result["completed_at"],
            "result": result["result"],
        },
    )
    return True


def create_asgi_app(flask_app) -> FastAPI:
    asgi_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    logger = flask_app.logger

    @asgi_app.websocket("/ws")
    async def websocket_stream(websocket: WebSocket):
        await websocket.accept()

        session_state: Dict[str, Any] = {
            "session_id": uuid.uuid4().hex,
            "fps": coerce_stream_float(websocket.query_params.get("fps"), DEFAULT_STREAM_FPS, 0.5, 30.0),
            "image_format": normalize_stream_format(websocket.query_params.get("format")),
            "quality": coerce_stream_int(
                websocket.query_params.get("quality"),
                DEFAULT_STREAM_QUALITY,
                10,
                95,
            ),
        }
        send_lock = asyncio.Lock()
        stop_event = asyncio.Event()

        await send_ws_json(
            websocket,
            send_lock,
            {
                "type": "session_started",
                "session_id": session_state["session_id"],
                "timestamp": time.time(),
                "fps": session_state["fps"],
                "format": session_state["image_format"],
                "quality": session_state["quality"],
            },
        )

        producer_task = asyncio.create_task(stream_frames_to_websocket(websocket, send_lock, session_state, stop_event))

        try:
            while True:
                raw_message = await websocket.receive_text()
                try:
                    message = json.loads(raw_message)
                except json.JSONDecodeError as exc:
                    await send_ws_json(
                        websocket,
                        send_lock,
                        {
                            "type": "error",
                            "session_id": session_state["session_id"],
                            "timestamp": time.time(),
                            "error": f"Invalid JSON message: {exc}",
                        },
                    )
                    continue

                message_type = message.get("type")
                if message_type == "ping":
                    await send_ws_json(
                        websocket,
                        send_lock,
                        {
                            "type": "pong",
                            "session_id": session_state["session_id"],
                            "timestamp": time.time(),
                        },
                    )
                    continue

                if message_type == "close":
                    break

                if message_type == "update_config":
                    if "fps" in message:
                        session_state["fps"] = coerce_stream_float(str(message["fps"]), session_state["fps"], 0.5, 30.0)
                    if "format" in message:
                        session_state["image_format"] = normalize_stream_format(str(message["format"]))
                    if "quality" in message:
                        session_state["quality"] = coerce_stream_int(str(message["quality"]), session_state["quality"], 10, 95)
                    await send_ws_json(
                        websocket,
                        send_lock,
                        {
                            "type": "config_updated",
                            "session_id": session_state["session_id"],
                            "timestamp": time.time(),
                            "fps": session_state["fps"],
                            "format": session_state["image_format"],
                            "quality": session_state["quality"],
                        },
                    )
                    continue

                if message_type == "request_frame":
                    try:
                        frame_payload = await asyncio.to_thread(
                            build_stream_frame_payload,
                            session_state["session_id"],
                            -1,
                            session_state["image_format"],
                            session_state["quality"],
                        )
                        if "request_id" in message:
                            frame_payload["request_id"] = message["request_id"]
                        await send_ws_json(websocket, send_lock, frame_payload)
                    except Exception as exc:
                        await send_ws_json(
                            websocket,
                            send_lock,
                            {
                                "type": "error",
                                "session_id": session_state["session_id"],
                                "timestamp": time.time(),
                                "error": str(exc),
                            },
                        )
                    continue

                if message_type == "action":
                    await handle_control_message(
                        websocket,
                        send_lock,
                        session_id=session_state["session_id"],
                        message=message,
                    )
                    continue

                await send_ws_json(
                    websocket,
                    send_lock,
                    {
                        "type": "error",
                        "session_id": session_state["session_id"],
                        "timestamp": time.time(),
                        "error": f"Unsupported message type: {message_type}",
                    },
                )
        except WebSocketDisconnect:
            pass
        finally:
            stop_event.set()
            producer_task.cancel()
            with suppress(asyncio.CancelledError):
                await producer_task
            with suppress(RuntimeError):
                await websocket.close()

    @asgi_app.websocket("/ws/control")
    async def websocket_control(websocket: WebSocket):
        await websocket.accept()

        session_id = websocket.query_params.get("session_id") or uuid.uuid4().hex
        send_lock = asyncio.Lock()

        await send_ws_json(
            websocket,
            send_lock,
            {"type": "control_ready", "session_id": session_id, "timestamp": time.time()},
        )

        try:
            while True:
                raw_message = await websocket.receive_text()
                try:
                    message = json.loads(raw_message)
                except json.JSONDecodeError as exc:
                    await send_ws_json(
                        websocket,
                        send_lock,
                        {
                            "type": "error",
                            "session_id": session_id,
                            "timestamp": time.time(),
                            "error": f"Invalid JSON message: {exc}",
                        },
                    )
                    continue

                should_continue = await handle_control_message(
                    websocket,
                    send_lock,
                    session_id=session_id,
                    message=message,
                )
                if not should_continue:
                    break
        except WebSocketDisconnect:
            pass
        finally:
            with suppress(RuntimeError):
                await websocket.close()

    @asgi_app.websocket("/ws/h264")
    async def websocket_h264_stream(websocket: WebSocket):
        await websocket.accept()

        session_id = websocket.query_params.get("session_id") or uuid.uuid4().hex
        fps = coerce_stream_float(websocket.query_params.get("fps"), 12.0, 1.0, 60.0)
        bitrate_kbps = coerce_stream_int(websocket.query_params.get("bitrate_kbps"), 2000, 250, 12000)
        gop = coerce_stream_int(websocket.query_params.get("gop"), max(1, int(round(fps))), 1, 240)

        if platform_name != "Linux":
            await websocket.send_json(
                {
                    "type": "error",
                    "session_id": session_id,
                    "timestamp": time.time(),
                    "error": "H.264 streaming is currently implemented only for Linux/X11 guests.",
                }
            )
            await websocket.close()
            return

        process: Optional[asyncio.subprocess.Process] = None
        stderr_task: Optional[asyncio.Task] = None

        try:
            width, height = await asyncio.to_thread(get_linux_screen_size)
            command = build_h264_stream_command(width, height, fps, bitrate_kbps, gop)
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stderr_task = asyncio.create_task(log_subprocess_stderr(process, session_id=session_id, stream_type="h264", logger=logger))
        except Exception as exc:
            await websocket.send_json(
                {
                    "type": "error",
                    "session_id": session_id,
                    "timestamp": time.time(),
                    "error": f"Failed to start H.264 stream: {exc}",
                }
            )
            await websocket.close()
            return

        await websocket.send_json(
            {
                "type": "session_started",
                "session_id": session_id,
                "timestamp": time.time(),
                "codec": "h264",
                "container": "mpegts",
                "fps": fps,
                "bitrate_kbps": bitrate_kbps,
                "gop": gop,
                "width": width,
                "height": height,
            }
        )

        try:
            while True:
                if process.stdout is None:
                    break

                chunk = await process.stdout.read(32768)
                if not chunk:
                    returncode = await process.wait()
                    if returncode not in (0, None):
                        with suppress(RuntimeError):
                            await websocket.send_json(
                                {
                                    "type": "stream_error",
                                    "session_id": session_id,
                                    "timestamp": time.time(),
                                    "error": f"ffmpeg exited with code {returncode}",
                                }
                            )
                    break
                await websocket.send_bytes(chunk)
        except WebSocketDisconnect:
            pass
        finally:
            await terminate_async_process(process)
            if stderr_task is not None:
                stderr_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stderr_task
            with suppress(RuntimeError):
                await websocket.close()

    @asgi_app.get("/live/h264.mp4")
    async def live_h264_mp4(request: Request):
        if platform_name != "Linux":
            return JSONResponse(
                status_code=400,
                content={
                    "error": "Browser H.264 streaming is currently implemented only for Linux/X11 guests.",
                    "platform": platform_name,
                },
            )

        session_id = request.query_params.get("session_id") or uuid.uuid4().hex
        fps = coerce_stream_float(request.query_params.get("fps"), 12.0, 1.0, 60.0)
        bitrate_kbps = coerce_stream_int(request.query_params.get("bitrate_kbps"), 2000, 250, 12000)
        gop = coerce_stream_int(request.query_params.get("gop"), max(1, int(round(fps))), 1, 240)

        async def iter_video_bytes():
            process: Optional[asyncio.subprocess.Process] = None
            stderr_task: Optional[asyncio.Task] = None
            try:
                width, height = await asyncio.to_thread(get_linux_screen_size)
                command = build_fmp4_stream_command(width, height, fps, bitrate_kbps, gop)
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stderr_task = asyncio.create_task(
                    log_subprocess_stderr(process, session_id=session_id, stream_type="fmp4", logger=logger)
                )

                while True:
                    if await request.is_disconnected():
                        break
                    if process.stdout is None:
                        break

                    chunk = await process.stdout.read(32768)
                    if not chunk:
                        break
                    yield chunk
            finally:
                await terminate_async_process(process)
                if stderr_task is not None:
                    stderr_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await stderr_task

        return StreamingResponse(
            iter_video_bytes(),
            media_type="video/mp4",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @asgi_app.get("/client/h264")
    async def browser_h264_client():
        candidate_paths = []

        frozen_base = getattr(sys, "_MEIPASS", None)
        if frozen_base:
            candidate_paths.append(Path(frozen_base) / "h264_browser_client.html")

        module_base = Path(__file__).resolve().parent.parent
        candidate_paths.append(module_base / "h264_browser_client.html")
        candidate_paths.append(Path.cwd() / "h264_browser_client.html")

        client_path = next((path for path in candidate_paths if path.exists()), candidate_paths[0])
        return FileResponse(client_path, media_type="text/html")

    asgi_app.mount("/", WSGIMiddleware(flask_app))
    return asgi_app
