"""Release retries must preserve an existing draft and confirmed uploaded bytes."""
from pathlib import Path
import hashlib
import io
import json
import sys
import tarfile
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
            with patch("release.sys.platform", "win32"), patch("release.subprocess.run") as run:
                with self.assertRaisesRegex(RuntimeError, "source archive is missing or changed"):
                    prepare_ffmpeg(bundle)
                run.assert_not_called()
            source.unlink()
            with patch("release.sys.platform", "win32"), patch("release.subprocess.run") as run:
                with self.assertRaisesRegex(RuntimeError, "source archive is missing or changed"):
                    prepare_ffmpeg(bundle)
                run.assert_not_called()

    def test_linux_tar_ffmpeg_bundle_validates_source_and_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "ffmpeg-linux64.tar.gz"
            source = root / "ffmpeg-8.1.2-hdusign-audio-linux64-source.tar.xz"
            source.write_bytes(b"linux corresponding source")
            fake_bin = b"fake-linux-ffmpeg-binary"
            bin_hash = hashlib.sha256(fake_bin).hexdigest()
            metadata = {
                "component": "FFmpeg", "version": "8.1.2-hdusign-audio",
                "license": "LGPL-2.1-or-later", "tls_backend": "gnutls",
                "corresponding_source_included": True, "binary_sha256": bin_hash,
                "source_archive": {"name": source.name, "sha256": sha256(source)},
            }
            files = {
                "ffmpeg/SOURCE.json": json.dumps(metadata).encode("utf-8"),
                "ffmpeg/ffmpeg": fake_bin,
                "ffmpeg/LICENSE": b"LGPL",
                "ffmpeg/README.txt": b"readme",
                "ffmpeg/version.txt": b"ffmpeg version 8.1.2-hdusign-audio Copyright",
                "ffmpeg/buildconf.txt": b"--enable-gnutls --disable-autodetect",
                "ffmpeg/license-notice.txt": b"GNU Lesser General Public License",
                "ffmpeg/licenses/toolchain/libc6/copyright": b"glibc copyright",
            }
            with tarfile.open(bundle, "w:gz") as package:
                for name, data in files.items():
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    package.addfile(info, io.BytesIO(data))
            responses = [
                MagicMock(stdout=files["ffmpeg/version.txt"].decode(), stderr=""),
                MagicMock(stdout=files["ffmpeg/buildconf.txt"].decode(), stderr=""),
                MagicMock(stdout=files["ffmpeg/license-notice.txt"].decode(), stderr=""),
            ]
            with patch("release.sys.platform", "linux"), \
                    patch("release.FFMPEG_BINARY", "ffmpeg"), \
                    patch("release.FFMPEG_FILES", ("ffmpeg", "LICENSE", "README.txt", "SOURCE.json",
                                                   "version.txt", "buildconf.txt", "license-notice.txt")), \
                    patch("release.RUNTIME", root / "runtime"), \
                    patch("release.BUILD", root), \
                    patch("release.OUTPUT", root / "output"), \
                    patch("release.subprocess.run", side_effect=responses), \
                    patch("release.check_ffmpeg_runtime", return_value={"version": "8.1.2-hdusign-audio"}):
                result = prepare_ffmpeg(bundle)
            self.assertEqual(result["tls_backend"], "gnutls")
            self.assertTrue((root / "output" / source.name).is_file())

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
