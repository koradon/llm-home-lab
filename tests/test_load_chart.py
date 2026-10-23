import pytest

from llm_home_lab.tui.load_chart import BASELINE, GREEN, ORANGE, RED, bar_level, bar_style


@pytest.mark.parametrize(
    ("ratio", "level"),
    [(0.0, 0), (1 / 32, 1), (0.25, 8), (0.5, 16), (0.875, 28), (1.0, 32), (1.5, 32), (-1.0, 0)],
)
def test_bar_level_uses_32_levels_on_a_fixed_scale(ratio, level):
    assert bar_level(ratio, height=4) == level


@pytest.mark.parametrize(
    ("ratio", "style"),
    [
        (1.0, GREEN),
        (0.875, GREEN),
        (0.70, GREEN),
        (0.6999, ORANGE),
        (0.50, ORANGE),
        (0.30, ORANGE),
        (0.2999, RED),
        (0.10, RED),
        (0.0, RED),
    ],
)
def test_bar_colour_follows_the_thresholds_exactly(ratio, style):
    assert bar_style(ratio) == style


def test_palette_uses_distinct_colours_and_a_dim_baseline():
    assert len({GREEN, ORANGE, RED, BASELINE}) == 4
