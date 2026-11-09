import argparse
import os
import socket
import threading
import time
import webbrowser

import uvicorn

APP_NAME = "Reels Manager"
SERVER_START_TIMEOUT_SECONDS = 15
PENDING_EDITS_TIMEOUT_SECONDS = 180


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_server(port: int) -> uvicorn.Server:
    from .server import app

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + SERVER_START_TIMEOUT_SECONDS
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError(f"The {APP_NAME} server did not start within {SERVER_START_TIMEOUT_SECONDS} seconds.")
        time.sleep(0.05)
    return server


def _ask_about_pending_edits(count: int) -> str:
    import AppKit

    alert = AppKit.NSAlert.alloc().init()
    noun = "change is" if count == 1 else "changes are"
    alert.setMessageText_(f"{count} {noun} still being sent to Instagram")
    alert.setInformativeText_("If you quit now, those changes are lost and the reels stay saved on Instagram.")
    alert.addButtonWithTitle_("Quit When Done")
    alert.addButtonWithTitle_("Quit Now")
    alert.addButtonWithTitle_("Cancel")
    return {
        AppKit.NSAlertFirstButtonReturn: "wait",
        AppKit.NSAlertSecondButtonReturn: "quit",
    }.get(alert.runModal(), "cancel")


def _close_when_edits_finish(window, pending_edits: dict) -> None:
    deadline = time.monotonic() + PENDING_EDITS_TIMEOUT_SECONDS
    while pending_edits["count"] > 0 and time.monotonic() < deadline:
        time.sleep(0.25)
    window.destroy()


def run_window() -> None:
    import webview

    from .server import pending_edits

    server = _start_server(_free_port())
    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    window = webview.create_window(
        APP_NAME,
        f"http://127.0.0.1:{server.config.port}/",
        width=1440,
        height=920,
        min_size=(900, 600),
        background_color="#E6EBF0",
    )
    def on_closing():
        if pending_edits["count"] == 0:
            return True
        choice = _ask_about_pending_edits(pending_edits["count"])
        if choice == "quit":
            return True
        if choice == "wait":
            threading.Thread(target=_close_when_edits_finish, args=(window, pending_edits), daemon=True).start()
        return False

    window.events.closing += on_closing
    webview.start(debug=os.environ.get("REELS_DEBUG") == "1")
    server.should_exit = True


def run_browser(port: int, open_browser: bool) -> None:
    if open_browser:
        threading.Timer(1.0, webbrowser.open, args=(f"http://127.0.0.1:{port}",)).start()
    from .server import app

    uvicorn.run(app, host="127.0.0.1", port=port)


def main() -> None:
    parser = argparse.ArgumentParser(description=f"Run {APP_NAME}.")
    parser.add_argument("--browser", action="store_true", help="serve the app to a browser instead of opening a window")
    parser.add_argument("--port", type=int, default=8790, help="port for --browser mode")
    parser.add_argument("--no-browser", action="store_true", help="with --browser, skip opening a browser tab")
    args = parser.parse_args()
    if args.browser:
        run_browser(args.port, not args.no_browser)
    else:
        run_window()


if __name__ == "__main__":
    main()
