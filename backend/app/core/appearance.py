"""Curated, accessible account appearance preferences.

The stored values are identifiers, not arbitrary CSS. That keeps a preference safe to render
on every page and lets the stylesheet preserve usable contrast for each combination.
"""

PRESETS = (
    {"value": "forest", "label": "Forest", "hint": "Calm greens", "swatch": "#245b48"},
    {"value": "ocean", "label": "Ocean", "hint": "Clear blues", "swatch": "#176b87"},
    {"value": "violet", "label": "Violet", "hint": "Soft purples", "swatch": "#6948a8"},
    {"value": "sunset", "label": "Sunset", "hint": "Warm terracotta", "swatch": "#a94c35"},
    {"value": "midnight", "label": "Midnight", "hint": "Deep indigo", "swatch": "#354b9b"},
)
BACKGROUND_OPTIONS = (
    {"value": "paper", "label": "Paper", "hint": "Quiet, solid background"},
    {"value": "mist", "label": "Mist", "hint": "Subtle cool gradient"},
    {"value": "dawn", "label": "Dawn", "hint": "Warm, soft gradient"},
    {"value": "aurora", "label": "Aurora", "hint": "Luminous green-blue gradient"},
)
DENSITY_OPTIONS = (
    {"value": "comfortable", "label": "Comfortable", "hint": "More breathing room"},
    {"value": "compact", "label": "Compact", "hint": "Fit more on screen"},
)

PRESET_VALUES = frozenset(option["value"] for option in PRESETS)
BACKGROUND_VALUES = frozenset(option["value"] for option in BACKGROUND_OPTIONS)
DENSITY_VALUES = frozenset(option["value"] for option in DENSITY_OPTIONS)
