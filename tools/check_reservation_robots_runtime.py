"""Five real frozen workers read only their own encrypted per-run routes."""
from concurrent.futures import ThreadPoolExecutor
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
from hdusign.reservation_webhooks import binding, webhook_key


def main():
    executable, report_path = [Path(value).resolve() for value in sys.argv[1:3]]
    with tempfile.TemporaryDirectory(prefix="hdusign-robot-routes-") as temporary:
        root = Path(temporary)
        services, routes, stops, configs = [], [], [], []
        for index in range(5):
            folder = root / str(index)
            folder.mkdir()
            user = f"offline-robot-fixture-{index}"
            service = ProcessServices(folder)
            service.command = lambda directory: [str(executable), "--account-worker", str(directory)]
            service.credentials = {"skl_username": user, "skl_password": "offline-password"}
            write_json(folder / "live_session.json", {"username": user})
            path = folder / "notification_routes" / "fixture.json"
            path.parent.mkdir()
            spec = {"path": str(path), "run_id": f"run-{index}"}
            initial = binding(pending=True) if index == 4 else binding(f"独立机器人 {index}", f"https://example.invalid/assigned-{index}")
            write_json(path, {"run_id": spec["run_id"], "binding": initial})
            routes.append((path, spec))
            configs.append({"_notification_route": spec, "alert": {"webhook": {"url": "https://example.invalid/WRONG-DEFAULT"}}})
            services.append(service)
            stops.append(threading.Event())
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(service.run, "isolation-check", cfg, stop,
                                   check_notification_route=True, hold=True)
                       for service, cfg, stop in zip(services, configs, stops)]
            try:
                deadline = time.monotonic()+40
                while not all(service.job_directory and (service.job_directory/"ready").exists() for service in services):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Frozen notification route check timed out")
                    for future in futures:
                        if future.done():
                            future.result()
                    time.sleep(.05)
                path, spec = routes[4]
                write_json(path, {"run_id": spec["run_id"], "binding": binding("人工新选定", "https://example.invalid/assigned-4")})
                for service in services:
                    (service.job_directory/"release").touch()
                results = [future.result(timeout=15) for future in futures]
                for index, result in enumerate(results):
                    expected = webhook_key(f"https://example.invalid/assigned-{index}")
                    assert result["notification_key_after"] == expected
                    assert result["notification_key_before"] == ("" if index == 4 else expected)
                assert len({result["pid"] for result in results}) == 5
            finally:
                for stop in stops:
                    stop.set()
        report = dict(ok=True, independent_workers=5, exclusive_course_routes=True,
                      pending_suppresses_push=True, manual_selection_updates_running_route=True,
                      account_default_never_used=True, school_requests=0, webhook_requests=0)
        write_json(report_path, report)
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
