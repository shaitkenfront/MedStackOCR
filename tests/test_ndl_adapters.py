from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from ocr.ndlocr_lite_adapter import NdlOcrLiteAdapter


class NdlAdaptersTest(unittest.TestCase):
    def test_ndlocr_lite_run_parses_json(self) -> None:
        script = (
            "import argparse,json,pathlib;"
            "p=argparse.ArgumentParser();"
            "p.add_argument('--sourceimg');"
            "p.add_argument('--output');"
            "p.add_argument('--device');"
            "p.add_argument('--viz');"
            "a,_=p.parse_known_args();"
            "out=pathlib.Path(a.output);"
            "out.mkdir(parents=True, exist_ok=True);"
            "stem=pathlib.Path(a.sourceimg).stem;"
            "payload={'contents': [[{'text': 'ndl-lite-line', 'boundingBox': [[0,0],[0,10],[30,10],[30,0]], 'confidence': 0.91}]]};"
            "(out / f'{stem}.json').write_text(json.dumps(payload), encoding='utf-8')"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            image = Path(tmp_dir) / "sample.jpg"
            image.write_bytes(b"fake-image")
            adapter = NdlOcrLiteAdapter(command=[sys.executable, "-c", script], device="cpu")
            result = adapter.run(str(image))

        self.assertEqual(result.engine, "ndlocr-lite")
        self.assertEqual(len(result.payload), 1)
        self.assertEqual(result.payload[0]["text"], "ndl-lite-line")
        self.assertAlmostEqual(float(result.payload[0]["confidence"]), 0.91, places=3)

    def test_healthcheck_false_for_missing_command(self) -> None:
        adapter = NdlOcrLiteAdapter(command="__missing_ndlocr_command_123__")
        self.assertFalse(adapter.healthcheck())


if __name__ == "__main__":
    unittest.main()
