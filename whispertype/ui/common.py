"""Menu content shared by the Windows tray and the macOS menu bar.

Both front-ends offer the same choices; only the widget toolkit differs. Keeping
the lists here is what stops the two menus drifting apart.
"""

#: Language submenu. `language` is read fresh for every job, so switching it
#: takes effect on the next dictation with no restart.
LANGUAGES = [
    ("en", "English"), ("hu", "Magyar"), ("de", "Deutsch"),
    ("fr", "Français"), ("es", "Español"), ("it", "Italiano"),
    ("pt", "Português"), ("nl", "Nederlands"), ("pl", "Polski"),
    ("ru", "Русский"), ("ja", "日本語"), ("zh", "中文"),
]

#: Idle-unload choices. Reloading from the local cache costs about a second, so
#: holding ~1.6 GB resident all day for a tool used a few times an hour is a bad
#: trade — but it is a trade, so it is a setting.
IDLE_CHOICES = [
    (0, "Never (keep resident)"), (2, "After 2 minutes"),
    (5, "After 5 minutes"), (10, "After 10 minutes"),
    (30, "After 30 minutes"), (60, "After 1 hour"),
]

ENGINES = [
    ("local", "Local (this machine)"),
    ("openai", "OpenAI API"),
]
