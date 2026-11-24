"""Download and verify the pinned Zipformer ASR model files."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hdusign.runtime import ASR_FOLDER

BASE_URL = f"https://huggingface.co/csukuangfj/{ASR_FOLDER}/resolve/main"
MODEL_FILES = {
    "encoder.int8.onnx": "5ac51e27981bb4dab01bb9be4958453ba50c3b61c063ddda0eab23fd3671aa4f",
    "decoder.onnx": "06522ad63cec0fdf6809f4e1db9bb4f7d710c34582e3b35db62ac60eccafac7e",
    "joiner.int8.onnx": "b34584dc6f561089e1d747fedebb3765f2caa72c927ef54d7ca55e5ae40a814b",
    "tokens.txt": "6193c7ea1c96d0d9a1e9652789b40d13a8a913b434a5451e93158f5a09fd6652",
}


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download_file(url: str, destination: Path, expected_sha256: str) -> None:
    if destination.is_file() and sha256(destination) == expected_sha256:
        print(f"Verified existing {destination.name}", flush=True)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            print(f"Downloading {destination.name} (attempt {attempt}/3)", flush=True)
            req = urllib.request.Request(url, headers={"User-Agent": "HDUSign-ModelFetch/1.0"})
            with urllib.request.urlopen(req, timeout=120) as response, partial.open("wb") as stream:
                while chunk := response.read(1024 * 1024):
                    stream.write(chunk)
            actual = sha256(partial)
            if actual != expected_sha256:
                raise RuntimeError(
                    f"SHA256 mismatch for {destination.name}: expected {expected_sha256}, got {actual}"
                )
            partial.replace(destination)
            print(f"Verified SHA256: {destination.name}", flush=True)
            return
        except RuntimeError:
            partial.unlink(missing_ok=True)
            raise
        except Exception as exc:
            partial.unlink(missing_ok=True)
            last_error = exc
            if attempt < 3:
                time.sleep(2 * attempt)
    raise RuntimeError(f"Failed to download {destination.name}: {last_error}") from last_error


def main() -> None:
    target_dir = ROOT / "models" / ASR_FOLDER
    for filename, expected in MODEL_FILES.items():
        download_file(f"{BASE_URL}/{filename}", target_dir / filename, expected)


if __name__ == "__main__":
    main()
