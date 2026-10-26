"""The one error SlicerBridge raises on purpose."""


class Refusal(Exception):
    """A conversion SlicerBridge won't make, and why.

    Raised instead of writing a file that would print wrong: more colors than the
    printer holds, a filament type with no preset, settings the slicer won't load,
    a check that failed. The message says what to do about it.
    """
