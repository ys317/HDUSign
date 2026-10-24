import os
import sys
import tempfile
from pathlib import Path
os.environ['QT_QPA_PLATFORM'] = 'windows'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import portable_tests
from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication
from hdusign.demo import DemoController, prepare_demo
from hdusign.qt_app import create_engine
app = QGuiApplication([])
with tempfile.TemporaryDirectory() as folder:
    root = Path(folder)
    prepare_demo(root)
    controller = DemoController(root)
    controller.account_store.save('2026001001', 'demo-only')
    engine, window = create_engine(controller, app)
    controller.navigateSettings.emit('username')
    def capture():
        window.grabWindow().save(str(Path(__file__).resolve().parents[1] / 'build/accounts-settings.png'))
        controller.shutdown()
        window.close()
        app.quit()
    QTimer.singleShot(800, capture)
    app.exec()
