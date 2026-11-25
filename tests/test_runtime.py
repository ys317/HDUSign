"""Portable dependencies must resolve without another installation or download."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from hdusign import runtime


class RuntimeTests(unittest.TestCase):
    def test_frozen_browser_ignores_external_cache(self):
        with tempfile.TemporaryDirectory(prefix="中文 路径 ") as folder:
            root = Path(folder)
            (root / "browsers").mkdir()
            with patch.object(sys, "frozen", True, create=True), \
                    patch.object(sys, "_MEIPASS", str(root), create=True), \
                    patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": "missing-external-cache"}):
                runtime.configure_browser_runtime()
                self.assertEqual(Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]), root / "browsers")

    def test_missing_browser_gives_extraction_guidance(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "_MEIPASS", folder, create=True):
            with self.assertRaisesRegex(RuntimeError, "完整解压"):
                runtime.configure_browser_runtime()

    def test_source_respects_developer_browser_cache(self):
        with patch.object(sys, "frozen", False, create=True), \
                patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": "custom-cache"}):
            runtime.configure_browser_runtime()
            self.assertEqual(os.environ["PLAYWRIGHT_BROWSERS_PATH"], "custom-cache")

    def test_bundled_semantic_model_never_downloads(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model = root / "models" / runtime.SEMANTIC_FOLDER
            model.mkdir(parents=True)
            for name in runtime.SEMANTIC_FILES:
                (model / name).write_bytes(b"fixture")
            with patch.object(runtime, "application_root", return_value=root):
                options = runtime.semantic_model_options(runtime.SEMANTIC_MODEL)
                self.assertTrue(options["local_files_only"])
                self.assertEqual(Path(options["specific_model_path"]), model)

    def test_incomplete_frozen_model_does_not_start_download(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(runtime, "application_root", return_value=Path(folder)), \
                patch.object(sys, "frozen", True, create=True):
            with self.assertRaisesRegex(RuntimeError, "完整解压"):
                runtime.semantic_model_options(runtime.SEMANTIC_MODEL)


class FFmpegRuntimeTests(unittest.TestCase):
    VERSION = b"ffmpeg version 8.1.2-full_build-www.gyan.dev Copyright FFmpeg developers\n"
    PROTOCOLS = b"Supported file protocols:\nInput:\nfile\npipe\nhttp\nhttps\ntcp\ntls\nOutput:\npipe\n"

    def results(self, decoded: bytes, protocols: bytes | None = None):
        return [subprocess.CompletedProcess([], 0, stdout=output, stderr=b"")
                for output in (self.VERSION, protocols or self.PROTOCOLS, b"encoded-aac", decoded)]

    def test_audio_check_exercises_conversion_without_network_or_console(self):
        import io
        import wave

        tone = (0.1 * np.sin(2 * np.pi * 440 * np.arange(4000) / 16000)).astype("<f4")
        with patch.object(runtime.subprocess, "run", side_effect=self.results(tone.tobytes())) as run:
            result = runtime.check_ffmpeg_runtime("ffmpeg.exe")
        self.assertEqual(result["sample_rate"], 16000)
        self.assertEqual(result["channels"], 1)
        self.assertEqual(result["decoded_samples"], 4000)
        self.assertEqual(result["version"], "8.1.2-full_build-www.gyan.dev")
        calls = run.call_args_list
        with wave.open(io.BytesIO(calls[2].kwargs["input"]), "rb") as original:
            self.assertEqual(original.getframerate(), 48000)
            self.assertEqual(original.getnchannels(), 2)
        for call in calls[2:]:
            arguments = call.args[0]
            self.assertEqual(arguments[arguments.index("-protocol_whitelist") + 1], "pipe")
            self.assertEqual(arguments[arguments.index("-i") + 1], "pipe:0")
        for call in calls:
            self.assertLessEqual(call.kwargs["timeout"], 20)
            if sys.platform == "win32":
                self.assertTrue(call.kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW)

    def test_output_https_cannot_substitute_for_missing_input_protocol(self):
        protocols = b"Input:\nfile\npipe\nhttp\ntcp\ntls\nOutput:\nhttps\npipe\n"
        with patch.object(runtime.subprocess, "run", side_effect=self.results(b"", protocols)) as run:
            with self.assertRaisesRegex(RuntimeError, "https"):
                runtime.check_ffmpeg_runtime("ffmpeg.exe")
        self.assertEqual(run.call_count, 2)

    def test_invalid_or_silent_audio_does_not_pass_runtime_check(self):
        cases = (b"", b"partial", np.zeros(4000, dtype="<f4").tobytes(),
                 np.full(4000, np.nan, dtype="<f4").tobytes(),
                 np.full(9000, 0.1, dtype="<f4").tobytes())
        for decoded in cases:
            with self.subTest(bytes=len(decoded)), \
                    patch.object(runtime.subprocess, "run", side_effect=self.results(decoded)):
                with self.assertRaisesRegex(RuntimeError, "FFmpeg"):
                    runtime.check_ffmpeg_runtime("ffmpeg.exe")

    def test_unlaunchable_binary_reports_runtime_failure(self):
        with patch.object(runtime.subprocess, "run", side_effect=FileNotFoundError("missing binary")):
            with self.assertRaisesRegex(RuntimeError, "FFmpeg 离线音频检查失败"):
                runtime.check_ffmpeg_runtime("missing-ffmpeg.exe")

    def test_build_requires_verified_ffmpeg_bundle_before_running_any_preparation_command(self):
        from tools import release

        with tempfile.TemporaryDirectory() as temporary, patch.object(release.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "FFmpeg bundle is missing"):
                release.prepare_ffmpeg(Path(temporary) / "missing.zip")
        run.assert_not_called()


class ExternalSubprocessEnvTests(unittest.TestCase):
    def test_frozen_linux_restores_original_ld_library_path(self):
        base = {
            "PATH": "/usr/bin",
            "LD_LIBRARY_PATH": "/tmp/app/_internal:/usr/lib",
            "LD_LIBRARY_PATH_ORIG": "/opt/hostedtoolcache/lib",
        }
        with patch.object(runtime.sys, "frozen", True, create=True), \
                patch.object(runtime.sys, "platform", "linux"):
            restored = runtime.external_subprocess_env(base)
            self.assertEqual(restored["LD_LIBRARY_PATH"], "/opt/hostedtoolcache/lib")
            self.assertNotIn("LD_LIBRARY_PATH_ORIG", restored)
            self.assertIn("env", runtime.external_subprocess_kwargs())

    def test_frozen_linux_drops_ld_library_path_when_orig_missing_or_empty(self):
        for orig_value in (None, ""):
            base = {"PATH": "/usr/bin", "LD_LIBRARY_PATH": "/tmp/app/_internal"}
            if orig_value is not None:
                base["LD_LIBRARY_PATH_ORIG"] = orig_value
            with patch.object(runtime.sys, "frozen", True, create=True), \
                    patch.object(runtime.sys, "platform", "linux"):
                restored = runtime.external_subprocess_env(base)
                self.assertNotIn("LD_LIBRARY_PATH", restored)
                self.assertNotIn("LD_LIBRARY_PATH_ORIG", restored)

    def test_unfrozen_or_windows_leaves_environment_untouched(self):
        base = {
            "PATH": "C:\\Windows\\System32",
            "LD_LIBRARY_PATH": "/custom/lib",
            "LD_LIBRARY_PATH_ORIG": "/orig/lib",
        }
        with patch.object(runtime.sys, "frozen", False, create=True), \
                patch.object(runtime.sys, "platform", "linux"):
            self.assertEqual(runtime.external_subprocess_kwargs(), {})
            self.assertEqual(runtime.external_subprocess_env(base), base)
        with patch.object(runtime.sys, "frozen", True, create=True), \
                patch.object(runtime.sys, "platform", "win32"):
            self.assertEqual(runtime.external_subprocess_kwargs(), {})
            self.assertEqual(runtime.external_subprocess_env(base), base)


if __name__ == "__main__":
    unittest.main()
