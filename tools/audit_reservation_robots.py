"""Migrate copies of local bookings and verify originals remain untouched."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import portable_tests
from PySide6.QtGui import QGuiApplication
from hdusign.multi_account import AccountManager
from hdusign.accounts import write_json
from hdusign.live import live_course_url
from hdusign.reservation_webhooks import binding_url

app = QGuiApplication.instance() or QGuiApplication([])
source, report_path = [Path(value).resolve() for value in sys.argv[1:3]]
files = [source/name for name in ('accounts.json', 'webhooks.json', 'config.yaml') if (source/name).exists()]
files += list((source/'account_data').glob('*/config.yaml'))
before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
with tempfile.TemporaryDirectory(prefix='hdusign-real-booking-copy-') as temporary:
    target = Path(temporary)
    for path in files:
        destination = target/path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    manager = AccountManager(target, auto_load=False)
    try:
        bookings = []
        for controller in manager.controllers.values():
            controller.shutdown()
            for course in controller._schedules:
                value = controller.cfg.get('reservation_webhooks', {}).get(live_course_url(course), {})
                assert value.get('mode') in ('bound', 'pending', 'local')
                assert value.get('mode') != 'bound' or binding_url(value)
                bookings.append({'account': controller.account_username, 'course': course.title,
                                 'mode': value['mode'], 'robot_name': value['name']})
        report = dict(ok=True, booking_count=len(bookings), migrated=bookings,
                      originals_unchanged=all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in before.items()),
                      school_requests=0, webhook_requests=0)
        assert report['originals_unchanged']
        write_json(report_path, report)
        print(json.dumps(report, ensure_ascii=True, indent=2))
    finally:
        manager.shutdown()
