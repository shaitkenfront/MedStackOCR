from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from ocr.deepseek_adapter import DeepSeekOCRAdapter


class _FakeDeepSeekClient:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs

    def parse(self, file_path: str, mode: str, dpi: int) -> str:
        _ = (file_path, mode, dpi)
        return "api-1\napi-2"


class _FakeTorchCuda:
    @staticmethod
    def is_available() -> bool:
        return False


class _FakeTorch:
    cuda = _FakeTorchCuda()
    bfloat16 = "bfloat16"
    float16 = "float16"
    float32 = "float32"


class _FakeTokenizer:
    @classmethod
    def from_pretrained(cls, model_name: str, trust_remote_code: bool = True) -> "_FakeTokenizer":
        _ = (model_name, trust_remote_code)
        return cls()


class _FakeModel:
    @classmethod
    def from_pretrained(cls, model_name: str, **kwargs: object) -> "_FakeModel":
        _ = (model_name, kwargs)
        return cls()

    def eval(self) -> "_FakeModel":
        return self

    def cuda(self) -> "_FakeModel":
        return self

    def infer(self, tokenizer: object, **kwargs: object) -> None:
        _ = tokenizer
        output_path = Path(str(kwargs["output_path"]))
        output_path.mkdir(parents=True, exist_ok=True)
        (output_path / "result.mmd").write_text("local-1\nlocal-2", encoding="utf-8")


class DeepSeekAdapterTest(unittest.TestCase):
    def test_markdown_to_lines(self) -> None:
        text = "1行目\n\n2行目\n3行目"
        lines = DeepSeekOCRAdapter._markdown_to_lines(text)  # noqa: SLF001
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0]["text"], "1行目")
        self.assertEqual(lines[1]["text"], "2行目")
        self.assertEqual(lines[2]["text"], "3行目")
        self.assertTrue(all("bbox" in line for line in lines))

    def test_api_backend_run(self) -> None:
        fake_deepseek_module = types.ModuleType("deepseek_ocr")
        fake_deepseek_module.DeepSeekOCR = _FakeDeepSeekClient
        fake_deepseek_module.__version__ = "test-api"

        with mock.patch.dict(sys.modules, {"deepseek_ocr": fake_deepseek_module}):
            adapter = DeepSeekOCRAdapter(backend="api", api_key="dummy")
            self.assertTrue(adapter.healthcheck())
            result = adapter.run("dummy.jpg")

        self.assertEqual(result.engine, "deepseek")
        self.assertEqual(result.metadata["backend"], "api")
        self.assertEqual(len(result.payload), 2)
        self.assertEqual(result.payload[0]["text"], "api-1")

    def test_local_backend_run(self) -> None:
        fake_torch_module = types.ModuleType("torch")
        fake_torch_module.cuda = _FakeTorch.cuda
        fake_torch_module.bfloat16 = _FakeTorch.bfloat16
        fake_torch_module.float16 = _FakeTorch.float16
        fake_torch_module.float32 = _FakeTorch.float32

        fake_transformers_module = types.ModuleType("transformers")
        fake_transformers_module.AutoModel = _FakeModel
        fake_transformers_module.AutoTokenizer = _FakeTokenizer

        with tempfile.TemporaryDirectory() as tmp_dir:
            with mock.patch.dict(
                sys.modules,
                {
                    "torch": fake_torch_module,
                    "transformers": fake_transformers_module,
                },
            ):
                adapter = DeepSeekOCRAdapter(
                    backend="local",
                    model_name="deepseek-ai/DeepSeek-OCR",
                    local_output_dir=tmp_dir,
                    local_dtype="bfloat16",
                )
                self.assertTrue(adapter.healthcheck())
                result = adapter.run("dummy.jpg")

            self.assertTrue((Path(tmp_dir) / "result.mmd").exists())
        self.assertEqual(result.metadata["backend"], "local")
        self.assertEqual(len(result.payload), 2)
        self.assertEqual(result.payload[0]["text"], "local-1")


if __name__ == "__main__":
    unittest.main()
