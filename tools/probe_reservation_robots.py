"""Offline visual review of the real conflict popup and confirmed booking."""
import os
from pathlib import Path
import sys

os.environ["QT_QPA_PLATFORM"] = "windows"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import portable_tests
import shiboken6
from PySide6.QtCore import QTimer
from test_reservation_webhooks import ReservationRobotTests, APP
from hdusign.qt_app import create_engine

fixture = ReservationRobotTests('test_qml_popup_lists_only_free_robots_and_confirm_binds_the_new_course')
fixture.setUp()
fixture.book(0, 0)
fixture.manager.activate('student-b')
engine, window = create_engine(fixture.manager, APP)
fixture.book(1, 1)
out = Path(__file__).resolve().parents[1]/'build'

def capture_popup():
    assert window.grabWindow().save(str(out/'robot-conflict-dialog.png'))
    fixture.choose(1)
    QTimer.singleShot(250, capture_booking)

def capture_booking():
    assert window.grabWindow().save(str(out/'course-bound-robot.png'))
    shiboken6.delete(engine)
    fixture.tearDown()
    APP.quit()

QTimer.singleShot(300, capture_popup)
APP.exec()
