from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from core.models import OCRRawResult
from ocr.base import OCRAdapterError
from ocr.ndl_common import command_exists, find_output_candidates, parse_ocr_payload, parse_text_payload, split_command


class NdlOcrLiteAdapter:
    name = "ndlocr-lite"
    version = "command"

    def __init__(
        self,
        command: str | list[str] = "python src/ocr.py",
        working_dir: str | None = None,
        device: str = "cpu",
        viz: bool = False,
        timeout_sec: int = 600,
        extra_args: list[str] | None = None,
    ) -> None:
        self.command = command
        self.working_dir = working_dir
        self.device = device
        self.viz = viz
        self.timeout_sec = timeout_sec
        self.extra_args = [str(arg) for arg in (extra_args or [])]
        self._command_parts = split_command(command)
        if not self._command_parts:
            raise OCRAdapterError("ndlocr-lite command is empty.")

        self.version = f"cmd-{Path(self._command_parts[0]).name}"

    def healthcheck(self) -> bool:
        return command_exists(self._command_parts, working_dir=self.working_dir)

    def run(self, image_path: str) -> OCRRawResult:
        image = Path(image_path)
        if not image.exists():
            raise OCRAdapterError(f"input image not found: {image_path}")
        if not self.healthcheck():
            raise OCRAdapterError(
                "ndlocr-lite command is not available. "
                "Set `ocr.engines.ndlocr_lite.command` and `working_dir`."
            )

        with tempfile.TemporaryDirectory(prefix="ndlocr_lite_") as temp_output:
            output_dir = Path(temp_output)
            cmd = list(self._command_parts)
            cmd.extend(["--sourceimg", str(image.resolve())])
            cmd.extend(["--output", str(output_dir.resolve())])
            cmd.extend(["--device", str(self.device)])
            if self.viz:
                cmd.extend(["--viz", "True"])
            cmd.extend(self.extra_args)

            completed = subprocess.run(
                cmd,
                cwd=self.working_dir,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout_sec,
            )
            if completed.returncode != 0:
                raise OCRAdapterError(
                    "ndlocr-lite failed: "
                    f"returncode={completed.returncode} stderr={_tail(completed.stderr)}"
                )

            lines, source = self._load_lines(output_dir=output_dir, image_stem=image.stem)
            return OCRRawResult(
                engine=self.name,
                engine_version=self.version,
                payload=lines,
                metadata={
                    "device": self.device,
                    "command": " ".join(cmd),
                    "working_dir": self.working_dir,
                    "output_source": source,
                    "stdout_tail": _tail(completed.stdout),
                },
            )

    def _load_lines(self, output_dir: Path, image_stem: str) -> tuple[list[dict[str, Any]], str | None]:
        json_candidates = find_output_candidates(output_dir, image_stem, suffixes=(".json",), recursive=True)
        fallback_lines: list[dict[str, Any]] = []
        fallback_path: str | None = None
        for path in json_candidates:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            lines = parse_ocr_payload(payload)
            if lines:
                return lines, str(path)
            if not fallback_lines:
                fallback_lines = lines
                fallback_path = str(path)
        if fallback_path is not None:
            return fallback_lines, fallback_path

        txt_candidates = find_output_candidates(output_dir, image_stem, suffixes=(".txt",), recursive=True)
        for path in txt_candidates:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            lines = parse_text_payload(text)
            if lines:
                return lines, str(path)
            if fallback_path is None:
                fallback_lines = lines
                fallback_path = str(path)
        return fallback_lines, fallback_path


def _tail(text: str | None, limit: int = 400) -> str:
    if not text:
        return ""
    return text[-limit:]
