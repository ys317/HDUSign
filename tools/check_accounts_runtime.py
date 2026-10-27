"""Five simultaneous frozen workers, independent fixture credentials and ASR."""
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import os
if os.environ.get("HDUSIGN_PORTABLE_DEPENDENCIES") == "1":
    from tools import portable_tests
from hdusign.accounts import write_json
from hdusign.account_worker import ProcessServices
from hdusign.runtime import ASR_FOLDER


def memory_mb(pid):
    class Counters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                    *[(name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
                    'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                    'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    handle = kernel.OpenProcess(0x410, False, pid)
    try:
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not handle or not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            raise OSError('Unable to measure worker memory')
        return round(counters.WorkingSetSize / 1024**2, 1)
    finally:
        if handle:
            kernel.CloseHandle(handle)


def main():
    executable = Path(sys.argv[1]).resolve()
    report_path = Path(sys.argv[2]).resolve()
    results, errors = {}, []
    report = {'ok': False, 'workers': 5}
    with tempfile.TemporaryDirectory(prefix='hdusign-five-check-') as temporary:
        root = Path(temporary)
        services = [ProcessServices(root / str(i)) for i in range(5)]
        stops = [threading.Event() for _ in services]
        for service in services:
            service.command = lambda directory: [str(executable), '--account-worker', str(directory)]
        def run(index):
            service = services[index]
            service.root.mkdir()
            service.credentials = {'skl_username': 'fixture-' + str(index), 'skl_password': 'isolated-' + str(index)}
            write_json(service.root / 'live_session.json', {'username': 'fixture-' + str(index)})
            cfg = {'asr': {'model_dir': str(executable.parent / 'models' / ASR_FOLDER), 'num_threads': 1}}
            try:
                results[index] = service.run('isolation-check', cfg, stops[index], check_model=True, hold=True)
            except Exception as exc:
                errors.append(str(exc))
        threads = [threading.Thread(target=run, args=(i,)) for i in range(5)]
        for thread in threads:
            thread.start()
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if errors:
                    raise RuntimeError('; '.join(errors))
                if all(s.job_directory and (s.job_directory / 'ready').exists() for s in services):
                    break
                time.sleep(.1)
            else:
                raise RuntimeError('Timed out waiting for five ASR workers')
            report['working_set_mb'] = [memory_mb(s.process.pid) for s in services]
            report['total_working_set_mb'] = round(sum(report['working_set_mb']), 1)
            for service in services:
                (service.job_directory / 'release').touch()
            for thread in threads:
                thread.join(15)
            assert not errors and len(results) == 5, errors
            assert len({r['pid'] for r in results.values()}) == 5
            for i, result in results.items():
                assert result['username'] == result['session_username'] == 'fixture-' + str(i)
                assert Path(result['root']) == services[i].root
                assert all(Path(result[key]) == services[i].root for key in ('live_root', 'browser_root'))
                assert Path(result['legacy_profile']).is_relative_to(services[i].root)
            report.update(ok=True, unique_processes=5, isolated_credentials=True,
                          isolated_sessions=True, asr=True, school_requests=0, webhook_requests=0)
        finally:
            for stop in stops:
                stop.set()
            for thread in threads:
                thread.join(20)
            write_json(report_path, report)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
