"""Isolated, offline visual check for account tasks and named webhooks."""
import os,sys,tempfile
from pathlib import Path
os.environ['QT_QPA_PLATFORM']='windows'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools import portable_tests
from PySide6.QtCore import QTimer,QObject
from PySide6.QtGui import QGuiApplication
from hdusign.demo import prepare_demo,DemoServices,demo_courses
from hdusign.accounts import AccountStore,write_json
from hdusign.multi_account import AccountManager
from hdusign.qt_app import create_engine
from hdusign.live import live_course_url
class OfflineServices(DemoServices):
    isolated=True
    def monitor(self,cfg,stop):
        self.log('[i] ASR 就绪 · 正在接收直播音频')
        self.log('[ASR 10:00:01] 当前是多账号离线演示，未连接课堂或签到。')
        stop.wait()
app=QGuiApplication([])
out=Path(__file__).resolve().parents[1]/'build'
with tempfile.TemporaryDirectory() as temporary:
    root=Path(temporary);prepare_demo(root)
    accounts=AccountStore(root)
    for i in range(5):accounts.save('demo-'+str(i),'demo-only')
    manager=AccountManager(root,services_factory=lambda root:OfflineServices(),auto_load=False)
    default=manager.webhooks.remember_verified('全班通知群（演示）','https://example.invalid/common')
    private=manager.webhooks.remember_verified('个人提醒（演示）','https://example.invalid/private')
    for i,(user,c) in enumerate(manager.controllers.items()):
        if user:
            c.set_courses(demo_courses());c.selectCourse(live_course_url(c._courses[0]))
            write_json(c.root/'live_session.json',{'username':user,'display_name':'演示同学'+str(i+1)})
            manager.bind_webhook(c, private if i==0 else '')
            c.start_monitor()
    engine,window=create_engine(manager,app)
    manager.changed.emit()
    def first():
        window.grabWindow().save(str(out/'multi-five-tasks.png'))
        manager.stopAccount('demo-4')
        QTimer.singleShot(150, second)
    def second():
        manager.active.drain()
        manager.active.navigateSettings.emit('webhookName')
        QTimer.singleShot(200, capture)
    def capture():
        panel=window.findChild(QObject,'settingsDocument').parent()
        panel.setProperty('contentY',float(panel.property('contentHeight'))-float(panel.property('height')))
        QTimer.singleShot(150, finish)
    def finish():
        window.grabWindow().save(str(out/'multi-webhooks.png'))
        manager.stopAll()
        for c in manager.controllers.values():
            if c.worker:c.worker.join(2)
            c.drain()
        manager.shutdown();window.close();app.quit()
    QTimer.singleShot(500,first)
    app.exec()
