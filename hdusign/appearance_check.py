"""Opt-in compositor check over owned test windows and temporary accounts only."""
from pathlib import Path
import ctypes
import json
import tempfile
import time


def check_native_glass(report_path):
    from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QObject, QPointF, Qt, QTimer
    from PySide6.QtGui import QColor, QGuiApplication, QMouseEvent, QWindow
    from PySide6.QtQuick import QQuickWindow
    import shiboken6
    from hdusign.accounts import AccountStore, write_json
    from hdusign.appearance import Appearance, DEFAULTS, WindowsBackdrop
    from hdusign.live import LiveCourse
    from hdusign.multi_account import AccountManager
    from hdusign.qt_app import create_engine

    def wait(milliseconds):
        loop = QEventLoop()
        QTimer.singleShot(milliseconds, loop.quit)
        loop.exec()

    path = Path(report_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    app = QGuiApplication.instance() or QGuiApplication([])
    app.setQuitOnLastWindowClosed(False)
    if not WindowsBackdrop.supported():
        raise RuntimeError("Native glass check requires the Windows Qt platform and Windows 11 22H2+")
    QQuickWindow.setDefaultAlphaBuffer(True)
    backdrop = QQuickWindow()
    backdrop.setColor(QColor('#e03030'))
    backdrop.setGeometry(20, 20, 1350, 900)
    backdrop.show()
    manager = engine = window = None
    try:
        with tempfile.TemporaryDirectory(prefix='hdusign-glass-check-') as temporary:
            root = Path(temporary)
            store = AccountStore(root)
            for user in ('demo-a', 'demo-b'):
                store.save(user, 'offline-fixture-password')
            manager = AccountManager(root, auto_load=False)
            for user, controller in manager.controllers.items():
                write_json(controller.root / 'live_session.json',
                           {'username': user, 'display_name': '示例甲' if user == 'demo-a' else '示例乙'})
            course = LiveCourse('901', '预约排序演示课', time.time() + 3600, time.time() + 6300)
            first = manager.controllers['demo-a']
            first.set_courses([course])
            assert first._reserve_course(course)
            manager.activate('demo-b')
            assert [r['username'] for r in manager.accountRows] == ['demo-a', 'demo-b']
            engine, window = create_engine(manager, app)
            window.setGeometry(60, 60, 1180, 760)
            window.raise_()
            window.requestActivate()
            ctypes.windll.user32.SetForegroundWindow(int(window.winId()))
            wait(400)
            appearance = engine.rootContext().contextProperty('appearance')
            assert appearance.active
            def capture(label):
                wait(400)
                pixmap = app.primaryScreen().grabWindow(0, window.x(), window.y(), window.width(), window.height())
                assert not pixmap.isNull()
                image = pixmap.toImage()
                pixel = image.pixelColor(image.width() - 35, image.height() - 35)
                if label:
                    assert pixmap.save(str(path.with_name(path.stem + '-' + label + '.png')))
                return [pixel.red(), pixel.green(), pixel.blue()]

            pixels = []
            for color in ('#e03030', '#2070f0'):
                backdrop.setColor(QColor(color))
                pixels.append(capture(None))
            assert sum(abs(a - b) for a, b in zip(*pixels)) > 15, 'Desktop behind glass did not affect rendered pixels'
            def visual(item, name):
                if item.objectName() == name:
                    return item
                for child in item.childItems():
                    found = visual(child, name)
                    if found is not None:
                        return found
                return None
            first_fill = visual(window.contentItem(), 'accountRowBackground_demo-a').property('color')
            other_fill = visual(window.contentItem(), 'accountRowBackground_demo-b').property('color')
            assert first_fill != other_fill
            appearance.setValue('transparency', 0)
            low = capture(None)
            appearance.setValue('transparency', 100)
            high = capture(None)
            assert low != high and window.opacity() == 1.0
            appearance.reset()
            capture('dark')
            manager.active._publish(dark=False)
            capture('light')
            manager.active._publish(dark=True)
            appearance.setValue('enabled', False)
            opaque = capture('opaque')
            assert opaque == [25, 25, 25]
            appearance.setValue('enabled', True)
            assert appearance.save()
            restored = Appearance(root)
            assert all(restored.values.value(k) == v for k, v in DEFAULTS.items())
            window.setProperty('settingsVisible', True)
            settings = window.findChild(QObject, 'settingsPage')
            wait(50)
            settings.setProperty('contentY', max(0, settings.property('contentHeight') - settings.height()))
            capture('settings')
            def click(name):
                item = window.findChild(QObject, name)
                pos = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
                local = QPointF(pos)
                global_pos = QPointF(window.mapToGlobal(pos))
                for kind, buttons in ((QEvent.MouseButtonPress, Qt.LeftButton),
                                      (QEvent.MouseButtonRelease, Qt.NoButton)):
                    event = QMouseEvent(kind, local, global_pos, Qt.LeftButton, buttons, Qt.NoModifier)
                    QCoreApplication.sendEvent(window, event)
                wait(200)
            click('glassMaximize')
            assert window.visibility() == QWindow.Maximized
            click('glassMaximize')
            assert window.visibility() == QWindow.Windowed
            click('glassMinimize')
            assert window.visibility() == QWindow.Minimized
            window.showNormal()
            wait(100)
            click('glassClose')
            assert not window.isVisible()
            result = dict(ok=True, native_desktop_changes=pixels,
                          transparency_slider_changes=[low, high], reserved_first=True,
                          reservation_background_distinct=True, text_window_opacity=1.0,
                          global_settings_reloaded=True, opaque_fallback=True,
                          native_caption_controls=True, renderer=str(window.rendererInterface().graphicsApi()),
                          school_requests=0, webhook_requests=0)
            write_json(path, result)
            print(json.dumps(result, ensure_ascii=True, indent=2))
    finally:
        if manager:
            manager.shutdown()
        if window:
            window.hide()
        backdrop.hide()
        if engine:
            shiboken6.delete(engine)
        if manager:
            shiboken6.delete(manager)
    return 0
