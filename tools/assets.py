"""Maintain the application icon and lossless desktop screenshots.

The screenshots command uses isolated demo data, without login, audio or webhooks.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def screenshots(args):
    # Set before creating QGuiApplication so screenshots exercise real Qt DPI
    # layout/rasterization, rather than scaling a previously captured bitmap.
    import os
    os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
    os.environ["QT_SCALE_FACTOR"] = str(args.scale)
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtTest import QTest
    from PySide6.QtCore import qInstallMessageHandler
    import shiboken6
    from hdusign.demo import DemoController, prepare_demo
    from hdusign.qt_app import create_engine

    messages = []
    qInstallMessageHandler(lambda kind, context, message: messages.append(message))
    app = QGuiApplication([])
    app.setQuitOnLastWindowClosed(False)
    with tempfile.TemporaryDirectory(prefix="hdusign-demo-") as temporary:
        root = Path(temporary)
        prepare_demo(root)
        controller = DemoController(root)
        engine, window = create_engine(controller, app)
        window.setProperty("settingsVisible", args.page == "extras")
        if args.preview:
            app.setQuitOnLastWindowClosed(True)
            app.exec()
        else:
            controller.populate_monitor()
            controller._clock.stop()
            controller._publish(elapsed="00:02:18")
            args.output.mkdir(parents=True, exist_ok=True)
            result = []
            for theme in ("dark", "light"):
                controller._publish(dark=theme == "dark")
                QTest.qWait(350)
                image = window.grabWindow()
                path = args.output.resolve() / f"{theme}.png"
                if image.isNull() or not image.save(str(path)):
                    raise RuntimeError("Qt Quick screenshot capture failed")
                if (image.width(), image.height()) != (round(1180 * args.scale), round(760 * args.scale)):
                    raise RuntimeError("Screenshot DPI did not match --scale")
                result.append(dict(file=str(path), pixels=[image.width(), image.height()],
                                   renderer=str(window.rendererInterface().graphicsApi())))
            print(json.dumps(result, ensure_ascii=False, indent=2))
        controller.shutdown()
        if controller.worker:
            controller.worker.join(timeout=2)
        shiboken6.delete(engine)
    if messages:
        raise RuntimeError("Qt warnings: " + "\n".join(messages))


def generate_icon():
    image = Image.new("RGBA", (256, 256))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((12, 12, 244, 244), radius=56, fill="#252525")
    for index, height in enumerate((64, 128, 94, 150)):
        x = 63 + 35 * index
        draw.rounded_rectangle(
            (x, 128 - height / 2, x + 20, 128 + height / 2),
            radius=8, fill="#ffffff")
    assets_dir = Path(__file__).resolve().parents[1] / "assets"
    image.save(assets_dir / "hdusign.ico",
               sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    image.save(assets_dir / "hdusign.png")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("icon", help="Generate the app's waveform icon")
    shots = commands.add_parser("screenshots", help="Render the real UI with demo data")
    shots.add_argument("--preview", action="store_true")
    shots.add_argument("--output", type=Path, default=ROOT / "assets/screenshots")
    shots.add_argument("--scale", type=float, default=1.0)
    shots.add_argument("--page", choices=("basic", "extras"), default="basic")
    args = parser.parse_args()
    if args.command == "icon":
        generate_icon()
        return
    if sys.platform != "win32":
        parser.error("The screenshot exporter requires Windows.")
    if not 1 <= args.scale <= 3:
        parser.error("--scale must be between 1 and 3.")
    screenshots(args)


if __name__ == "__main__":
    main()
