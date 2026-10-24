"""Offline screenshot: two bookings remain while viewing a third course."""
import datetime as dt
import os
from pathlib import Path
import sys
import tempfile

os.environ["QT_QPA_PLATFORM"] = "windows"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import portable_tests
from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication
from hdusign.demo import prepare_demo, DemoServices
from hdusign.desktop_controller import Controller
from hdusign.live import LiveCourse, live_course_url
from hdusign.qt_app import create_engine

app = QGuiApplication([])
with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    prepare_demo(root)
    controller = Controller(root, DemoServices(), auto_load=False)
    tomorrow = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date() + dt.timedelta(days=1)
    def stamp(hour, minute):
        return dt.datetime.combine(tomorrow, dt.time(hour, minute), dt.timezone(dt.timedelta(hours=8))).timestamp()
    courses = [
        LiveCourse("801", "金融科技概论", stamp(8, 5), stamp(9, 40), teacher="薛老师", section="第1–2节 · 2节连堂"),
        LiveCourse("802", "习近平新时代中国特色社会主义思想概论", stamp(10, 0), stamp(12, 25), teacher="杨老师", section="第3–5节 · 3节连堂"),
        LiveCourse("803", "统计学概论", stamp(15, 15), stamp(16, 50), teacher="赵老师", section="第8–9节 · 2节连堂"),
    ]
    controller.set_courses(courses)
    for course in courses[1:]:
        controller.selectCourse(live_course_url(course))
        controller.toggleMonitor()
    controller.selectCourse(live_course_url(courses[0]))
    engine, window = create_engine(controller, app)
    def capture():
        destination = Path(__file__).resolve().parents[1] / "build/multiple-reservations.png"
        if not window.grabWindow().save(str(destination)):
            raise RuntimeError("Screenshot failed")
        controller.shutdown()
        window.close()
        app.quit()
    QTimer.singleShot(250, capture)
    app.exec()
