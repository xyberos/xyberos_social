"""The three theme choices a reader can make, and how they become a document attribute.

"System" is the absence of an explicit choice, not a fourth palette: the server leaves
`data-theme` off and the stylesheet's `prefers-color-scheme` block decides. That keeps the
first paint correct without a script and without the server having to know the operating
system's preference.
"""

SYSTEM = "system"
LIGHT = "light"
DARK = "dark"

THEME_CHOICES = (SYSTEM, LIGHT, DARK)

THEME_OPTIONS = (
    {"value": SYSTEM, "label": "System", "icon": "brightness_auto", "hint": "Match this device"},
    {"value": LIGHT, "label": "Light", "icon": "light_mode", "hint": "Always light"},
    {"value": DARK, "label": "Dark", "icon": "dark_mode", "hint": "Always dark"},
)


def resolve_theme(choice: str | None) -> tuple[str, str]:
    """Return the valid stored choice and the explicit `data-theme` value for it."""
    if choice not in THEME_CHOICES:
        choice = SYSTEM
    return choice, ("" if choice == SYSTEM else choice)


def option_for(choice: str) -> dict:
    return next(option for option in THEME_OPTIONS if option["value"] == choice)
