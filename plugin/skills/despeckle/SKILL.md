---
description: Cleans the paint of a painted multicolor 3MF with SlicerBridge's despeckle_paint tool, merging paint islands too small to print into the color around them. Use when someone sees one-layer lines or specks of stray color in a painted model's slice or print, has a model painted from a textured or color OBJ, or asks to despeckle or clean up a model's paint.
---

# Despeckle a painted model

A model painted from a textured OBJ carries thousands of paint specks, each under a square millimetre: texture
edges rounded to the nearest filament. Where a layer's slice plane cuts one, the slicer prints that color for a
single layer, a stray line on the wall. `despeckle_paint` merges every same-color island smaller than a threshold
into the neighbor it shares the longest edge with, and writes a new file. The model itself is never changed.

## The steps

1. **Run `despeckle_paint` with `dry_run` first.** Tell the user how many islands it found, the area they cover,
   and how many triangles would change. If it finds few, the stray lines have another cause, such as a color
   boundary that runs along a layer on a shallow slope. Say so.
2. **Keep the default threshold, 1 mm²,** unless the user picks another. Thin streaks painted on purpose lose
   their fragments under it. If the model has fine painted detail (eye highlights, thin lines), offer a lower
   one such as 0.5.
3. **Write it**: `despeckle_paint` again without `dry_run`. The file goes beside the model as
   `<name> - despeckled.3mf` unless they give `out_path`.
4. **Report what came back**: the triangles recolored and their area, the largest from -> to moves, and every ⚠.
   A filament the result paints nowhere means the model needs one color fewer. Say which.
5. **Ask them to look at it in their slicer** before they print or publish it. To make files for other printers,
   convert the despeckled file.

## Rules

- Despeckle only when the user asks, or agrees after you suggest it. It changes the design.
- If `despeckle_paint` isn't among the tools, the plugin runs an older SlicerBridge. Say so.
