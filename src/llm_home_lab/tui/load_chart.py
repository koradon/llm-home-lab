from rich.style import Style
from rich.text import Text
from textual.widgets import Sparkline

BARS = " ▁▂▃▄▅▆▇█"


class LoadChart(Sparkline):
    """A Sparkline drawn on a fixed 0..1 scale.

    Textual's Sparkline scales to the min and max of its own data, so a node that stays at
    100% (or 0%) load has no range and collapses to a flat line at the bottom. Load ratios
    are absolute, so here 1.0 is always full height and 0.0 is always empty.
    """

    def render(self) -> Text:
        width, height = self.size.width, self.size.height
        data = list(self.data or [])
        if not data or width <= 0 or height <= 0:
            return Text()

        columns = [
            max(
                data[
                    c * len(data) // width : max(
                        (c + 1) * len(data) // width, c * len(data) // width + 1
                    )
                ]
            )
            for c in range(width)
        ]
        levels = [round(min(max(value, 0.0), 1.0) * len(BARS[1:]) * height) for value in columns]

        style = Style(color="green")
        text = Text(no_wrap=True)
        for row in reversed(range(height)):
            low = row * len(BARS[1:])
            line = "".join(BARS[min(max(level - low, 0), len(BARS) - 1)] for level in levels)
            text.append(line, style)
            if row:
                text.append("\n")
        return text
