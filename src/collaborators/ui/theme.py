"""The collaborators colour themes (dark and light).

Roles map onto theme colours so every theme stays coherent:
Lead = $primary (violet), workers = $secondary (blue), testers = $success (green).
"""

from textual.theme import Theme

DARK = Theme(
    name="collaborators",
    primary="#A78BFA",
    secondary="#60A5FA",
    accent="#F472B6",
    foreground="#E6E8EF",
    background="#0D1017",
    surface="#131722",
    panel="#1A1F2D",
    success="#34D399",
    warning="#FBBF24",
    error="#F87171",
    dark=True,
)

LIGHT = Theme(
    name="collaborators-light",
    primary="#7C3AED",
    secondary="#2563EB",
    accent="#DB2777",
    foreground="#1F2330",
    background="#F7F7FA",
    surface="#FFFFFF",
    panel="#ECEDF3",
    success="#059669",
    warning="#B45309",
    error="#DC2626",
    dark=False,
)

THEMES = (DARK, LIGHT)
