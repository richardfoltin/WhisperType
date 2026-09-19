"""Platform backend interface.

A backend owns everything the app cannot do portably:

  * finding out which window/app the user was in before recording started,
  * bringing that window back to the front afterwards,
  * synthesising the keystrokes that type the transcript,
  * reading GPU utilisation for the overlay sparkline.

`Target` is an opaque handle produced by `capture_target()` and only ever
interpreted by the backend that produced it (an HWND on Windows, a
pid + AXUIElement pair on macOS).
"""


class Backend:
    #: Human readable, shown in logs only.
    name = "?"

    #: Value stamped into every keystroke this backend synthesises, so the
    #: global key hook can tell our own typing from the user's and never
    #: swallow it. None on a backend whose events carry no such field.
    event_marker = None

    #: False when the platform has no usable GPU counter — the overlay then
    #: hides the graph, exactly like the Windows build did without NVML.
    gpu_available = False

    #: Label above the utilisation graph.
    gpu_label = "GPU"

    def set_own_window_provider(self, fn):
        """Register a callable returning this app's own native window ids, so
        `capture_target` can skip the overlay when it has focus."""

    # ── Target window ──

    def capture_target(self):
        """Snapshot the currently focused window/app. Returns an opaque handle
        or None."""
        raise NotImplementedError

    def target_title(self, target):
        """Short window title for the overlay (truncated to ~20 chars)."""
        raise NotImplementedError

    def target_app(self, target):
        """Process/application name for the queue table."""
        raise NotImplementedError

    def target_bundle(self, target):
        """Stable identifier for the target application, used to resolve its
        icon for the history rows: a bundle id on macOS, the executable's full
        path on Windows. "" when it cannot be determined — the view then falls
        back to a monogram plate."""
        return ""

    def activate(self, target):
        """Bring `target` back to the front. Returns True once it is confirmed
        frontmost; False means the transcript should stay in history only."""
        raise NotImplementedError

    # ── Input synthesis ──

    def type_text(self, text):
        """Type `text` into whatever is focused, without using the clipboard."""
        raise NotImplementedError

    def send_enter(self):
        """Send a single Return keypress."""
        raise NotImplementedError

    def preflight_typing(self, target):
        """Raise PermissionError when the transcript cannot possibly reach
        `target`, so the text is kept in history with a reason instead of being
        typed into a void.

        Called after the window is confirmed frontmost and before the first
        keystroke is synthesised. Silence here means "go ahead"; a backend that
        cannot tell must stay silent rather than guess.
        """

    # ── Metrics ──

    def gpu_percent(self):
        """Current GPU utilisation 0..100, or None if unavailable."""
        return None

    def fullscreen_app_running(self):
        """True while something is running full screen — a game, a
        presentation, a full-screen video.

        A dictation model resident in VRAM is invisible until the machine
        needs that VRAM for something else; on an 8 GB card, 1.8 GB is the
        difference between a game fitting and not. Backends that cannot tell
        return False and nothing changes.
        """
        return False

    # ── Permissions ──

    def check_permissions(self):
        """Return a list of human-readable strings describing missing OS
        permissions. Empty list means everything needed is granted."""
        return []
