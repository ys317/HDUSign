"""Render the actual QML and exercise input with isolated demo services."""
from pathlib import Path
from dataclasses import replace
import tempfile
import unittest

import shiboken6
from PySide6.QtCore import QObject, QPointF, Qt, qInstallMessageHandler
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest

from hdusign.demo import DemoController, prepare_demo
from hdusign.qt_app import create_engine

APP = QGuiApplication.instance() or QGuiApplication([])
APP.setQuitOnLastWindowClosed(False)


class QmlDesktopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        prepare_demo(self.root)
        self.c = DemoController(self.root)
        self.c._clock.stop()
        self.messages = []
        self.old_handler = qInstallMessageHandler(lambda kind, context, msg: self.messages.append(msg))
        self.engine, self.window = create_engine(self.c, APP)
        QTest.qWait(100)

    def tearDown(self):
        self.c.shutdown()
        if self.c.worker:
            self.c.worker.join(2)
        shiboken6.delete(self.engine)
        shiboken6.delete(self.c)
        self.tmp.cleanup()
        qInstallMessageHandler(self.old_handler)
        self.assertEqual(self.messages, [])

    def item(self, name):
        item = self.window.findChild(QObject, name)
        self.assertIsNotNone(item, name)
        return item

    def click(self, name):
        item = self.item(name)
        pos = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
        QTest.mouseClick(self.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
        QTest.qWait(50)

    def test_quote_is_content_height_and_clamps_document_width(self):
        self.c.populate_monitor()
        QTest.qWait(30)
        quote, transcript = self.item('quoteBlock'), self.item('transcript')
        self.assertAlmostEqual(quote.height(), max(28, transcript.property('contentHeight')) + 8)
        initial = quote.height()
        self.c.consume(['[ASR 00:00:00] ' + '这是一段很长的课堂转写。' * 60])
        QTest.qWait(30)
        self.assertGreater(quote.height(), initial * 3)
        self.c.consume(['[ASR 00:00:01] 简短转写。'])
        QTest.qWait(30)
        self.assertAlmostEqual(quote.height(), initial)
        for width in (960, 1400, 1500):
            self.window.setWidth(width)
            QTest.qWait(40)
            self.assertLessEqual(self.item('document').width(), 1120)
            if width == 1400:
                self.assertGreater(self.item('document').width(), 760)
            card, button = self.item('codeCard'), self.item('copyButton')
            self.assertLess(button.mapToItem(card, QPointF(button.width(), 0)).x(), card.width())

    def test_theme_copy_focus_and_activity_toggle_receive_mouse_input(self):
        self.c.populate_monitor()
        self.click('copyButton')
        self.assertEqual(APP.clipboard().text(), '2330')
        self.assertTrue(self.c._state['copied'])
        self.assertIsNone(self.window.findChild(QObject, 'logList'))
        self.click('activityToggle')
        self.assertTrue(self.window.property('activityExpanded'))
        self.assertIsNotNone(self.item('logList'))
        scroll = self.item('documentScroll')
        scroll.setProperty('contentY', scroll.property('contentHeight') - scroll.property('height'))
        QTest.qWait(50)
        follow = self.item('followToggle')
        self.assertTrue(follow.property('checked'))
        self.click('followToggle')
        self.assertFalse(self.window.property('followLogs'))
        self.assertFalse(follow.property('checked'))
        self.click('activityToggle')
        self.assertFalse(self.window.property('activityExpanded'))
        self.assertIsNone(self.window.findChild(QObject, 'logList'))
        self.click('focusButton')
        self.assertTrue(self.window.property('collapsed'))
        self.c.toggleTheme()
        QTest.qWait(30)
        self.assertEqual(self.window.color().name(), '#ffffff')
        self.assertFalse(self.window.grabWindow().isNull())

    def test_text_entry_survives_timer_and_transcript_updates(self):
        self.click('navSettings')
        field = self.item('field_username')
        self.click('field_username')
        QTest.keyClick(self.window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        for character in '12345':
            QTest.keyClick(self.window, ord(character))
        before = field.property('cursorPosition')
        self.c._publish(elapsed='00:00:41', transcript='新的转写')
        QTest.qWait(20)
        self.assertEqual(field.property('text'), '12345')
        self.assertEqual(field.property('cursorPosition'), before)
        self.assertEqual(self.c._form['username'], '12345')

    def test_invalid_field_is_scrolled_into_view(self):
        self.c.setField('webhook', 'invalid')
        self.c.runAction('webhook')
        QTest.qWait(80)
        field = self.item('field_webhook')
        self.assertTrue(self.window.property('settingsVisible'))
        self.assertTrue(field.hasActiveFocus())
        top = field.mapToScene(QPointF()).y()
        self.assertGreater(top, 160)
        self.assertLess(top + field.height(), self.window.height() - 80)

    def test_settings_is_a_workspace_page_with_existing_actions(self):
        self.click('navSettings')
        settings = self.item('settingsPage')
        self.assertTrue(settings.isVisible())
        self.assertFalse(self.item('documentScroll').isVisible())
        self.assertEqual(self.item('pageBreadcrumb').property('text'), '设置')
        self.assertGreater(settings.width(), 700)
        self.assertTrue(self.item('field_username').isVisible())
        for name in ('field_password', 'field_url',
                     'field_rules', 'field_webhook'):
            self.assertIsNotNone(self.item(name), name)
        for name in ('field_latitude', 'field_longitude'):
            self.assertIsNone(self.window.findChild(QObject, name))
        self.assertIsNotNone(self.item('navLive'))
        self.assertIsNotNone(self.item('monitorButton'))

    def test_course_page_title_and_breadcrumb_follow_selection(self):
        self.assertEqual(self.window.property('title'), '')
        self.assertEqual(self.item('pageBreadcrumb').property('text'), '计算机网络')
        self.assertEqual(self.item('pageTitle').property('text'), '计算机网络')
        self.assertEqual(self.item('courseLibraryHeading').property('text'), '课程库')
        self.assertIn('王老师', self.item('pageSubtitle').property('text'))
        self.assertIsNone(self.window.findChild(QObject, 'accountMenu'))
        self.c.selectCourse(self.c.course_model.items[1]['url'])
        QTest.qWait(30)
        self.assertEqual(self.item('pageTitle').property('text'), '数据库原理')
        self.assertEqual(self.item('pageBreadcrumb').property('text'), '数据库原理')
        self.assertIn('李老师', self.item('pageSubtitle').property('text'))
        self.assertEqual(self.item('monitorButton').property('text'), '预约直播')
        self.c.setField('live', False)
        QTest.qWait(30)
        self.assertEqual(self.item('pageTitle').property('text'), '电脑声音')

    def test_settings_and_course_page_share_alignment_at_supported_widths(self):
        self.c.set_courses([replace(self.c._courses[0], title='计算机网络与分布式系统：课程实验及教学研讨')])
        for width in (960, 1180, 1500):
            self.window.setWidth(width)
            self.click('navSettings')
            settings = self.item('settingsDocument')
            course = self.item('document')
            self.assertAlmostEqual(settings.width(), course.width())
            self.assertAlmostEqual(settings.mapToScene(QPointF()).x(), course.mapToScene(QPointF()).x())
            self.click('navLive')
            self.assertLess(self.item('courseList').property('contentHeight'), 100)
            button = self.item('monitorButton')
            title = self.item('pageTitle')
            self.assertLess(title.mapToScene(QPointF(title.width(), 0)).x(), button.mapToScene(QPointF()).x())
            self.assertLessEqual(button.mapToScene(QPointF(button.width(), 0)).x(), self.window.width() - 24)

    def test_empty_courses_and_failed_load_show_distinct_recoverable_states(self):
        self.c.setField('url', '')
        self.c.set_courses([])
        QTest.qWait(30)
        self.assertEqual(self.item('courseNoticeTitle').property('text'), '今明暂无直播课程')
        self.assertFalse(self.item('monitorButton').isEnabled())
        self.assertEqual(self.item('codeValue').property('text'), '等待识别')
        self.assertFalse(self.item('copyButton').isEnabled())
        self.c._publish(courseLoadState='error', courseError='课程暂时不可用')
        QTest.qWait(30)
        self.assertEqual(self.item('courseNoticeTitle').property('text'), '课程加载失败')
        self.click('retryCoursesButton')
        for _ in range(50):
            if not self.c._task:
                break
            QTest.qWait(20)
        self.assertEqual(self.c._state['courseLoadState'], 'ready')
        self.assertEqual(self.c._state['courseError'], '')
        self.assertEqual(self.item('courseNoticeTitle').property('text'), '选择一节课程')

    def test_stop_action_remains_reachable_from_settings(self):
        self.click('monitorButton')
        self.click('navSettings')
        self.assertTrue(self.item('ongoingAction').isVisible())
        self.click('ongoingAction')
        for _ in range(50):
            if not self.c._task:
                break
            QTest.qWait(20)
        self.assertFalse(self.c._state['monitoring'])
        self.assertFalse(self.item('ongoingAction').isVisible())

    def test_monitor_stop_and_close_complete_without_blocking_window(self):
        self.click('monitorButton')
        self.assertEqual(self.c._task, 'monitor')
        self.assertTrue(self.c._state['monitoring'])
        self.assertFalse(self.window.close())
        for _ in range(50):
            if not self.c._task:
                break
            QTest.qWait(20)
        self.assertFalse(self.c._task)
        self.assertFalse(self.window.isVisible())


if __name__ == '__main__':
    unittest.main()
