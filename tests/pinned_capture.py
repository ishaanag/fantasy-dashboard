"""Import in any test module that reads Yahoo captures: it pins the archived regression capture.

The suite reads `data/reports/archive-2026-10-20T1411Z` (private, Git ignored, kept locally) through
FANTASY_REPORTS_DIR, so results do not move when the live capture is refreshed. Without the archive (for example
in CI) the importing module is skipped rather than failed.
"""
import os
import unittest

from support import PINNED_REPORTS

if not PINNED_REPORTS.is_dir():
    raise unittest.SkipTest(f'private regression capture not found: {PINNED_REPORTS}')
os.environ.setdefault('FANTASY_REPORTS_DIR', str(PINNED_REPORTS))
