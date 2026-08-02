"""UI front-ends.

Windows uses the original tkinter overlay + pystray tray. macOS uses AppKit:
tkinter cannot be used there because Tk activates the application on every
window map, which would steal focus from the window we are about to type into.
"""
import sys


def create_ui_factory():
    """Return a callable `factory(app) -> ui`."""
    if sys.platform == "darwin":
        from .appkit_ui import AppKitUI
        return AppKitUI
    from .tk_ui import TkUI
    return TkUI
