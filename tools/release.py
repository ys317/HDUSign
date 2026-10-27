"""Build and publish a verified, self-contained Windows release.

Usage: python tools/release.py build --ffmpeg-bundle <verified-audio-build.zip>
       python tools/release.py publish --notes <release-notes.md>
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zipfile

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hdusign import __version__
from hdusign.processes import hidden_subprocess_options
from hdusign.runtime import (ASR_FILES, ASR_FOLDER, FFMPEG_FILES, SEMANTIC_FILES,
                              SEMANTIC_FOLDER, SEMANTIC_MODEL, check_ffmpeg_runtime)

BUILD = ROOT / "build"
RUNTIME = BUILD / "portable-runtime"
VERSION = f"v{__version__}"
DIST = BUILD / "releases" / VERSION
OUTPUT = ROOT / "dist" / "releases" / VERSION


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def clear_generated(directory: Path) -> None:
    """Only remove tool-owned output below this checkout's build directory."""
    target = directory.resolve()
    boundary = BUILD.resolve()
    if target == boundary or not target.is_relative_to(boundary):
        raise ValueError(f"Refusing to clear a directory outside build/: {target}")
    if directory.exists():
        shutil.rmtree(directory)


def require_files(directory: Path, names) -> None:
    missing = [name for name in names if not (directory / name).is_file()]
    if missing:
        raise RuntimeError(f"Missing files in {directory}: {', '.join(missing)}")


def copy_files(source: Path, destination: Path, names) -> None:
    require_files(source, names)
    destination.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copy2(source / name, destination / name)


def prepare_ffmpeg(bundle: Path) -> dict:
    """Bundle our minimal decoder only with its matching complete source archive."""
    bundle = bundle.resolve()
    if not bundle.is_file():
        raise RuntimeError("The verified FFmpeg bundle is missing; build the audio component first.")
    with zipfile.ZipFile(bundle) as package:
        metadata = json.loads(package.read("ffmpeg/SOURCE.json"))
        if (metadata.get("component") != "FFmpeg"
                or metadata.get("license") != "LGPL-2.1-or-later"
                or metadata.get("tls_backend") != "schannel"
                or metadata.get("corresponding_source_included") is not True):
            raise RuntimeError("Use the HDUSign FFmpeg audio build with its complete matching source.")
        source = metadata.get("source_archive") or {}
        name = source.get("name", "")
        if not name or Path(name).name != name or not name.endswith("-source.tar.xz"):
            raise RuntimeError("The FFmpeg source archive name is invalid.")
        source_archive = bundle.parent / name
        if not source_archive.is_file() or sha256(source_archive) != source.get("sha256"):
            raise RuntimeError("The complete matching FFmpeg source archive is missing or changed.")
        destination = RUNTIME / "ffmpeg"
        clear_generated(destination)
        destination.mkdir(parents=True)
        for filename in FFMPEG_FILES:
            (destination / filename).write_bytes(package.read("ffmpeg/" + filename))
        # Keep the MinGW/GCC runtime notices shipped by the verified build.
        notices = [entry for entry in package.infolist()
                   if entry.filename.startswith("ffmpeg/licenses/") and not entry.is_dir()]
        if not notices:
            raise RuntimeError("The audio build's toolchain license notices are missing.")
        for entry in notices:
            target = (destination / entry.filename.removeprefix("ffmpeg/")).resolve()
            if not target.is_relative_to((destination / "licenses").resolve()):
                raise RuntimeError("The audio build contains an invalid license path.")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(package.read(entry))
    executable = destination / "ffmpeg.exe"
    if sha256(executable) != metadata.get("binary_sha256"):
        raise RuntimeError("The FFmpeg binary does not match its build provenance.")

    def inspect(option: str) -> str:
        result = subprocess.run(
            [str(executable), "-hide_banner", option], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=True, timeout=20, **hidden_subprocess_options())
        return result.stdout + result.stderr

    version_text = inspect("-version")
    buildconf = inspect("-buildconf")
    license_notice = inspect("-L")
    if (not version_text.startswith(f"ffmpeg version {metadata['version']} ")
            or "--enable-schannel" not in buildconf or "--disable-autodetect" not in buildconf
            or "--enable-gpl" in buildconf or "--enable-nonfree" in buildconf
            or "--enable-version3" in buildconf
            or "GNU Lesser General Public" not in license_notice):
        raise RuntimeError("The FFmpeg executable differs from the documented minimal LGPL build.")
    for filename, actual in (("version.txt", version_text), ("buildconf.txt", buildconf),
                              ("license-notice.txt", license_notice)):
        if (destination / filename).read_text(encoding="utf-8").strip() != actual.strip():
            raise RuntimeError(f"FFmpeg provenance differs from the executable: {filename}")
    metadata["audio_check"] = check_ffmpeg_runtime(executable)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_archive, OUTPUT / source_archive.name)
    print(f"Bundled FFmpeg {metadata['version']}; matching sources and audio decoding verified", flush=True)
    return metadata


