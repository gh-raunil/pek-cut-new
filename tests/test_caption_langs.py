"""Test caption language options: hinglish, hindi, and english."""
import os
import sys
import tempfile
import unittest

os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-caplang-")
os.environ["DATABASE_URL"] = ""
sys.path.insert(0, ".")

from studio import brain, jobs, render, roman


class TestCaptionLanguages(unittest.TestCase):
    def test_caption_lang_clean_options(self):
        self.assertEqual(jobs.clean_options({"caption_lang": "hindi"}), {"caption_lang": "hindi"})
        self.assertEqual(jobs.clean_options({"caption_lang": "hinglish"}), {"caption_lang": "hinglish"})
        self.assertEqual(jobs.clean_options({"caption_lang": "english"}), {"caption_lang": "english"})
        self.assertEqual(jobs.clean_options({"caption_lang": "french"}), {})

    def test_devanagari_drawable(self):
        self.assertTrue(roman.drawable("नमस्ते"))
        self.assertTrue(roman.drawable("वीडियो"))
        self.assertTrue(roman.drawable("Hello 123!"))

    def test_caption_words_hinglish(self):
        words = [{"w": "चूना", "s": 0.0, "e": 0.5}, {"w": "लगा", "s": 0.5, "e": 1.0}]
        # without AI, fallback Roman words:
        old_ai = brain.ask_ai
        try:
            brain.ask_ai = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no ai"))
            out = brain.caption_words_for_lang(words, "hinglish", job_lang="hi")
            self.assertEqual(" ".join(w["w"] for w in out), "chuna laga")
        finally:
            brain.ask_ai = old_ai

    def test_caption_words_hindi(self):
        words = [{"w": "नमस्ते", "s": 0.0, "e": 0.5}, {"w": "दोस्तो", "s": 0.5, "e": 1.0}]
        old_ai = brain.ask_ai
        try:
            brain.ask_ai = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no ai"))
            out = brain.caption_words_for_lang(words, "hindi", job_lang="hi")
            self.assertEqual(" ".join(w["w"] for w in out), "नमस्ते दोस्तो")
        finally:
            brain.ask_ai = old_ai

    def test_hindi_ass_rendering(self):
        words = [{"w": "नमस्ते", "s": 0.5, "e": 1.2}, {"w": "दोस्तों", "s": 1.2, "e": 1.8}]
        ass = render.build_ass(words, 0.0, 2.0, "", "bounce", [], 200, 1400)
        self.assertIn("नमस्ते", ass)
        self.assertIn("दोस्तों", ass)
        self.assertIn("Dialogue: 2", ass)  # active highlighted dialogue event


if __name__ == "__main__":
    unittest.main()
