"""Release retries must preserve an existing draft and confirmed uploaded bytes."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zipfile

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from release import find_release, prepare_ffmpeg, sha256, source_hashes, upload_asset


def response(payload):
    return MagicMock(ok=True, json=lambda: payload)


class ReleaseTests(unittest.TestCase):
    def test_source_manifest_excludes_runtime_logs_and_includes_build_workflow(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("requirements.txt", "config.example.yaml", "README.md", "CHANGELOG.md", "LICENSE",
                         "tests/test_sample.py", "tests/generated.jsonl",
                         "tools/ffmpeg-build/downloads/source.tar.xz",
                         ".github/workflows/ffmpeg-audio.yml"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture", encoding="utf-8")
            with patch("release.ROOT", root):
                sources = source_hashes()
            self.assertIn("tests/test_sample.py", sources)
            self.assertIn(".github/workflows/ffmpeg-audio.yml", sources)
            self.assertNotIn("tests/generated.jsonl", sources)
            self.assertNotIn("tools/ffmpeg-build/downloads/source.tar.xz", sources)

    def test_audio_bundle_requires_the_matching_source_before_running_any_binary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "audio.zip"
            source = root / "ffmpeg-source.tar.xz"
            source.write_bytes(b"original corresponding source")
            metadata = {"component": "FFmpeg", "license": "LGPL-2.1-or-later",
                        "tls_backend": "schannel", "corresponding_source_included": True,
                        "source_archive": {"name": source.name, "sha256": sha256(source)}}
            with zipfile.ZipFile(bundle, "w") as package:
                package.writestr("ffmpeg/SOURCE.json", json.dumps(metadata))
            source.write_bytes(b"different source")
            with patch("release.subprocess.run") as run:
                with self.assertRaisesRegex(RuntimeError, "source archive is missing or changed"):
                    prepare_ffmpeg(bundle)
                run.assert_not_called()
            source.unlink()
            with patch("release.subprocess.run") as run:
                with self.assertRaisesRegex(RuntimeError, "source archive is missing or changed"):
                    prepare_ffmpeg(bundle)
                run.assert_not_called()

    def test_retry_finds_draft_without_a_published_tag_lookup(self):
        draft = {"id": 1, "tag_name": "v1.4", "draft": True}
        client = MagicMock()
        client.get.return_value = response([draft])
        self.assertEqual(find_release(client, "https://api.example/repo", "v1.4"), draft)
        self.assertEqual(client.get.call_args.args[0], "https://api.example/repo/releases")
        client.post.assert_not_called()

    def test_release_search_includes_later_pages(self):
        draft = {"id": 1, "tag_name": "v1.4", "draft": True}
        client = MagicMock()
        client.get.side_effect = [response([{"tag_name": f"v{i + 2}"} for i in range(100)]),
                                  response([draft])]
        self.assertEqual(find_release(client, "https://api.example/repo", "v1.4"), draft)

    def test_duplicate_drafts_are_not_modified(self):
        client = MagicMock()
        client.get.return_value = response([{"tag_name": "v1.4"}, {"tag_name": "v1.4"}])
        with self.assertRaisesRegex(RuntimeError, "Multiple releases"):
            find_release(client, "https://api.example/repo", "v1.4")
        client.post.assert_not_called()

    def test_lost_upload_response_recovers_only_matching_remote_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "release.zip"
            path.write_bytes(b"release fixture")
            asset = {"name": path.name, "state": "uploaded", "size": path.stat().st_size,
                     "digest": "sha256:" + sha256(path)}
            client = MagicMock()
            client.post.side_effect = requests.ConnectionError("Response lost")
            client.get.return_value = response({"assets": [asset]})
            release = {"id": 1, "upload_url": "https://upload.example/assets{?name,label}"}
            self.assertEqual(upload_asset(client, "https://api.example/repo", release, path), asset)
            self.assertEqual(client.post.call_count, 1)
            client.delete.assert_not_called()
            asset["digest"] = "sha256:wrong"
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                upload_asset(client, "https://api.example/repo", release, path)

    def test_unconfirmed_upload_leaves_draft_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "release.zip"
            path.write_bytes(b"release fixture")
            client = MagicMock()
            client.post.side_effect = requests.ConnectionError("Upload interrupted")
            client.get.return_value = response({"assets": []})
            with patch("release.time.sleep"), self.assertRaisesRegex(RuntimeError, "remains a draft"):
                upload_asset(client, "https://api.example/repo", {"id": 1, "upload_url": "https://upload.example"}, path)
            self.assertEqual(client.post.call_count, 1)
            client.patch.assert_not_called()
            client.delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
