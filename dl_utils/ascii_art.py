"""DeadMoon's TTY-safe kinetic moon boot sequence."""

from __future__ import annotations

import sys
import time

BANNER = "D E A D M O O N   D O W N L O A D   B O T   V 3"
_MOONS = ("🌑", "🌒", "🌓", "🌔", "🌕", "🌔", "🌓", "🌒")
_FRAMES = (
    (
        r"        .  *      .",
        r"     .----.",
        r"   .'      '.      ✦",
        "  /          \\",
        r" |    DeadMoon  |",
        r"  \          /",
        r"   '.______.'",
    ),
    (
        r"   *       .      .",
        r"     .----.",
        r"   .'   _   '.",
        "  /   _( )_   \\",
        r" |  /  DeadMoon \ |",
        r"  \ \  (__)    / /",
        r"   '.________.'",
    ),
    (
        r"  .       ✦       .",
        r"      .-~~~-.",
        r"   .-'   _    '-.",
        "  /   .-' '-.    \\",
        r" |  / DeadMoon \  |",
        r"  \  '-._____.-'  /",
        r"   '-.________.-'",
    ),
    (
        r" ✦       .       *",
        r"      .-~~~-.",
        r"   .-'       '-.",
        "  /   .~~~~~.   \\",
        r" |  | DeadMoon | |",
        r"  \   '~~~~~'   /",
        r"   '-._______.-'",
    ),
)
_STAGES = (
    "Tuning frequencies...      ",
    "Loading provider engines...",
    "Syncing relay protocols... ",
    "Moonlight Engine Ready.    ",
)


def get_moon_frame(frame_idx: int) -> str:
    """Return one of four deterministic ASCII frames and its loading stage."""
    index = int(frame_idx) % 4
    progress = (index + 1) * 25
    bars = "#" * (index + 1) * 3
    body = "\n".join(_FRAMES[index])
    return (
        f"{BANNER}\n{_MOONS[index]} DeadMoon\n{body}\n"
        f"{progress:3d}% [{bars:<12}] {_STAGES[index]}"
    )


def run_boot_animation(stream=None, sleep=time.sleep) -> None:
    """Play 12 kinetic frames on a TTY, or one clean Docker-log frame."""
    output = stream or sys.stdout
    if not output.isatty():
        print(get_moon_frame(0), file=output)
        print(BANNER, file=output)
        print(f"100% [{'#' * 12}] {_STAGES[-1]}", file=output, flush=True)
        return
    print(BANNER, file=output)
    for index in range(12):
        print("\x1b[2J\x1b[H", end="", file=output)
        print(BANNER, file=output)
        print(get_moon_frame(index), file=output, flush=True)
        sleep(0.08)
    print(file=output, flush=True)


if __name__ == "__main__":
    run_boot_animation()
