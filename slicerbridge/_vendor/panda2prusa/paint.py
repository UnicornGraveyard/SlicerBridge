"""Decode / re-encode triangle paint RLE strings (TriangleSelector serialization).

Bambu Studio's ``paint_color`` and PrusaSlicer's ``slic3rpe:mmu_segmentation`` share
this format (Bambu forked PrusaSlicer's TriangleSelector). The hex string is a
bitstream read nibble-by-nibble from the END of the string, bits LSB-first within
each nibble. Node grammar::

    node := split:2 ( state:2 [ext:4 if state==0b11, value ext+3]   -- leaf
                    | special_side:2 node{split+1} )                -- split

State 0 is "use the volume's extruder"; state N paints extruder N. Remapping paint
to different extruder numbers therefore requires rewriting leaf states in the
bitstream — the strings cannot be value-preserved when extruders are renumbered.
"""

from __future__ import annotations


class PaintError(ValueError):
    pass


def _bits_of(code: str) -> list[int]:
    bits: list[int] = []
    for ch in reversed(code):
        try:
            n = int(ch, 16)
        except ValueError:
            raise PaintError(f"non-hex character {ch!r} in paint string")
        bits.extend((n >> i) & 1 for i in range(4))
    return bits


def _string_of(bits: list[int]) -> str:
    while len(bits) % 4:
        bits.append(0)
    chars = []
    for i in range(0, len(bits), 4):
        nibble = bits[i] | bits[i + 1] << 1 | bits[i + 2] << 2 | bits[i + 3] << 3
        chars.append("0123456789ABCDEF"[nibble])
    return "".join(reversed(chars))


def _transform(code: str, map_fn) -> str:
    bits = _bits_of(code)
    out: list[int] = []
    pos = 0

    def take(k: int) -> int:
        nonlocal pos
        if pos + k > len(bits):
            raise PaintError("truncated paint bitstream")
        v = sum(bits[pos + i] << i for i in range(k))
        pos += k
        return v

    def emit(v: int, k: int) -> None:
        out.extend((v >> i) & 1 for i in range(k))

    def node() -> None:
        split = take(2)
        emit(split, 2)
        if split == 0:
            s = take(2)
            if s == 0b11:
                s = take(4) + 3
            s = map_fn(s)
            if s < 0 or s > 18:
                raise PaintError(f"state {s} out of range for TriangleSelector encoding")
            if s <= 2:
                emit(s, 2)
            else:
                emit(0b11, 2)
                emit(s - 3, 4)
        else:
            emit(take(2), 2)  # special side index
            for _ in range(split + 1):
                node()

    node()
    if any(bits[pos:]) and len(bits) - pos >= 4:
        raise PaintError("leftover data bits after root node")
    return _string_of(out)


def transform_states(code: str, mapping: dict[int, int]) -> str:
    """Rewrite every leaf state through ``mapping`` (states absent stay unchanged).

    With an identity mapping this round-trips byte-for-byte (modulo zero padding,
    which producers already emit as zeros).
    """
    return _transform(code, lambda s: mapping.get(s, s))


def used_states(code: str) -> set[int]:
    """Return the set of leaf states referenced by one paint string."""
    states: set[int] = set()

    def spy(s: int) -> int:
        states.add(s)
        return s

    _transform(code, spy)
    return states