def prepare_browsers() -> None:
    import playwright
    from playwright.sync_api import sync_playwright

    package = Path(playwright.__file__).resolve().parent / "driver" / "package"
    registry = json.loads((package / "browsers.json").read_text(encoding="utf-8"))
    components = [entry for entry in registry["browsers"]
                  if entry["name"] in {"chromium", "ffmpeg", "winldd"}]
    browsers = RUNTIME / "browsers"
    clear_generated(browsers)
    browsers.mkdir(parents=True)
    with sync_playwright() as driver:
        # Resolve the cache with this exact Playwright version, not another install.
        cache = Path(driver.chromium.executable_path).parents[2]
    for component in components:
        name = f"{component['name']}-{component['revision']}"
        if (cache / name).is_dir():
            shutil.copytree(cache / name, browsers / name)
    if any(not (browsers / f"{c['name']}-{c['revision']}").is_dir() for c in components):
        env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(browsers))
        subprocess.run([sys.executable, "-m", "playwright", "install", "--no-shell", "chromium"],
                       cwd=ROOT, env=env, stdin=subprocess.DEVNULL, check=True,
                       **hidden_subprocess_options())
    # Playwright's cache bookkeeping is unrelated to the portable application.
    for extra in browsers.iterdir():
        if extra.name.startswith("."):
            if extra.is_dir():
                clear_generated(extra)
            else:
                extra.unlink()
    chromium = next(c for c in components if c["name"] == "chromium")
    require_files(browsers / f"chromium-{chromium['revision']}" / "chrome-win64", ["chrome.exe"])
    print(f"Bundled Chromium {chromium['browserVersion']}", flush=True)


def prepare_semantic_model() -> None:
    from fastembed import TextEmbedding

    source = ROOT / "models" / SEMANTIC_FOLDER
    if not all((source / name).is_file() for name in SEMANTIC_FILES):
        try:
            model = TextEmbedding(model_name=SEMANTIC_MODEL, local_files_only=True, lazy_load=True)
        except (ValueError, OSError):
            model = TextEmbedding(model_name=SEMANTIC_MODEL,
                                  cache_dir=str(RUNTIME / "model-cache"), lazy_load=True)
        source = Path(model.model._model_dir)
    copy_files(source, RUNTIME / "models" / SEMANTIC_FOLDER, SEMANTIC_FILES)
    print("Prepared local semantic model", flush=True)


def source_hashes() -> dict[str, str]:
    paths = [ROOT / name for name in ("requirements.txt", "config.example.yaml", "README.md", "CHANGELOG.md", "LICENSE")]
    generated = tuple(ROOT / "tools" / "ffmpeg-build" / name for name in ("downloads", "work", "dist"))
    for folder in ("hdusign", "assets", "docs", "tools", "tests", ".github"):
        paths.extend(p for p in (ROOT / folder).rglob("*")
                     if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".jsonl"
                     and not any(p.is_relative_to(directory) for directory in generated))
    return {p.relative_to(ROOT).as_posix(): sha256(p) for p in sorted(paths)}


