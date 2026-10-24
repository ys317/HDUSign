"""Offline visual check of the diagnostic switch; no external app actions."""
import os,sys,tempfile
from pathlib import Path
os.environ['QT_QPA_PLATFORM']='windows'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools import portable_tests
from PySide6.QtCore import QTimer,QObject,QPointF
from PySide6.QtGui import QGuiApplication
from hdusign.demo import prepare_demo,DemoController
from hdusign.qt_app import create_engine
app=QGuiApplication([])
with tempfile.TemporaryDirectory() as temporary:
    root=Path(temporary)
    prepare_demo(root)
    controller=DemoController(root)
    controller.setField('diagnostic',True)
    controller.save()
    engine,window=create_engine(controller,app)
    controller.navigateSettings.emit('diagnostic')
    def scroll():
        toggle=window.findChild(QObject,'diagnosticToggle')
        document=window.findChild(QObject,'settingsDocument')
        panel=document.parent()
        offset=toggle.mapToItem(panel.property('contentItem'),QPointF(0,0)).y()-65
        panel.setProperty('contentY',max(0,min(offset,float(panel.property('contentHeight'))-float(panel.property('height')))))
        QTimer.singleShot(200,capture)
    def capture():
        window.grabWindow().save(str(Path(__file__).resolve().parents[1]/'build/diagnostic-settings.png'))
        controller.shutdown();window.close();app.quit()
    QTimer.singleShot(300,scroll)
    app.exec()
