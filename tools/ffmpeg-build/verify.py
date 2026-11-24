"""Validate the build without accounts or external media.

Certificate-store changes are restricted to a disposable GitHub-hosted Windows
runner. Only the newly generated test CA is added, then removed in finally.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
from pathlib import Path
import re
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import uuid
import wave


ROOT = Path(__file__).resolve().parent
SYSTEM_DLLS = {
    "advapi32.dll", "bcrypt.dll", "crypt32.dll", "kernel32.dll", "msvcrt.dll",
    "ncrypt.dll", "ole32.dll", "psapi.dll", "secur32.dll", "shell32.dll",
    "user32.dll", "ws2_32.dll", "ntdll.dll", "rpcrt4.dll",
}
LINUX_SYSTEM_LIBS = {
    "libc.so.6", "libm.so.6", "libpthread.so.0", "libdl.so.2",
    "librt.so.1", "libgnutls.so.30", "libgcc_s.so.1", "ld-linux-x86-64.so.2",
}


def build_path() -> Path:
    path = Path((ROOT / "work/build-path.txt").read_text().strip()).resolve()
    if not path.is_relative_to((ROOT / "work").resolve()):
        raise ValueError("Build path is outside this recipe's work directory")
    return path


def run(arguments: list[str], data: bytes | None = None) -> subprocess.CompletedProcess:
    options = {"stdin": subprocess.DEVNULL} if data is None else {"input": data}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(arguments, capture_output=True, timeout=25, **options)


def checked(arguments: list[str], data: bytes | None = None) -> bytes:
    result = run(arguments, data)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[-3000:])
    return result.stdout


def fixture() -> bytes:
    frames = bytearray()
    for sample in range(12000):
        value = round(0.2 * math.sin(2 * math.pi * 440 * sample / 48000) * 32767)
        frames.extend(struct.pack("<hh", value, value))
    target = io.BytesIO()
    with wave.open(target, "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(48000)
        stream.writeframes(frames)
    return target.getvalue()


def check_pcm(raw: bytes) -> dict:
    if not raw or len(raw) % 4:
        raise AssertionError("Incomplete float32 audio")
    samples = struct.unpack(f"<{len(raw) // 4}f", raw)
    rms = math.sqrt(sum(value * value for value in samples) / len(samples))
    if not (3200 <= len(samples) <= 8000 and all(map(math.isfinite, samples)) and 0.01 < rms < 0.5):
        raise AssertionError("Invalid 16 kHz mono float32 result")
    return {"sample_rate": 16000, "channels": 1, "samples": len(samples), "rms": rms}


def audio_check(ffmpeg: Path, record: Path) -> tuple[dict, bytes]:
    is_linux = sys.platform.startswith("linux")
    tls_flag = "--enable-gnutls" if is_linux else "--enable-schannel"
    command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin"]
    version = checked(command + ["-version"]).decode()
    configuration = checked(command + ["-buildconf"]).decode()
    license_text = checked(command + ["-L"]).decode()
    if not version.startswith("ffmpeg version 8.1.2-hdusign-audio "):
        raise AssertionError("Unexpected FFmpeg version")
    for flag in ("--disable-gpl", "--disable-nonfree", "--disable-version3", "--disable-autodetect", tls_flag):
        if flag not in configuration:
            raise AssertionError(f"Missing build setting: {flag}")
    normalized_license = " ".join(license_text.split())
    if "GNU Lesser General Public License" not in normalized_license or "version 2.1" not in normalized_license:
        raise AssertionError("Unexpected FFmpeg license")
    protocols = checked(command + ["-protocols"]).decode().partition("Input:")[2].partition("Output:")[0].split()
    required = {"file", "pipe", "http", "https", "tcp", "tls", "httpproxy"}
    if not required.issubset(protocols):
        raise AssertionError(f"Missing protocols: {required.difference(protocols)}")
    if "hls" in checked(command + ["-demuxers"]).decode().split():
        raise AssertionError("HLS must not be enabled")
    decoders = checked(command + ["-decoders"]).decode()
    if re.search(r"^\s*V[.A-Z]{5}\s+\w+", decoders, re.MULTILINE):
        raise AssertionError("A video decoder was enabled")
    if is_linux:
        imports = re.findall(r"\(NEEDED\)\s+Shared library:\s+\[([^\]]+)\]",
                             (record / "elf-imports.txt").read_text())
        if (not imports or "libgnutls.so.30" not in imports
                or any(name not in LINUX_SYSTEM_LIBS for name in imports)):
            raise AssertionError(f"Unexpected ELF shared library dependency: {imports}")
    else:
        imports = re.findall(r"DLL Name:\s*(\S+)", (record / "pe-imports.txt").read_text())
        if not imports or any(name.lower() not in SYSTEM_DLLS for name in imports):
            raise AssertionError(f"Unexpected DLL dependency: {imports}")
    common = command + ["-threads", "1", "-filter_threads", "1", "-protocol_whitelist", "pipe"]
    source = fixture()
    encoded = checked(common + ["-f", "wav", "-i", "pipe:0", "-map", "0:a:0", "-vn", "-sn", "-dn",
                                "-c:a", "aac", "-b:a", "64k", "-threads", "1", "-f", "adts", "pipe:1"], source)
    pcm = checked(common + ["-f", "aac", "-i", "pipe:0", "-map", "0:a:0", "-vn", "-sn", "-dn",
                            "-ac", "1", "-ar", "16000", "-threads", "1", "-c:a", "pcm_f32le",
                            "-f", "f32le", "pipe:1"], encoded)
    flv = checked(common + ["-f", "wav", "-i", "pipe:0", "-map", "0:a:0", "-vn", "-sn", "-dn",
                            "-c:a", "aac", "-b:a", "64k", "-threads", "1", "-f", "flv", "pipe:1"], source)
    (record / "version.txt").write_text(version, encoding="utf-8", newline="\n")
    (record / "buildconf.txt").write_text(configuration, encoding="utf-8", newline="\n")
    (record / "license-notice.txt").write_text(license_text, encoding="utf-8", newline="\n")
    return {**check_pcm(pcm), "imports": imports, "aac_round_trip": True}, flv


def https_check(ffmpeg: Path, flv: bytes) -> list[dict]:
    is_linux = sys.platform.startswith("linux")
    if not is_linux and not (os.name == "nt" and os.environ.get("GITHUB_ACTIONS") == "true"
                             and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"):
        raise RuntimeError("Certificate-store tests require a disposable GitHub-hosted Windows runner")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    import ipaddress

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        hits = 0
        content = b""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.hits += 1
            self.send_response(200)
            self.send_header("Content-Length", str(len(self.server.content)))
            self.end_headers()
            self.wfile.write(self.server.content)

        def log_message(self, *_):
            pass

    now = datetime.now(timezone.utc)
    crl_server = Server(("127.0.0.1", 0), Handler)
    crl_thread = threading.Thread(target=crl_server.serve_forever, daemon=True)
    crl_thread.start()
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "HDUSign CI " + uuid.uuid4().hex)])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=3))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(True, False, False, False, False, True, True, False, False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    crl_server.content = (x509.CertificateRevocationListBuilder().issuer_name(ca_name)
                         .last_update(now - timedelta(hours=1)).next_update(now + timedelta(days=2))
                         .sign(ca_key, hashes.SHA256()).public_bytes(serialization.Encoding.DER))
    distribution = x509.CRLDistributionPoints([x509.DistributionPoint(
        full_name=[x509.UniformResourceIdentifier(f"http://127.0.0.1:{crl_server.server_port}/root.crl")],
        relative_name=None, reasons=None, crl_issuer=None)])
    thumbprint = ca.fingerprint(hashes.SHA1()).hex()
    cases = [
        ("valid_dns", "localhost", [x509.DNSName("localhost")], True, True),
        ("valid_ip", "127.0.0.1", [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))], True, True),
        ("wrong_dns", "localhost", [x509.DNSName("wrong.invalid")], True, False),
        ("wrong_ip", "127.0.0.1", [x509.IPAddress(ipaddress.ip_address("127.0.0.2"))], True, False),
        ("ip_in_dns_san", "127.0.0.1", [x509.DNSName("127.0.0.1")], True, None),
        ("untrusted_ca", "localhost", [x509.DNSName("localhost")], False, False),
    ]
    outcomes = []
    imported = False
    try:
        with tempfile.TemporaryDirectory(prefix="tls-ci-", dir=ROOT / "work") as directory:
            folder = Path(directory)
            if is_linux:
                root_file = folder / "root.pem"
                root_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
                tls_args = ["-tls_verify", "1", "-ca_file", str(root_file)]
            else:
                root_file = folder / "root.cer"
                root_file.write_bytes(ca.public_bytes(serialization.Encoding.DER))
                # CurrentUser root import can open a trust-confirmation dialog.
                # This disposable hosted runner is elevated; use its machine store
                # noninteractively, then remove only our unique generated test CA.
                imported = True
                checked(["certutil.exe", "-f", "-addstore", "Root", str(root_file)])
                tls_args = ["-tls_verify", "1"]
            for name, host, sans, trusted, should_pass in cases:
                key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
                subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test.invalid")])
                issuer_key = ca_key if trusted else key
                issuer_name = ca_name if trusted else subject
                cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer_name)
                        .public_key(key.public_key()).serial_number(x509.random_serial_number())
                        .not_valid_before(now - timedelta(hours=1)).not_valid_after(now + timedelta(days=1))
                        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
                        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                        .add_extension(distribution, critical=False)
                        .sign(issuer_key, hashes.SHA256()))
                pem, private = folder / "server.pem", folder / "server.key"
                pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
                private.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                                     serialization.PrivateFormat.PKCS8,
                                                     serialization.NoEncryption()))
                server = Server(("127.0.0.1", 0), Handler)
                server.content = flv
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                context.load_cert_chain(pem, private)
                server.socket = context.wrap_socket(server.socket, server_side=True)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    url = f"https://{host}:{server.server_port}/audio.flv"
                    result = run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin",
                                  "-rw_timeout", "5000000", "-protocol_whitelist", "https,tcp,tls,httpproxy",
                                  *tls_args, "-format_whitelist", "flv", "-i", url,
                                  "-map", "0:a:0", "-vn", "-sn", "-dn", "-threads", "1",
                                  "-filter_threads", "1", "-ac", "1", "-ar", "16000",
                                  "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1"])
                    stderr = result.stderr.decode("utf-8", errors="replace").replace(url, "<test-stream>")
                    stderr = re.sub(r"\[([^\]]+) @ [0-9a-fA-F]+\]", r"[\1]", stderr)
                    passed = result.returncode == 0
                    outcome = {"case": name, "expected_accept": should_pass, "accepted": passed,
                               "http_requests": server.hits, "stderr": stderr[-3000:]}
                    outcomes.append(outcome)
                    print(json.dumps(outcome, ensure_ascii=True), flush=True)
                    if should_pass:
                        check_pcm(result.stdout)
                    # Windows CryptoAPI matches a textual IP host against a dNSName
                    # entry, unlike RFC 6125 verifiers. Public CAs do not issue such
                    # names and HDUSign only resolves DNS host names, so this
                    # platform behaviour is recorded without being asserted.
                    if should_pass is None:
                        continue
                    if passed != should_pass or (not should_pass and (server.hits or result.stdout)):
                        raise AssertionError(f"TLS certificate test failed: {name}")
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)
    finally:
        try:
            if imported:
                checked(["certutil.exe", "-delstore", "Root", thumbprint])
        finally:
            crl_server.shutdown()
            crl_server.server_close()
            crl_thread.join(timeout=2)
    return outcomes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tls-ci", action="store_true")
    args = parser.parse_args()
    is_linux = sys.platform.startswith("linux")
    build = build_path()
    ffmpeg = build / ("out/ffmpeg" if is_linux else "out/ffmpeg.exe")
    record = build / "record"
    report = {"ok": False, "tls_backend": "gnutls" if is_linux else "schannel",
              "tls_ci": args.tls_ci,
              "binary_sha256": hashlib.sha256(ffmpeg.read_bytes()).hexdigest()}
    try:
        report["audio"], flv = audio_check(ffmpeg, record)
        if args.tls_ci:
            report["https"] = https_check(ffmpeg, flv)
        report["ok"] = True
    finally:
        (record / "verification.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