def copy_dependency_licenses(destination: Path) -> None:
    from packaging.requirements import Requirement

    destination.mkdir(parents=True, exist_ok=True)

    pending = [line.strip() for line in (ROOT / "requirements.txt").read_text().splitlines()
               if line.strip() and not line.startswith("#")] + ["pyinstaller"]
    visited = set()
    while pending:
        requirement = Requirement(pending.pop())
        if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
            continue
        distribution = importlib.metadata.distribution(requirement.name)
        name = distribution.metadata["Name"]
        if name in visited:
            continue
        visited.add(name)
        pending.extend(distribution.requires or [])
        for path in distribution.files or []:
            if ".." in path.parts or not path.name.upper().startswith(("LICENSE", "COPYING", "NOTICE")):
                continue
            source = Path(distribution.locate_file(path))
            if source.is_file():
                target = destination / name / path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
    python_root = Path(sys._base_executable).resolve().parent
    for name in ("LICENSE.txt", "LICENSE"):
        if (python_root / name).is_file():
            shutil.copy2(python_root / name, destination / "Python-LICENSE.txt")
            break


def verify_archive(archive: Path) -> dict:
    """Exercise the extracted exe with empty caches, a minimal PATH and no downloads."""
    with tempfile.TemporaryDirectory(prefix="解压 验证 ", dir=BUILD) as temporary:
        workspace = Path(temporary)
        with zipfile.ZipFile(archive) as package:
            if bad := package.testzip():
                raise RuntimeError(f"ZIP checksum failed: {bad}")
            package.extractall(workspace)
        profile = workspace / "empty-user"
        local = profile / "AppData" / "Local"
        roaming = profile / "AppData" / "Roaming"
        temp = local / "Temp"
        temp.mkdir(parents=True)
        roaming.mkdir(parents=True)
        env = dict(os.environ)
        for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
            env.pop(name, None)
        system_root = Path(os.environ["SystemRoot"])
        env.update(USERPROFILE=str(profile), LOCALAPPDATA=str(local), APPDATA=str(roaming),
                   TEMP=str(temp), TMP=str(temp), HF_HOME=str(profile / "huggingface"),
                   FASTEMBED_CACHE_PATH=str(profile / "fastembed"), HF_HUB_OFFLINE="1",
                   PLAYWRIGHT_BROWSERS_PATH=str(profile / "absent-browser-cache"),
                   PATH=os.pathsep.join([str(system_root / "System32"), str(system_root)]))
        report = workspace / "runtime.json"
        result = subprocess.run([str(workspace / "HDUSign" / "HDUSign.exe"),
                                 "--check-runtime", str(report)],
                                cwd=workspace, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=240, **hidden_subprocess_options())
        if not report.is_file():
            raise RuntimeError(f"Runtime check exited with {result.returncode} without a report")
        content = json.loads(report.read_text(encoding="utf-8"))
        (BUILD / f"runtime-{VERSION}.json").write_text(
            json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
        if result.returncode or not content["ok"]:
            raise RuntimeError(content.get("error", "Runtime check failed"))
        if content["version"] != __version__:
            raise RuntimeError("Packaged version does not match the source")
        if not content.get("ffmpeg"):
            raise RuntimeError("Packaged FFmpeg did not pass the offline audio check")
        if not content.get("desktop", {}).get("qml"):
            raise RuntimeError("Packaged Qt Quick desktop did not pass the rendering check")
        return content


def write_version_resource() -> None:
    """Derive Windows file metadata from the same version shown by the app."""
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable,
        VarFileInfo, VarStruct, VSVersionInfo,
    )

    parts = tuple(int(part) for part in __version__.split("."))
    if not 1 <= len(parts) <= 4:
        raise ValueError("Windows versions must have one to four numeric components")
    version = parts + (0,) * (4 - len(parts))
    strings = {
        "FileDescription": "HDUSign 课堂辅助工具",
        "FileVersion": ".".join(map(str, version)),
        "InternalName": "HDUSign",
        "LegalCopyright": "Copyright (c) 2026 ys317",
        "OriginalFilename": "HDUSign.exe",
        "ProductName": "HDUSign",
        "ProductVersion": __version__,
    }
    resource = VSVersionInfo(
        ffi=FixedFileInfo(filevers=version, prodvers=version, mask=0x3f, flags=0,
                          OS=0x40004, fileType=1, subtype=0, date=(0, 0)),
        kids=[StringFileInfo([StringTable("040904B0", [
            StringStruct(key, value) for key, value in strings.items()])]),
              VarFileInfo([VarStruct("Translation", [1033, 1200])])],
    )
    (BUILD / "windows-version.txt").write_text(str(resource), encoding="utf-8")


