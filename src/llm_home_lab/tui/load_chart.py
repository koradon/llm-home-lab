from rich.style import Style
from rich.text import Text
from textual.widgets import Sparkline

BARS = " ▁▂▃▄▅▆▇█"
LEVELS_PER_ROW = len(BARS) - 1

GREEN_AT = 0.70
ORANGE_AT = 0.30

# xterm-256 palette indices so Terminal.app renders the same colours as truecolor terminals.
GREEN = Style(color="color(34)")
ORANGE = Style(color="color(208)")
RED = Style(color="color(196)")
BASELINE = Style(color="color(240)")


def bar_level(ratio: float, height: int) -> int:
    """Filled eighth-block levels (0..8*height) for a load ratio on the fixed 0..1 scale."""
    return round(min(max(ratio, 0.0), 1.0) * LEVELS_PER_ROW * height)


def bar_style(ratio: float) -> Style:
    if ratio >= GREEN_AT:
        return GREEN
    if ratio >= ORANGE_AT:
        return ORANGE
    return RED


class LoadChart(Sparkline):
    """A load chart drawn on a fixed 0..1 scale, one column per poll sample.

    Textual's Sparkline scales to the min and max of its own data, so a node that stays at
    100% (or 0%) load has no range and collapses to a flat line at the bottom. Load ratios
    are absolute, so here 1.0 is always full height and equal loads always look equal. Each
    bar is coloured as a whole by its own value; an empty bar keeps a dim one-cell baseline
    so an idle or offline node still shows a chart.
    """

    def render(self) -> Text:
        width, height = self.size.width, self.size.height
        samples = list(self.data or [])[-width:] if width > 0 else []
        if not samples or height <= 0:
            return Text()

        levels = [bar_level(ratio, height) for ratio in samples]
        styles = [bar_style(ratio) for ratio in samples]

        text = Text(no_wrap=True)
        for row in reversed(range(height)):
            for level, style in zip(levels, styles, strict=True):
                filled = min(max(level - row * LEVELS_PER_ROW, 0), LEVELS_PER_ROW)
                if row == 0 and level == 0:
                    text.append("▁", BASELINE)
                else:
                    text.append(BARS[filled], style)
            if row:
                text.append("\n")
        return text
