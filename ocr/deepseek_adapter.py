from __future__ import annotations

import contextlib
import io
import os
import tempfile
from pathlib import Path
from typing import Any

from core.models import OCRRawResult
from ocr.base import OCRAdapterError


class DeepSeekOCRAdapter:
    name = "deepseek"
    version = "unknown"

    def __init__(
        self,
        api_key_env: str = "DS_OCR_API_KEY",
        api_key: str | None = None,
        base_url: str | None = None,
        model_name: str | None = None,
        backend: str = "api",
        mode: str = "free_ocr",
        dpi: int = 200,
        local_prompt: str | None = None,
        local_output_dir: str | None = None,
        local_base_size: int = 512,
        local_image_size: int = 512,
        local_crop_mode: bool = False,
        local_device: str = "cuda",
        local_dtype: str = "bfloat16",
        local_attn_impl: str = "eager",
        local_trust_remote_code: bool = True,
    ) -> None:
        self.api_key_env = api_key_env
        self.api_key = api_key or os.getenv(api_key_env)
        self.base_url = base_url
        self.model_name = model_name
        self.backend = (backend or "api").strip().lower()
        self.mode = mode
        self.dpi = dpi
        self.local_prompt = local_prompt or self._default_local_prompt(mode)
        self.local_output_dir = local_output_dir
        self.local_base_size = max(1, int(local_base_size))
        self.local_image_size = max(1, int(local_image_size))
        self.local_crop_mode = bool(local_crop_mode)
        self.local_device = local_device
        self.local_dtype = local_dtype
        self.local_attn_impl = local_attn_impl
        self.local_trust_remote_code = bool(local_trust_remote_code)
        self._client_cls: Any = None
        self._client: Any = None
        self._auto_model_cls: Any = None
        self._auto_tokenizer_cls: Any = None
        self._local_model: Any = None
        self._local_tokenizer: Any = None
        self._torch_module: Any = None
        self._load_dependency()

    def _load_dependency(self) -> None:
        if self.backend == "api":
            try:
                import deepseek_ocr  # type: ignore
                from deepseek_ocr import DeepSeekOCR  # type: ignore
            except Exception as exc:
                raise OCRAdapterError(
                    "deepseek api backend requires `deepseek-ocr` package."
                ) from exc

            self._client_cls = DeepSeekOCR
            self.version = f"api-{getattr(deepseek_ocr, '__version__', 'unknown')}"
            return

        if self.backend == "local":
            try:
                import torch  # type: ignore
                from transformers import AutoModel, AutoTokenizer  # type: ignore
            except Exception as exc:
                raise OCRAdapterError(
                    "deepseek local backend requires `torch` and `transformers` packages."
                ) from exc

            self._torch_module = torch
            self._auto_model_cls = AutoModel
            self._auto_tokenizer_cls = AutoTokenizer
            model_id = self.model_name or "deepseek-ai/DeepSeek-OCR"
            self.version = f"local-{model_id}"
            return

        raise OCRAdapterError(
            f"unsupported deepseek backend: {self.backend}. use `api` or `local`."
        )

    def _ensure_client(self) -> None:
        if self._client is not None:
            return
        if not self.api_key:
            raise OCRAdapterError(
                f"deepseek API key is missing. Set {self.api_key_env} or configure api_key."
            )
        kwargs: dict[str, Any] = {
            "api_key": self.api_key,
            "dpi": self.dpi,
        }
        if self.base_url:
            kwargs["base_url"] = self.base_url
        if self.model_name:
            kwargs["model_name"] = self.model_name
        self._client = self._client_cls(**kwargs)

    def _ensure_local_model(self) -> None:
        if self._local_model is not None and self._local_tokenizer is not None:
            return
        if (
            self._auto_model_cls is None
            or self._auto_tokenizer_cls is None
            or self._torch_module is None
        ):
            raise OCRAdapterError("deepseek local backend is not initialized.")

        model_id = self.model_name or "deepseek-ai/DeepSeek-OCR"
        dtype = self._resolve_local_dtype()
        kwargs: dict[str, Any] = {
            "trust_remote_code": self.local_trust_remote_code,
            "use_safetensors": True,
            "_attn_implementation": self.local_attn_impl,
        }
        if dtype is not None:
            kwargs["torch_dtype"] = dtype

        tokenizer = self._auto_tokenizer_cls.from_pretrained(
            model_id,
            trust_remote_code=self.local_trust_remote_code,
        )
        model = self._auto_model_cls.from_pretrained(model_id, **kwargs).eval()
        if self._should_use_cuda():
            model = model.cuda()
        self._local_tokenizer = tokenizer
        self._local_model = model

    def _should_use_cuda(self) -> bool:
        if self._torch_module is None:
            return False
        device = (self.local_device or "cuda").strip().lower()
        if not device.startswith("cuda"):
            return False
        try:
            return bool(self._torch_module.cuda.is_available())
        except Exception:
            return False

    def _resolve_local_dtype(self) -> Any:
        if self._torch_module is None:
            return None
        key = (self.local_dtype or "bfloat16").strip().lower()
        mapping: dict[str, Any] = {
            "auto": None,
            "bfloat16": self._torch_module.bfloat16,
            "bf16": self._torch_module.bfloat16,
            "float16": self._torch_module.float16,
            "fp16": self._torch_module.float16,
            "float32": self._torch_module.float32,
            "fp32": self._torch_module.float32,
        }
        if key not in mapping:
            raise OCRAdapterError(
                f"unsupported deepseek local dtype: {self.local_dtype}. "
                "use auto/bfloat16/float16/float32."
            )
        return mapping[key]

    @staticmethod
    def _default_local_prompt(mode: str) -> str:
        normalized = (mode or "").strip().lower()
        if normalized in {"markdown", "document_markdown", "convert_markdown"}:
            return "<image>\n<|grounding|>Convert the document to markdown."
        return "<image>\nFree OCR."

    def healthcheck(self) -> bool:
        if self.backend == "api":
            return bool(self._client_cls is not None and self.api_key)
        return bool(self._auto_model_cls is not None and self._auto_tokenizer_cls is not None)

    def run(self, image_path: str) -> OCRRawResult:
        if self.backend == "local":
            markdown_text = self._run_local(image_path)
            lines = self._markdown_to_lines(markdown_text)
            metadata = {
                "backend": self.backend,
                "model_name": self.model_name or "deepseek-ai/DeepSeek-OCR",
                "prompt": self.local_prompt,
                "base_size": self.local_base_size,
                "image_size": self.local_image_size,
                "crop_mode": self.local_crop_mode,
                "device": self.local_device,
                "dtype": self.local_dtype,
                "attn_impl": self.local_attn_impl,
            }
            if self.local_output_dir:
                metadata["local_output_dir"] = self.local_output_dir
            return OCRRawResult(
                engine=self.name,
                engine_version=self.version,
                payload=lines,
                metadata=metadata,
            )

        self._ensure_client()
        try:
            markdown_text = self._client.parse(file_path=image_path, mode=self.mode, dpi=self.dpi)
        except Exception as exc:
            raise OCRAdapterError(f"deepseek OCR failed: {exc}") from exc
        lines = self._markdown_to_lines(markdown_text)
        return OCRRawResult(
            engine=self.name,
            engine_version=self.version,
            payload=lines,
            metadata={
                "backend": self.backend,
                "mode": self.mode,
                "dpi": self.dpi,
                "api_key_env": self.api_key_env,
            },
        )

    def _run_local(self, image_path: str) -> str:
        self._ensure_local_model()
        output_root, temp_dir = self._prepare_output_dir()
        try:
            result_file = output_root / "result.mmd"
            if result_file.exists():
                result_file.unlink()

            captured_stdout = io.StringIO()
            captured_stderr = io.StringIO()
            try:
                with contextlib.redirect_stdout(captured_stdout), contextlib.redirect_stderr(captured_stderr):
                    infer_result = self._local_model.infer(
                        self._local_tokenizer,
                        prompt=self.local_prompt,
                        image_file=image_path,
                        output_path=str(output_root),
                        base_size=self.local_base_size,
                        image_size=self.local_image_size,
                        crop_mode=self.local_crop_mode,
                        save_results=True,
                        test_compress=False,
                    )
            except Exception as exc:
                raise OCRAdapterError(f"deepseek local OCR failed: {exc}") from exc

            if result_file.exists():
                markdown_text = result_file.read_text(encoding="utf-8", errors="ignore")
            elif isinstance(infer_result, str):
                markdown_text = infer_result
            else:
                markdown_text = captured_stdout.getvalue()
            return str(markdown_text)
        finally:
            if temp_dir is not None:
                temp_dir.cleanup()

    def _prepare_output_dir(self) -> tuple[Path, tempfile.TemporaryDirectory[str] | None]:
        if self.local_output_dir:
            path = Path(self.local_output_dir)
            path.mkdir(parents=True, exist_ok=True)
            return path, None
        temp_dir = tempfile.TemporaryDirectory(prefix="deepseek_ocr_")
        return Path(temp_dir.name), temp_dir

    @staticmethod
    def _markdown_to_lines(text: str) -> list[dict[str, Any]]:
        rows = [line.strip() for line in str(text).splitlines() if line.strip()]
        if not rows:
            return []

        line_count = len(rows)
        height = 1.0 / max(1, line_count)
        lines: list[dict[str, Any]] = []

        for idx, content in enumerate(rows):
            y1 = min(0.99, idx * height)
            y2 = min(1.0, y1 + height * 0.95)
            lines.append(
                {
                    "text": content,
                    "bbox": [0.02, y1, 0.98, y2],
                    "polygon": [[0.02, y1], [0.98, y1], [0.98, y2], [0.02, y2]],
                    "confidence": 0.5,
                    "line_index": idx,
                    "page": 1,
                }
            )
        return lines