def build_release(ffmpeg_bundle: Path) -> None:
    if sys.platform != "win32" or struct.calcsize("P") != 8:
        raise SystemExit("Build with 64-bit Python on Windows.")
    BUILD.mkdir(exist_ok=True)
    require_files(ROOT / "models" / ASR_FOLDER, ASR_FILES)
    sources = source_hashes()
    write_version_resource()
    ffmpeg = prepare_ffmpeg(ffmpeg_bundle)
    prepare_browsers()
    prepare_semantic_model()
    clear_generated(DIST)
    log = BUILD / f"build-{VERSION}.log"
    print(f"Building {VERSION}; PyInstaller output: {log}", flush=True)
    with log.open("w", encoding="utf-8") as stream:
        subprocess.run([sys.executable, "-X", "utf8", "-m", "PyInstaller", "--noconfirm", "--clean",
                        "--distpath", str(DIST), "--workpath", str(BUILD / "pyinstaller" / VERSION),
                        str(ROOT / "tools" / "HDUSign.spec")],
                       cwd=ROOT, stdin=subprocess.DEVNULL, stdout=stream,
                       stderr=subprocess.STDOUT, check=True, **hidden_subprocess_options())
    app = DIST / "HDUSign"
    packaged_ffmpeg = app / "_internal" / "ffmpeg"
    require_files(packaged_ffmpeg, FFMPEG_FILES)
    if sha256(packaged_ffmpeg / "ffmpeg.exe") != ffmpeg["binary_sha256"]:
        raise RuntimeError("Packaging changed the FFmpeg executable; keep the original static binary.")
    copy_files(ROOT, app, ["README.md", "CHANGELOG.md", "LICENSE", "config.example.yaml"])
    shutil.copytree(ROOT / "docs", app / "docs")
    shutil.copytree(ROOT / "assets" / "screenshots", app / "assets" / "screenshots")
    shutil.copytree(ROOT / "assets" / "licenses", app / "assets" / "licenses")
    copy_dependency_licenses(app / "licenses")
    copy_files(ROOT / "models" / ASR_FOLDER, app / "models" / ASR_FOLDER, ASR_FILES)
    copy_files(RUNTIME / "models" / SEMANTIC_FOLDER, app / "models" / SEMANTIC_FOLDER, SEMANTIC_FILES)
    if source_hashes() != sources:
        raise RuntimeError("Source files changed during the build; rebuild before publishing")

    OUTPUT.mkdir(parents=True, exist_ok=True)
    archive = OUTPUT / f"HDUSign-{VERSION}-win64.zip"
    print(f"Creating {archive.name}", flush=True)
    forbidden = {"config.yaml", "secrets_local.json", "session_local.json", "browser_profile",
                 "live_session.json", "live_profile", "accounts.json", "webhooks.json",
                 "account_data", "notification_routes", "trajectory_library.json"}
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as package:
        for path in sorted(app.rglob("*")):
            relative = path.relative_to(DIST)
            if any(part in forbidden for part in relative.parts) or path.suffix == ".jsonl":
                raise RuntimeError(f"Private runtime data must not be packaged: {relative}")
            if path.is_file():
                package.write(path, relative.as_posix())
    print("Checking the extracted application with empty user caches", flush=True)
    runtime = verify_archive(archive)
    checksum = sha256(archive)
    source = ffmpeg["source_archive"]
    (OUTPUT / "SHA256SUMS.txt").write_text(
        f"{checksum}  {archive.name}\n{source['sha256']}  {source['name']}\n", encoding="ascii")
    manifest = {"version": VERSION, "archive": str(archive.relative_to(ROOT)), "sha256": checksum,
                "bytes": archive.stat().st_size, "source_hashes": sources, "runtime": runtime,
                "ffmpeg": ffmpeg,
                "dependencies": {name: importlib.metadata.version(name)
                                 for name in ("pyinstaller", "playwright", "fastembed", "sherpa-onnx")}}
    (BUILD / f"release-{VERSION}-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: manifest[key] for key in ("version", "archive", "bytes", "sha256")},
                     ensure_ascii=False, indent=2), flush=True)


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, stdin=subprocess.DEVNULL,
                                   text=True, encoding="utf-8", **hidden_subprocess_options()).strip()


