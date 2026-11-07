"""Story structure for the silent-film demo. No encode."""

from __future__ import annotations

import unittest

from rubbish_video.drama import BUTTON, DURATION, HOOK, TWIST, story_lines, story_script
from rubbish_video.script import build_lines


class DramaScriptTests(unittest.TestCase):
    def test_length_hook_and_twist(self) -> None:
        self.assertGreaterEqual(DURATION, 25)
        self.assertLessEqual(DURATION, 35)
        lines = story_lines()
        cards = [line for line in lines if line.kind == "card"]
        self.assertEqual(cards[0].start, HOOK)
        self.assertGreaterEqual(cards[0].span, 2.5)
        self.assertLessEqual(cards[0].start + 1.0, cards[0].start + cards[0].span)
        self.assertTrue(any(line.kind == "mark" and line.text == "Fictional story" for line in lines))
        text = story_script()
        self.assertIn("Andito na ako.", text)
        self.assertIn("Walang umupo.", text)
        self.assertIn("Hindi siya nawala.", text)
        self.assertNotIn("是怎么回事", text)
        self.assertGreater(TWIST, 20)
        self.assertGreater(BUTTON, TWIST)
        self.assertAlmostEqual(cards[-1].start + cards[-1].span, DURATION)

    def test_template_still_has_eight_lines(self) -> None:
        self.assertEqual(len(build_lines("甲", "乙", "丙", 50)), 8)


if __name__ == "__main__":
    unittest.main()
