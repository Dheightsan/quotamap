import contextlib
import io
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class DemoTest(unittest.TestCase):
    def test_demo_output_matches_the_readme_example(self):
        r = subprocess.run([sys.executable, str(ROOT / "examples" / "make_demo.py")],
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        expected = (ROOT / "docs" / "example-output.txt").read_text(encoding="utf-8")
        self.assertEqual(r.stdout.replace("\r\n", "\n"), expected.replace("\r\n", "\n"))
        self.assertIn(r.stdout.splitlines()[0], (ROOT / "README.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