def github_session() -> requests.Session:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
        result = subprocess.run(["git", "credential", "fill"],
                                input="protocol=https\nhost=github.com\n\n",
                                capture_output=True, text=True, env=env, timeout=30,
                                **hidden_subprocess_options())
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        token = fields.get("password")
    if not token:
        raise RuntimeError("No GitHub credential is available")
    client = requests.Session()
    # Only repeat idempotent reads. Uploads are reconciled by asset digest after
    # a lost response; blindly replaying a POST could create duplicate assets.
    client.mount("https://", HTTPAdapter(max_retries=Retry(
        total=3, connect=0, read=3, status=3, other=0, backoff_factor=1,
        allowed_methods={"GET", "HEAD"}, status_forcelist=(429, 500, 502, 503, 504))))
    client.headers.update({"Authorization": "Bearer " + token,
                           "Accept": "application/vnd.github+json",
                           "X-GitHub-Api-Version": "2022-11-28"})
    return client


def checked(response):
    if not response.ok:
        try:
            message = response.json().get("message", "GitHub API request failed")
        except ValueError:
            message = "GitHub API request failed"
        raise RuntimeError(f"HTTP {response.status_code}: {message}")
    return response.json()


def verify_asset(asset: dict, path: Path) -> None:
    if (asset["state"] != "uploaded" or asset["size"] != path.stat().st_size
            or asset.get("digest") != "sha256:" + sha256(path)):
        raise RuntimeError(f"Uploaded asset does not match the local file: {path.name}")


def find_release(client, api: str, tag: str):
    # Looking up a tag can return 404 for an existing draft. The authenticated
    # release list includes drafts, so a retry must search it before creating one.
    matches = []
    page = 1
    while True:
        releases = checked(client.get(api + "/releases",
                                      params={"per_page": 100, "page": page}, timeout=30))
        matches.extend(release for release in releases if release["tag_name"] == tag)
        if len(releases) < 100:
            break
        page += 1
    if len(matches) > 1:
        raise RuntimeError(f"Multiple releases exist for {tag}; inspect the drafts before continuing")
    return matches[0] if matches else None


def upload_asset(client, api: str, release: dict, path: Path) -> dict:
    try:
        with ProgressFile(path) as stream:
            response = client.post(release["upload_url"].split("{", 1)[0],
                params={"name": path.name}, data=stream, timeout=(30, 600),
                headers={"Content-Type": "application/zip" if path.suffix == ".zip" else
                         "application/x-xz" if path.suffix == ".xz" else "text/plain"})
    except requests.RequestException as exc:
        # The server may have accepted every byte even if its response was lost.
        # Confirm the existing asset before doing any further write operation.
        print("Upload connection interrupted; checking the remote asset", flush=True)
        for attempt in range(3):
            current = checked(client.get(api + f"/releases/{release['id']}", timeout=30))
            uploaded = next((asset for asset in current["assets"]
                             if asset["name"] == path.name and asset["state"] == "uploaded"), None)
            if uploaded:
                verify_asset(uploaded, path)
                return uploaded
            if attempt < 2:
                time.sleep(2)
        raise RuntimeError("Upload could not be confirmed; the release remains a draft") from exc
    uploaded = checked(response)
    verify_asset(uploaded, path)
    return uploaded


class ProgressFile(io.BufferedReader):
    def __init__(self, path: Path):
        super().__init__(io.FileIO(path, "rb"))
        self.label = path.name
        self.total = path.stat().st_size
        self.last_report = time.monotonic()

    def read(self, size=-1):
        data = super().read(size)
        now = time.monotonic()
        if now - self.last_report >= 20:
            print(f"Uploading {self.label}: {self.tell() / self.total:.0%}", flush=True)
            self.last_report = now
        return data


