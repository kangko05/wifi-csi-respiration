"""Offline tests. No serial port is opened and no hardware is required.

Each test module puts `src/` on `sys.path` itself, so
`python -m unittest discover -s tests` (which imports them as top-level
modules) works without installing the package.
"""
