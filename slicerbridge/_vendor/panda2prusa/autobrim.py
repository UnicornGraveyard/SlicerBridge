"""Approximate Bambu Studio / OrcaSlicer's automatic brim width for PrusaSlicer.

Bambu's "Auto" brim type picks a brim width per object from how likely it is to tip
over (tall and slender, printed fast) or to warp (a large footprint in a shrinking
material). PrusaSlicer has no such mode, so the width is computed here with Orca's
formula (``configBrimWidthByVolumeGroups`` in Brim.cpp) and written as an outer brim.

Differences from Orca: the footprint is a cut of the mesh at half the first layer
height, not the sliced first layer after elephant foot compensation, and the whole
object is one group, where Orca computes a width per group of touching parts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

# Orca's MaterialType table: filament type -> (adhesion coefficient, thermal length).
_MATERIALS = {
    "ABS": (1, 100), "ABS-CF": (1, 100), "ABS-GF": (1, 100), "ASA": (1, 100), "ASA-CF": (1, 100),
    "ASA-GF": (1, 100), "ASA-AERO": (1, 100), "BVOH": (1, 200), "CoPE": (1, 200), "EVA": (1, 200),
    "FLEX": (0.5, 1000), "HIPS": (1, 200), "PA": (1, 100), "PA-CF": (1, 100), "PA-GF": (1, 100),
    "PA6": (1, 100), "PA6-CF": (1, 100), "PA6-GF": (1, 100), "PA11": (1, 100), "PA11-CF": (1, 100),
    "PA11-GF": (1, 100), "PA12": (1, 100), "PA12-CF": (1, 100), "PA12-GF": (1, 100),
    "PAHT": (1, 200), "PAHT-CF": (1, 200), "PAHT-GF": (1, 200), "PC": (1, 40), "PC-ABS": (1, 80),
    "PC-CF": (1, 80), "PC-PBT": (1, 40), "PCL": (1, 200), "PCTG": (2, 200), "PE": (1, 200),
    "PE-CF": (1, 200), "PE-GF": (1, 200), "PEI-1010": (1, 200), "PEI-1010-CF": (1, 200),
    "PEI-1010-GF": (1, 200), "PEI-9085": (1, 200), "PEI-9085-CF": (1, 200), "PEI-9085-GF": (1, 200),
    "PEEK": (1, 200), "PEEK-CF": (1, 200), "PEEK-GF": (1, 200), "PEKK": (1, 200), "PEKK-CF": (1, 200),
    "PES": (1, 200), "PET": (2, 100), "PET-CF": (2, 100), "PET-GF": (2, 100), "PETG": (2, 100),
    "PETG-CF": (1, 100), "PETG-GF": (1, 100), "PHA": (1, 200), "PI": (1, 200), "PLA": (1, 200),
    "PLA-AERO": (1, 200), "PLA-CF": (1, 200), "POM": (1, 200), "PP": (1, 200), "PP-CF": (1, 200),
    "PP-GF": (1, 200), "PPA-CF": (1, 200), "PPA-GF": (1, 200), "PPS": (1, 200), "PPS-CF": (1, 200),
    "PPSU": (1, 200), "PSU": (1, 200), "PVA": (1, 200), "PVB": (1, 200), "PVDF": (1, 200),
    "SBS": (1, 200), "TPI": (1, 200), "TPU": (0.5, 1000),
}
_DEFAULT_MATERIAL = (1.0, 200.0)

# Speeds whose maximum feeds the tipping estimate (Orca's Model::findMaxSpeed).
SPEED_KEYS = ("inner_wall_speed", "outer_wall_speed", "sparse_infill_speed",
              "internal_solid_infill_speed", "top_surface_speed", "support_speed")


def material(filament_type: Optional[str]) -> tuple[float, float]:
    """(adhesion coefficient, thermal length in mm) of a filament type."""
    return _MATERIALS.get(filament_type or "", _DEFAULT_MATERIAL)


@dataclass
class Footprint:
    """Area properties of a horizontal cut through an object, in mm."""

    area: float = 0.0
    perimeter: float = 0.0
    ixx: float = 0.0  # second moment of area about the horizontal axis through the centroid
    iyy: float = 0.0  # ... about the vertical axis through the centroid
    width_x: float = 0.0
    width_y: float = 0.0


class FootprintBuilder:
    """Accumulates a cut at height ``z`` from triangles, without joining the outlines.

    Each triangle that crosses the plane adds one edge of the cut, oriented so the
    material is on its left (from the triangle's outward normal). Area, first and second
    moments are sums over edges (Green's theorem), so holes subtract on their own.
    """

    def __init__(self, z: float):
        self.z = z
        self.a = self.sx = self.sy = self.sxx = self.syy = self.length = 0.0
        self.min_x = self.min_y = math.inf
        self.max_x = self.max_y = -math.inf

    def add_triangle(self, p1, p2, p3) -> None:
        """Add a triangle with outward-facing counter-clockwise vertices (x, y, z)."""
        z = self.z
        pts = []
        for a, b in ((p1, p2), (p2, p3), (p3, p1)):
            if (a[2] < z) != (b[2] < z):
                t = (z - a[2]) / (b[2] - a[2])
                pts.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
        if len(pts) != 2:
            return
        (xa, ya), (xb, yb) = pts
        # Outward normal's horizontal part; the edge runs along (-ny, nx).
        ux, uy, uz = p2[0] - p1[0], p2[1] - p1[1], p2[2] - p1[2]
        vx, vy, vz = p3[0] - p1[0], p3[1] - p1[1], p3[2] - p1[2]
        nx, ny = uy * vz - uz * vy, uz * vx - ux * vz
        if (xb - xa) * -ny + (yb - ya) * nx < 0:
            xa, ya, xb, yb = xb, yb, xa, ya
        cross = xa * yb - xb * ya
        self.a += cross
        self.sx += (xa + xb) * cross
        self.sy += (ya + yb) * cross
        self.sxx += (xa * xa + xa * xb + xb * xb) * cross
        self.syy += (ya * ya + ya * yb + yb * yb) * cross
        self.length += math.hypot(xb - xa, yb - ya)
        self.min_x, self.max_x = min(self.min_x, xa, xb), max(self.max_x, xa, xb)
        self.min_y, self.max_y = min(self.min_y, ya, yb), max(self.max_y, ya, yb)

    def result(self) -> Optional[Footprint]:
        area = self.a / 2
        if area <= 1e-9:
            return None
        cx, cy = self.sx / (6 * area), self.sy / (6 * area)
        return Footprint(
            area=area,
            perimeter=self.length,
            ixx=self.syy / 12 - area * cy * cy,
            iyy=self.sxx / 12 - area * cx * cx,
            width_x=self.max_x - self.min_x,
            width_y=self.max_y - self.min_y,
        )


def brim_width(
    footprint: Footprint,
    height: float,
    max_speed: float,
    adhesion: float,
    thermal_length_ref: float,
    line_spacing: float,
) -> float:
    """Orca's automatic brim width in mm (0 for no brim), rounded to whole brim lines."""
    fp = footprint
    if fp.ixx <= 0 or fp.iyy <= 0:
        return 0.0
    thermal_length = math.hypot(fp.width_x, fp.width_y)
    height_to_area = max(height / fp.ixx * fp.width_y, height / fp.iyy * fp.width_x) * height / 1920
    raw = adhesion * min(
        max(height_to_area * max_speed, thermal_length * 8.0 / thermal_length_ref * min(height, 30.0) / 30.0),
        18.0,
        1.5 * thermal_length,
    )
    if raw < 5 and raw < 1.5 * thermal_length:
        raw = 0.0  # small brims are omitted
    raw = min(raw, 18.0)

    def lines(w: float) -> float:
        return math.floor(w / line_spacing / 2) * line_spacing * 2 if line_spacing > 0 else w

    width = lines(raw)
    # A tall, thin part (area / perimeter under 1.1 mm) without a brim gets 5 mm.
    if width < 5 and height > 10 and width < line_spacing and fp.area / fp.perimeter < 1.1:
        width += lines(5.0)
    # Never wider than the footprint itself.
    return lines(min(width, max(fp.width_x, fp.width_y)))