def publish_release(notes_path: Path) -> None:
    notes = notes_path.read_text(encoding="utf-8").strip()
    if not notes:
        raise RuntimeError("Release notes must not be empty")
    if git("status", "--porcelain"):
        raise RuntimeError("Commit all release source changes before publishing")
    commit = git("rev-parse", "HEAD")
    if git("rev-parse", f"{VERSION}^{{commit}}") != commit:
        raise RuntimeError("The version tag must point to the release commit")
    origin = git("remote", "get-url", "origin")
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([^/]+/[^/]+?)(?:\.git)?", origin)
    if not match:
        raise RuntimeError("The origin remote must be a GitHub repository")
    api = "https://api.github.com/repos/" + match[1]

    manifest = json.loads((BUILD / f"release-{VERSION}-manifest.json").read_text(encoding="utf-8"))
    archive = OUTPUT / f"HDUSign-{VERSION}-win64.zip"
    source = manifest["ffmpeg"]["source_archive"]
    source_archive = OUTPUT / source["name"]
    if not source_archive.is_file() or sha256(source_archive) != source["sha256"]:
        raise RuntimeError("The matching FFmpeg corresponding source must accompany the release")
    assets = (archive, source_archive, OUTPUT / "SHA256SUMS.txt")
    if (manifest["version"] != VERSION or manifest["source_hashes"] != source_hashes()
            or manifest["sha256"] != sha256(archive) or not manifest["runtime"]["ok"]):
        raise RuntimeError("Rebuild the release: source or archive differs from the verified build")
    if assets[2].read_text(encoding="ascii") != (
            f"{manifest['sha256']}  {archive.name}\n{source['sha256']}  {source['name']}\n"):
        raise RuntimeError("The checksum file does not match the archive")

    with github_session() as client:
        branch = git("branch", "--show-current")
        ref = checked(client.get(api + f"/git/ref/heads/{branch}", timeout=30))
        if ref["object"]["sha"] != commit:
            raise RuntimeError("Push the release commit before publishing")
        tag = checked(client.get(api + f"/git/ref/tags/{VERSION}", timeout=30))["object"]
        while tag["type"] == "tag":
            tag = checked(client.get(tag["url"], timeout=30))["object"]
        if tag["sha"] != commit:
            raise RuntimeError("The remote tag does not match the release commit")

        release = find_release(client, api, VERSION)
        if release is None:
            release = checked(client.post(api + "/releases", timeout=30, json={
                "tag_name": VERSION, "target_commitish": commit, "name": f"HDUSign {VERSION}",
                "draft": True, "prerelease": False, "body": notes}))
        print(f"Release {release['id']}: {VERSION}, draft={release['draft']}", flush=True)
        existing = {asset["name"]: asset for asset in release["assets"]}
        if set(existing) - {p.name for p in assets}:
            raise RuntimeError("The release has unexpected assets; inspect it before continuing")
        for path in assets:
            if path.name in existing:
                verify_asset(existing[path.name], path)
                continue
            if not release["draft"]:
                raise RuntimeError("Refusing to modify assets in a published release")
            print(f"Uploading {path.name} ({path.stat().st_size / 1024 ** 2:.1f} MiB)", flush=True)
            upload_asset(client, api, release, path)
        release = checked(client.get(api + f"/releases/{release['id']}", timeout=30))
        remote_assets = {asset["name"]: asset for asset in release["assets"]}
        for path in assets:
            verify_asset(remote_assets[path.name], path)
        if release["draft"]:
            release = checked(client.patch(api + f"/releases/{release['id']}", timeout=30,
                json={"body": notes, "draft": False, "make_latest": "true"}))
        elif release["body"].strip() != notes:
            raise RuntimeError("The published release notes differ; no changes were made")
        print(json.dumps({"url": release["html_url"], "tag": release["tag_name"],
                          "draft": release["draft"], "assets": [
                              {k: asset.get(k) for k in ("name", "size", "digest", "browser_download_url")}
                              for asset in release["assets"]]}, ensure_ascii=False, indent=2), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="Create and check the portable ZIP")
    build.add_argument("--ffmpeg-bundle", type=Path, required=True,
                       help="Verified minimal FFmpeg ZIP, with its source archive alongside")
    publish = commands.add_parser("publish", help="Upload the verified build to GitHub")
    publish.add_argument("--notes", type=Path, required=True, help="UTF-8 release notes")
    args = parser.parse_args()
    if args.command == "build":
        build_release(args.ffmpeg_bundle)
    else:
        publish_release(args.notes)


if __name__ == "__main__":
    main()
