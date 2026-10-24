"""Offline packaged-EXE check: five scoped logs/audio plus recording disabled."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import threading
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import portable_tests
from hdusign.accounts import write_json
from hdusign.account_worker import ProcessServices


def main():
    exe = Path(sys.argv[1]).resolve()
    report_path = Path(sys.argv[2]).resolve()
    with tempfile.TemporaryDirectory(prefix='hdusign-diagnostic-check-') as temporary:
        root = Path(temporary)
        def run(index, enabled=True):
            folder = root / str(index)
            folder.mkdir()
            service = ProcessServices(folder)
            service.command = lambda directory: [str(exe), '--account-worker', str(directory)]
            user = 'offline-fixture-' + str(index)
            service.credentials = {'skl_username': user, 'skl_password': 'PASSWORD-CANARY-' + str(index)}
            write_json(folder / 'live_session.json', {'username': user})
            cfg = {'diagnostics': {'enabled': enabled},
                   'notification_context': {'username': user, 'course_name': '离线诊断自检'},
                   'alert': {'webhook': {'url': 'https://example.invalid/WEBHOOK-CANARY'}}}
            result = service.run('isolation-check', cfg, threading.Event(), check_diagnostics=True,
                                 fixture_sample_value=.05 * (index + 1))
            assert result['diagnostics_checked']
            directory = service.last_log_directory
            assert directory and not service.job_directory.exists()
            summary = json.loads((directory / 'summary.json').read_text('utf-8'))
            assert summary['account'] == user
            assert 'first_audio' in summary['timings'] and 'first_partial' in summary['timings']
            assert 'first_final' in summary['timings'] and 'task_requested' in summary['timings']
            assert (directory / 'startup-audio.wav').exists() == enabled
            if enabled:
                import struct
                with wave.open(str(directory / 'startup-audio.wav'), 'rb') as recording:
                    assert recording.getnframes() == 16000
                    sample = struct.unpack('<h', recording.readframes(1))[0]
                    assert sample == round(.05 * (index + 1) * 32768)
            for path in directory.iterdir():
                if path.suffix != '.wav':
                    content = path.read_text('utf-8')
                    assert 'PASSWORD-CANARY' not in content and 'WEBHOOK-CANARY' not in content
            return True
        with ThreadPoolExecutor(max_workers=5) as pool:
            assert all(pool.map(run, range(5)))
        assert run(5, False)
        report = dict(ok=True, parallel_accounts=5, separate_audio_samples=True, persistent_logs=True,
                      timestamps=True, recording_disabled_creates_no_wav=True,
                      credentials_absent=True, synthetic_input=True, school_requests=0, webhook_requests=0)
        write_json(report_path, report)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
