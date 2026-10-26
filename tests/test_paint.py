"""The paint decoder the checks use, against strings with known meaning."""
from collections import Counter

import pytest

from builders import leaf
from slicerbridge._vendor.panda2prusa.paint import transform_states
from slicerbridge.checks.u1 import paint_leaves


@pytest.mark.parametrize("state", [1, 2, 3, 4, 10, 15])
def test_whole_triangle_leaves(state):
    assert paint_leaves(leaf(state).encode()) == Counter({state: 1})


def test_a_split_triangle():
    # bl2u1's test string: a triangle split in two halves, filament 1 and the part's own (state 0)
    assert paint_leaves(b"041") == Counter({1: 1, 0: 1})


def test_remapping_moves_paint_to_the_new_filament():
    for old, new in ((3, 2), (5, 3), (10, 1), (1, 4)):
        assert paint_leaves(transform_states(leaf(old), {old: new}).encode()) == Counter({new: 1})
