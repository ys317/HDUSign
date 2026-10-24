"""One entry point for the desktop, command-line tools and frozen executable."""
from __future__ import annotations

import argparse
import os
import sys


def main(argv=None) -> int:
    from hdusign.runtime import application_root, configure_qt_runtime

    # Windowed executables can inherit GBK-encoded redirected handles. ASR text
    # must not crash logging, and worker logs are read as UTF-8.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            # Frozen Python can ignore PYTHONUNBUFFERED. Startup messages must
            # reach the GUI before the first nonempty ASR partial flushes stdout.
            reconfigure(encoding="utf-8", errors="replace", line_buffering=True, write_through=True)
    os.chdir(application_root())
    configure_qt_runtime()
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--account-worker":
        from hdusign.account_worker import worker_main
        return worker_main(args[1:])
    if args and args[0] in ("devices", "run", "test", "demo", "code", "webhook-test"):
        from hdusign.monitor import main as monitor_main

        return monitor_main(args)
    parser = argparse.ArgumentParser(
        prog="HDUSign", description="不带参数启动桌面界面。",
        epilog="命令行：devices · run · test · demo · code · webhook-test；使用 <命令> --help 查看选项。")
    from hdusign import __version__

    parser.add_argument("--version", action="version", version=f"HDUSign v{__version__}")
    parser.add_argument("--check-runtime", metavar="REPORT", help=argparse.SUPPRESS)
    options = parser.parse_args(args)
    if options.check_runtime:
        from hdusign.runtime import check_runtime

        return check_runtime(options.check_runtime)
    from hdusign.qt_app import main as gui_main

    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
