"""WhisperType — push-to-talk voice dictation, fully local.

Windows and macOS share everything except the input/window layer
(`whispertype.backends`) and the overlay/tray layer (`whispertype.ui`).
"""
__version__ = "2.0.0"


def main():
    from .app import App
    from .backends import create_backend
    from .log import init, log
    from .ui import create_ui_factory

    init()
    log("Starting voice daemon...")

    backend = create_backend()
    app = App(backend, create_ui_factory())

    missing = backend.check_permissions()
    for item in missing:
        log(f"MISSING PERMISSION: {item}")
    if missing and hasattr(backend, "request_permissions"):
        backend.request_permissions()

    app.start()
    app.run()
