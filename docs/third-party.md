# 第三方组件

HDUSign 的 MIT 许可证适用于本项目原创代码，不改变第三方软件与模型的许可条款。

- **Chromium / Chrome for Testing**：由 Playwright 对应版本提供，保留完整发行文件及其中的许可资料。组件声明可在浏览器的 `chrome://credits` 中查看。来源：<https://chromium.googlesource.com/chromium/src/+/main/LICENSE>。
- **FFmpeg 直播音频解码器**：从 [FFmpeg 8.1.2 官方源码](https://ffmpeg.org/releases/ffmpeg-8.1.2.tar.xz) 构建精简音频组件，采用 **LGPL-2.1-or-later**，关闭 GPL、nonfree 和外部库自动探测。Windows x64 版通过 Windows Schannel 验证 HTTPS 证书与服务器身份；Linux x64 版启用 GnuTLS 并配合随包提供的 `certifi` CA 证书链验证 HTTPS。随包的 `_internal/ffmpeg/` 包含独立的 `ffmpeg.exe`（或 Linux 下的 `ffmpeg`）、完整 `LICENSE`、`README.txt`、`version.txt`、`buildconf.txt`、`license-notice.txt` 和 `SOURCE.json`，后者记录实际版本、构建配置与文件摘要。Playwright 自带的同名录制组件继续由 Playwright 管理，不能替代此解码器。
- **BGE small zh v1.5**：语义模型来自 [BAAI](https://huggingface.co/BAAI/bge-small-zh-v1.5)，使用 [Qdrant 的 ONNX 转换](https://huggingface.co/Qdrant/bge-small-zh-v1.5)。上游标注为 MIT，随包附有 [FlagEmbedding-MIT.txt](../assets/licenses/FlagEmbedding-MIT.txt)。
- **Zipformer 中文 ASR**：使用 [sherpa-onnx 的转换模型](https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-zh-int8-2025-06-30)，原始权重来自 [yuekai/icefall-asr-multi-zh-hans-zipformer-large](https://huggingface.co/yuekai/icefall-asr-multi-zh-hans-zipformer-large)。该转换模型页面未单独标注权重许可证，相关许可信息以原作者发布说明为准。
- **Python 与运行库**：包括 PySide6、Shiboken6、Pillow、NumPy、SoundCard、sherpa-onnx、FastEmbed、ONNX Runtime、Playwright 等。各组件保留原有版权，随包的许可文本位于 `licenses/` 及 `_internal/` 中相应组件目录。

发行包未修改上述浏览器与模型权重。FFmpeg 使用本项目提供的构建脚本从上游源码编译，通过独立进程调用；本项目原创代码的 MIT 许可与 FFmpeg 自身的许可分别保留。

与音频组件匹配的完整 FFmpeg 源码、配置和构建脚本随同版本发布在 [GitHub Releases](https://github.com/ys317/EchoSign/releases)，文件名为 `ffmpeg-8.1.2-hdusign-audio-source.tar.xz`（Windows）及 `ffmpeg-8.1.2-hdusign-audio-linux64-source.tar.xz`（Linux）。获取安装包时，可在同一发布页下载对应源码包，并用 `SHA256SUMS.txt` / `SHA256SUMS-linux-x64.txt` 核对；对应源码归档的摘要也记录在组件的 `SOURCE.json` 中。维护者应将二进制与对应源码一起保留、一起分发。应用不阻止用户替换此独立音频组件；替换后仍需提供兼容的音频接口与证书验证能力。

## Qt 桌面组件

HDUSign v1.9 使用未经修改的 **PySide6 / Shiboken6 6.11.2 与 Qt 6.11.2**，按 **LGPL-3.0** 使用其中的 Qt Core、Gui、Network、Qml、Quick、Quick Controls / Templates 等模块。只打包本应用使用的模块，不收集 Charts、Graphs、Quick3D 等无关附加组件。部分底层第三方代码遵守其自身许可证，随 Qt 的 notices 一并保留。

官方源代码：[Qt 6.11.2](https://download.qt.io/official_releases/qt/6.11/6.11.2/submodules/) · [Qt for Python 6.11.2](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.2-src/)；构建方法见 [Qt for Python 构建文档](https://doc.qt.io/qtforpython-6/building_from_source/index.html)。发行包 `licenses/` 保留原始 wheel 的许可文件；`assets/licenses/Qt-*` 补充来自对应 Qt 源码的完整 LGPL/GPL 文本与 Qt 声明。源码归档中同时保留各第三方组件的版权及许可说明。

应用以动态库方式调用 Qt。关闭软件后，用户可以用 ABI 兼容的修改版本替换 `_internal/PySide6/` 下的 Qt DLL、插件和 QML 文件；`_internal/hdusign/qml/` 为本应用的界面源码。替换时需同时保留相容的 PySide6 / Shiboken6 Python 扩展和运行库。应用不校验或阻止替换这些组件，不限制为调试此类修改而进行的逆向工程；本项目原创代码仍按 MIT 许可提供。重新分发时应保留这些声明和许可证，并按 LGPL 要求提供匹配版本的对应源码及获取方式。
