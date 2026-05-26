"""Tests for the URL builders and identifier helpers in ``endpoints._ids``.

Run with: ``venv/bin/python -m unittest tests.test_ids -v``
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from instagram_sdk.endpoints._ids import (  # noqa: E402
    extract_shortcode,
    normalize_username,
)


class ExtractShortcodeTests(unittest.TestCase):
    def test_accepts_canonical_11_char_shortcode(self) -> None:
        self.assertEqual(extract_shortcode("DYrlQReEbxS"), "DYrlQReEbxS")

    def test_accepts_extended_carousel_shortcode(self) -> None:
        # Instagram's home feed returns these compound codes for some carousel
        # containers (canonical shortcode + appended slot metadata). They were
        # rejected by the old 32-char cap.
        long_code = "DYsnevSDITohZaBObFfKbpduASzKFvC00zy_vc0"
        self.assertEqual(extract_shortcode(long_code), long_code)

    def test_extracts_from_post_url(self) -> None:
        self.assertEqual(
            extract_shortcode("https://www.instagram.com/p/DYrlQReEbxS/"),
            "DYrlQReEbxS",
        )

    def test_extracts_from_reel_url(self) -> None:
        self.assertEqual(
            extract_shortcode("https://www.instagram.com/reel/DYrlQReEbxS/"),
            "DYrlQReEbxS",
        )

    def test_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            extract_shortcode("")

    def test_rejects_invalid_characters(self) -> None:
        with self.assertRaises(ValueError):
            extract_shortcode("has spaces")
        with self.assertRaises(ValueError):
            extract_shortcode("has/slash")


class NormalizeUsernameTests(unittest.TestCase):
    def test_strips_at_sign_and_trailing_slash(self) -> None:
        self.assertEqual(normalize_username("@alice/"), "alice")

    def test_rejects_invalid(self) -> None:
        with self.assertRaises(ValueError):
            normalize_username("has spaces")


if __name__ == "__main__":
    unittest.main()
