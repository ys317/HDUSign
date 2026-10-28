"""The real account manager switching QML models while another account runs."""
from pathlib import Path
import tempfile
import time
import unittest

import shiboken6
from PySide6.QtCore import QObject, QPointF, Qt, qInstallMessageHandler
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest

from hdusign.accounts import AccountStore
from hdusign.demo import prepare_demo, demo_courses
from hdusign.live import live_course_url
from hdusign.multi_account import AccountManager
from hdusign.qt_app import create_engine
from test_multi_account import IsolatedFakeServices

APP = QGuiApplication.instance() or QGuiApplication([])
APP.setQuitOnLastWindowClosed(False)


class MultiQmlTests(unittest.TestCase):
    def test_switch_during_monitor_and_named_webhook_models_have_no_qml_errors(self):
        messages = []
        previous = qInstallMessageHandler(lambda kind, ctx, message: messages.append(message))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepare_demo(root)
            store = AccountStore(root)
            store.save('student-a', 'password-a')
            store.save('student-b', 'password-b')
            manager = AccountManager(root, services_factory=IsolatedFakeServices, auto_load=False)
            engine, window = create_engine(manager, APP)
            try:
                manager.activate('student-a')
                first = manager.active
                first.set_courses(demo_courses())
                first.selectCourse(live_course_url(first._courses[0]))
                first.start_monitor()
                QTest.qWait(70)
                manager.activate('student-b')
                QTest.qWait(70)
                current = engine.rootContext().contextProperty('appController')
                self.assertIs(current, manager.active)
                self.assertEqual(current._form['username'], 'student-b')
                self.assertEqual(first._task, 'monitor')
                account_list = window.findChild(QObject, 'accountTaskList')
                self.assertIsNotNone(account_list)
                rows = account_list.property('model')
                self.assertEqual([row['username'] for row in rows], ['student-b', 'student-a'])
                current.navigateSettings.emit('webhookName')
                hook = manager.webhooks.remember_verified('测试群', 'https://example.invalid/fixture')
                manager.assignWebhook(hook)
                manager.setDefaultWebhook(hook)
                QTest.qWait(70)
                box = window.findChild(QObject, 'webhookBox')
                self.assertIsNotNone(box)
                self.assertEqual(box.property('currentText'), '测试群')
                self.assertEqual(current._form['webhookName'], '测试群')
                # The second visible row is now A. Clicking it must select A
                # and move it above B without interrupting A's running task.
                QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier,
                                 account_list.mapToScene(QPointF(20, 61)).toPoint())
                QTest.qWait(70)
                self.assertIs(engine.rootContext().contextProperty('appController'), first)
                rows = account_list.property('model')
                self.assertEqual([row['username'] for row in rows], ['student-a', 'student-b'])
                self.assertEqual(first._task, 'monitor')
                manager.stopAll()
                if first.worker:
                    first.worker.join(2)
                first.drain()
            finally:
                manager.shutdown()
                for controller in manager.controllers.values():
                    if controller.worker:
                        controller.worker.join(2)
                shiboken6.delete(engine)
                shiboken6.delete(manager)
                qInstallMessageHandler(previous)
        self.assertEqual(messages, [])


if __name__ == '__main__':
    unittest.main()
