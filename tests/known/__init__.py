"""Known 1.9.1 read-path defects, pinned as strict xfails.

One file per fix: each test asserts the fixed behaviour and carries
``xfail(strict=True)`` whose reason names the audit item and the 1.10
stream that fixes it. That stream removes the markers in its own file
only, so an accidental fix (or a fix that doesn't hold) shows up as an
XPASS or a failure.
"""
