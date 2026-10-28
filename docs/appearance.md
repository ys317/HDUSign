# 预约排序与桌面透视玻璃 v2.3.3

2026-10-28 获准实施并生成 `HDUSign-v2.3.3-预约排序与玻璃界面.exe`，保留旧版及其界面目录。统一业务口径见[功能规则](功能规则.md)，实际版本记录见 [CHANGELOG](../CHANGELOG.md)。

## 使用与保存

- 账号列表中，有预约的账号优先，两组内部各按最近使用排列。预约底色略深，当前选中账号用边框区分。取消最后一门预约后回到普通组；列表排序不会取消预约或停止任务。
- 切换账号、成功登录、成功预约和开始监听会更新使用记录。后台刷新、读取课程及启动恢复不计为新的使用。记录保存在 `accounts.json` 的 `last_used` 字段，不改变密码及登录默认账号。
- 旧账号没有使用记录时保留原相对顺序，之后按新记录排序；未保存账号仅保留本次运行的顺序。写入失败会提示，本次仍能切换和继续任务。
- 设置页底部“全局玻璃外观”提供开关、背景透视程度、着色浓度、高光强度。滑块即时预览，点击“保存全局外观”后持久保存到便携目录 `appearance.json`，切换账号及重启保持。恢复默认后仍需保存。
- Windows 11 22H2 及以上支持原生桌面透视。文字与按钮保持不透明；仅背景参与透视。效果不可用时显示普通界面，也可以主动关闭。系统透明效果及后续 Windows 兼容性可能影响呈现。

这里实现桌面磨砂玻璃与边缘反光风格。模糊半径由系统决定，设置中的三个百分比不表示精确模糊半径，不包含实时透镜折射。

## 实现与体积

沿用现有 PySide6／Qt 和 Python `ctypes`，调用系统自带的窗口合成接口；不新增运行依赖、图片或桌面截图缓存。实际交付使用原生 Accent 合成路径：直接使用 DWM 系统材质时，Qt 标准边框在本机产生实色背景；无边框窗口结合 Accent Acrylic 在受控背景上实际生效。

无边框窗口保留标题栏拖动、双击最大化、八个边缘缩放区和最小化／最大化／关闭按钮。关闭仍经过原有任务退出流程。

`SetWindowCompositionAttribute` 不属于稳定的公开 Windows API 契约，因此仅在受支持环境中尝试，异常或接口失败时保留实色回退。未来 Windows 更新后的表现需重新核验，不能以一次成功保证所有系统及驱动环境。

用户允许本次新增占用在 100 MB 内。新 EXE 与旧版共存，新 QML 单独保存在 `_internal/qml-2.3.3/`；实际新 EXE 为 27.112 MiB，本次包括源码、界面和验证证据合计新增约 32.1 MiB；完整统计见本机 `build/release-2.3.3-verification.json`。

## 验证与限制

- 当前 Qt／核心回归 402 项，397 通过、5 项通用 FFmpeg 测试跳过。4 个旧 Tk 测试模块仍受环境缺少 tkinter 的限制。
- 原生界面使用两个临时账号及自建背景窗口验证：预约优先、预约底色、背景红／蓝变化实际透过窗口、透视滑块改变像素、深浅主题、全局设置保存恢复、关闭效果、标题栏按钮。窗口整体透明度始终为 1，文字不随背景褪色。
- `--check-glass` 只供离线验收，保存受控测试界面截图和报告，不加载真实账号，不请求学校或真实机器人。
- 新 EXE 的综合运行自检与五进程通知路由检查记录分别见 `build/release-2.3.3-runtime.json`、`build/release-2.3.3-worker-routes.json`；原生界面报告为 `build/release-2.3.3-native.json`。本机证据文件不保证随源码分发。
- 本次没有真实课堂或真实微信投递验收，机器人按监听时间判断冲突的规则未改动。

## 参考资料

- [Qt Window 透明窗口说明](https://doc.qt.io/qt-6/qml-qtquick-window.html) 与 [Windows 背景材质](https://learn.microsoft.com/en-us/windows/win32/api/dwmapi/ne-dwmapi-dwm_systembackdrop_type)。
- [Qt Liquid Glass Widgets](https://github.com/OliverZhaohaibin/Qt-liquid-glass-widgets)：参考材质分层及边缘高光思路。
- [PyQt Frameless Window](https://github.com/zhiyiYo/PyQt-Frameless-Window)：参考原生合成路径及接口使用。没有安装或打包该项目，也没有引入其依赖或组件代码。
