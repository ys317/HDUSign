"""Offline Chinese heading rendering probe; never reads personal configuration."""
import json
import os
from pathlib import Path
import sys
import tempfile

os.environ['QT_QPA_PLATFORM'] = 'windows'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import portable_tests
from PySide6.QtCore import QTimer, QObject
from PySide6.QtGui import QGuiApplication, QFont, QFontInfo, QRawFont, QFontDatabase
from hdusign.demo import DemoController, prepare_demo
from hdusign.live import LiveCourse, live_course_url
from hdusign.qt_app import create_engine

root = Path(__file__).resolve().parents[1]
out = root / 'build/font-probe'
out.mkdir(parents=True, exist_ok=True)
app = QGuiApplication([])
temporary = tempfile.TemporaryDirectory()
directory = Path(temporary.name)
prepare_demo(directory)
controller = DemoController(directory)
course = LiveCourse('123', '公司金融学', 1, 9999999999, teacher='演示教师',
                    classroom='演示教室', section='3节连堂', tecl_id='456')
controller.set_courses([course])
controller.selectCourse(live_course_url(course))
controller.populate_monitor()
if '--legacy-register' in sys.argv:
    # Reproduce the old font registration without changing system settings.
    QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
engine, window = create_engine(controller, app)

def capture():
    title = window.findChild(QObject, 'coursePageHeading')
    if title is None:
        title = window.findChild(QObject, 'pageTitle')
    fonts = []
    for family in ('Microsoft YaHei', 'Microsoft YaHei UI', 'Noto Sans SC', 'Noto Serif SC'):
        for weight in (QFont.Weight.Normal, QFont.Weight.DemiBold, QFont.Weight.Bold):
            font = QFont(family, 24, weight)
            info = QFontInfo(font)
            raw = QRawFont.fromFont(font)
            fonts.append(dict(requested=family, weight=int(weight), resolved=info.family(),
                              style=info.styleName(), rawFamily=raw.familyName(),
                              rawStyle=raw.styleName(), glyphs=raw.glyphIndexesForString('公司金融学实时转写')))
    screenshot_name = 'legacy.png' if '--legacy-register' in sys.argv else 'fixed.png'
    window.grabWindow().save(str(out / screenshot_name))
    (out / 'fonts.json').write_text(json.dumps(fonts, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'screenshot': str(out/screenshot_name), 'body':engine.rootContext().contextProperty('bodyFont'),
                      'heading':engine.rootContext().contextProperty('headingFont')}, ensure_ascii=False))
    controller.shutdown()
    window.close()
    app.quit()

QTimer.singleShot(1000, capture)
app.exec()
temporary.cleanup()
