# 开发指南

Windows x64，Python 3.13。

## 本地运行

桌面使用 Qt Quick 课程工作区，默认由 `AccountManager` 管理多账号。每个账号的 `Controller` 独立维护课程、预约、转写与设置，后台服务通过 `account_worker.py` 启动独立进程。账号凭据、直播会话、签到浏览器和日志使用 `runtime.data_root()`；程序、模型和界面资源仍由 `application_root()`／`resource_root()` 定位。源码工作进程与签到子进程通过绝对路径入口启动，避免账号目录作为工作目录时找不到包。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\pythonw.exe -m hdusign
```

将发行包中的 `models/` 复制到项目根目录，即可使用本地识别模型。个人配置沿用根目录中的 `config.yaml` 等文件，不应提交到仓库。

图形界面与命令行共用 `python -m hdusign` 入口：

```powershell
python -m hdusign --help
python -m hdusign devices
python -m hdusign run --config config.yaml
python -m hdusign test --help
python -m hdusign code
```

`demo` 用于检查匹配规则，`--login` 打开登录浏览器。`test` 与 `run` 使用配置中的通知和签到选项；离线调试时应关闭 `auto_sign.enabled` 并清空 Webhook。

## 构建

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller==6.22.2
.\.venv\Scripts\python.exe tools/release.py build --ffmpeg-bundle build/ffmpeg-audio/ffmpeg-8.1.2-hdusign-audio-win64.zip
```

构建工具准备匹配的浏览器和语义模型，在 `build/releases/<版本>/` 生成独立目录，再打包到 `dist/releases/<版本>/`。语音模型从项目根目录的 `models/` 读取。首次准备缺失依赖时需要联网。

音频组件由 `.github/workflows/ffmpeg-audio-windows.yml` 使用 `tools/ffmpeg-build/` 中的脚本构建。先下载并解压该流水线产物，将音频 ZIP 及其同名 `-source.tar.xz` 对应源码归档放在同一目录，再传入 `--ffmpeg-bundle`。构建工具核对二进制、对应源码及构建记录，执行离线音频检查，并将对应源码一并放入最终发布目录；发布时上传安装包、对应源码和校验文件。

版本号统一维护在 `hdusign/__init__.py`，Windows 版本资源由构建工具自动生成。发布前先定稿代码、文档和图片，再构建；工具会校验源码摘要，避免上传与代码不一致的包。

维护者提交代码并推送对应版本标签后，可运行 `python tools/release.py publish --notes <发布说明.md>`，将已构建的 ZIP 发布到 GitHub `origin` 仓库。

## 目录

| 目录 | 内容 |
| --- | --- |
| `hdusign/` | 应用代码，按职责划分模块 |
| `tests/` | 自动化测试与音频样本 |
| `tools/` | `release.py` 构建与发布；`assets.py` 资源维护；`HDUSign.spec` 打包配置 |
| `assets/`、`docs/` | 图标、截图与文档 |

应用内部由 `audio.py` 负责采集和转写，`rules.py` 负责匹配与提码，`monitor.py` 连接识别流程；`attendance.py` 管理签到任务与结果，`browser.py` 负责网页操作。`desktop_controller.py` 在线程间传递事件并维护增量数据模型，`qt_app.py` 启动 Qt Quick，`qml/` 维护主题和控件，`runtime.py` 处理源码与便携版的资源路径。

`live.py` 只通过杭电已验证的直播接口读取单节 HTTP(S)-FLV 播放信息，使用独立的直播登录态；`media.py` 通过隐藏的 FFmpeg 子进程输出 16 kHz 单声道音频，不解码画面。直播输入按网络速度读取，不使用 `-re` 限速：限速不能平滑断续后的补发突发，却会在服务器时间戳跳变时一直等待到空闲超时。约 10 秒的有界队列吸收补发突发和识别器的短暂停顿，识别器通常快于实时，积压会在数秒内消化。队列仍然填满时丢弃整段积压、从当前直播声音继续，并在下一块音频前报告跳过的时长；监控层据此重置识别器与未确认的候选码，不断开连接，也不把它计入故障窗口，断点两侧的数字不会拼成签到码。断流不冲刷未完成的识别结果。媒体请求只携带取流令牌和必要的公开请求头，不转发站点 Cookie/JWT；HTTPS 验证证书，暂不接受 HLS。

同一节直播的 API 与媒体临时故障进入相同恢复流程，按 2/5/10/20/30 秒退避，后续间隔上限 30 秒。每次连续故障的恢复窗口为 120 秒；只在同一连接同时接收至少 30 秒音频且持续至少 30 秒后复位，短暂连接或突发缓冲不延长故障窗口。登录过期、无权限、明确结束以及组件、格式、证书或清理失败不重试；媒体授权失败重新解析一次令牌，再次失败则要求重新登录。等待使用可取消事件，API 保持有界连接与读取超时，返回后再次检查停止信号。界面在重新连接期间清除未完成转写并显示黄色状态，保留已确认签到码。

稳定性回归使用虚拟单调时钟，覆盖初次和恢复期间 API 失败、退避期限、健康后复位、短暂连接不复位、身份失效、媒体错误分类、多个取消时机、积压跳过后不重连且重置识别，以及断点前后的数字不能拼成签到码。连堂课由 `lessons.py` 根据可靠元数据生成计划，切换链接时重新取流并清空未确认转写，保留当前任务已确认签到码；详见[连堂课](continuous-lessons.md)。[早期方案](live-continuation-plan.md)仅保留接口调查背景。

源码使用后台音频需在 PATH 中提供 FFmpeg；便携包内置经过核验的精简音频组件。内置组件通过 Windows Schannel 验证服务器身份与证书信任，源码使用的其他 FFmpeg 后端沿用 certifi 的 CA 文件。HTTP 输入禁止未经验证地跳转到 HTTPS；网络输入继续限制为 HTTP(S)-FLV。组件的许可证、构建过程和对应源码见[第三方声明](third-party.md)。离线自检不依赖系统 PATH 或外部媒体服务。

界面使用 PySide6 6.11.2 / Qt Quick，分为「直播 / 设置」。`desktop_controller.py` 保留原配置结构和服务调用，后台线程接收配置快照，只有主线程更新模型。每个状态字段分别通知 QML；计时、转写与签到码不使整页状态绑定同时失效。活动记录折叠时不创建列表，展开后使用 `ListView.reuseItems` 复用行；详细日志最多保留 1500 条，签到事件保留 20 条。每批最多消费 160 行，中间转写合并为最后一条；队列积压时 16 ms 后继续，运行时 33 ms、空闲时 100 ms 检查。GPU 后端由 Qt 自动选择，Windows 本机验证为 Direct3D 11。

直播登录后读取今明课程，并结合“我的课表”核验连堂课。预约保存在各账号的 `scheduled_courses` 列表，旧 `scheduled_course` 自动迁移；查看课程与预约队列分离，启动时使用预约保存的地址、课表和通知快照。每账号同一时间监听一门，最多五个账号并行。停止与关闭通过取消事件通知工作进程；收到完成事件后再释放通知路由，避免旧任务推送未结束就复用机器人。

旧 `gui.py` / `ui.py` 暂时保留为迁移行为回归基线，默认入口与发行包不加载它们。运行全部历史测试需安装 `requirements-dev.txt`；应用只需要 `requirements.txt`。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe tools/assets.py screenshots --scale 1.5
.\.venv\Scripts\python.exe tools/assets.py screenshots --preview
.\.venv\Scripts\python.exe tools/profile_desktop.py
```

截图与压力测试使用临时演示配置，不读取个人账号，也不调用登录、音频、签到或通知服务。`profile_desktop.py` 每 33 ms 注入 160 条模拟转写/日志，同时通过真实 Qt 鼠标事件切换聚焦。它报告队列处理、输入分发与事件循环间隔，不把回调耗时等同于屏幕帧率。首轮本机 Direct3D 11 测试中，展开记录时队列处理 p95 约 2.75 ms、输入分发 p95 约 9.15 ms，无剩余队列；结果不代表实际 ASR、浏览器同时运行或不同设备上的帧率保证。

打包钩子只收集实际使用的 Qt QML / Quick / Basic Controls 模块；Qt DLL 与 QML 保持为可替换文件。发行包自检会加载并渲染深浅主题，再验证模型、浏览器和 FFmpeg。对应的使用模块、许可证和替换说明见[第三方声明](third-party.md)。

`location.py` 在用户点击按钮时，通过 Windows 自带 PowerShell 调用系统定位，返回经纬度及可用精度；不使用 IP 定位，也不反查街道地址。请求在工作线程运行，有 20 秒总超时，结果在主线程填入表单，失败保留原值。`processes.py` 统一后台命令的 Windows 无控制台启动参数；源码图形界面使用 `pythonw.exe` 启动，便携版以无控制台方式打包。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试使用临时数据，不连接学校服务、发送通知或录制课堂声音。

## 回放评估

实时 ASR 默认 `asr.num_threads: 1`。在 i5-13420H（12 逻辑处理器）上，用相同 60 秒本地录音按 0.25 秒真实节拍测量，1/2/4 线程分别消耗约 13.73/167.05/452.31 CPU 秒，折算 ASR 进程平均整机占用约 1.90%/23.17%/62.72%。单线程块处理 p95 为 118.6 ms，最大 144.8 ms，均低于 250 ms 输入节拍。该对照仅覆盖本机、这段音频及默认模型，未包含网页、采集和内存开销；多线程额外消耗符合实时块间线程池空转的特征，尚未做底层性能追踪。保持模型与分块不变，先使用单线程；调整线程数应按真实节拍验证占用和积压，不能只比较离线吞吐。

本机 HTTP-FLV 服务到 FFmpeg、单线程 ASR 的 60 秒完整链路测试中，合计占用约 2.56%，观测队列最多 0.25 秒，没有持续积压。随后使用正在进行的杭电 HTTPS-FLV 直播，手动换账号、关闭登录窗口后接收并识别了 30 秒音频，取声和识别合计平均占用约 3.19%，分进程峰值工作集约为 309.2/30.0 MiB，停止后没有遗留解码进程或读取线程。结果不包含 GUI、签到浏览器或整节课的长时间运行，不能据此保证其他机器、网络和课程的表现。

监码支持确认后的中间结果和完整结果两条路径。默认 `code_watch.early_code: true`、`early_stable_seconds: 0.4`：同一位置的四位码至少经过两次实际解码，持续不少于 0.4 秒音频，且有非数字后续话语或完整重复报码，才可提前提交。`StreamingASR.revision` 只在实际解码时增加，重复返回缓存文本不增加确认次数；空白或改写后的结果打断候选稳定期。裸四位尾串、拆开的长数字、小数、单位和截短的重复串不提前提交。`feed(final=False)` 仍无副作用，提前确认统一经过 `feed_partial`。

设置 `early_code: false` 可退回只提交完整结果。回放结束时需先排空识别器的剩余音频，停止监控则不提交剩余转写。中间结果只使用强规则预备页面，不运行语义模型，也不提前发送普通签到提示；已确认的码仍按码去重，整句结束不重复提交同一码。

默认 `auto_sign.prewarm_browser: true`，系统声音与后台音频监控在初始化模型前调用 `AutoSigner.prepare()`；后续提示或候选码仍可调用同一幂等入口，不传入码。每次监控复用一个独立浏览器进程，父子进程以原子写入的 `request-{id}.json`、`result-{id}.json` 交换确认请求和结果；每次使用新文件，避免 Windows 下覆盖正在读取的请求。每轮结束重新打开空白键盘。页面预备失败允许确认码后冷启动；已发请求结果未知时不自动重试；取消时终止正在处理请求的进程树。

网页操作等待元素可用，不使用进入键盘后或每位数字后的固定停顿。签到结果仍只接受匹配当前码、请求方法和签到接口的响应。`timing-{id}.json` 单独记录确认码、页面就绪、输入、点击和响应时间；活动记录显示确认码到页面就绪、点击签到的耗时。计时信息缺失或损坏不改变已确认的签到结果。

`tools/replay_metrics.py` 提供独立于模型和账号的评分函数。每段音频应单独标注是否存在点名过程、明确的四位参考码，以及确认属于普通授课的时间段。`true_codes` 支持同一片段多个参考码，兼容旧的 `true_code`。未列出的码默认待核实，只有显式标为 `invalid_codes`、出现在已标注普通授课时段，或标注明确声明 `code_labels_complete: true` 时才判错。缺少标注或参考转写不清楚时保留未知，不能归为负样本。

评估同时报告是否检出全部已标有码、检出且输出均已核实的片段数、已确认错码、待核实码、已判定报码事件的准确率及判定覆盖率，以及已标注普通授课时段的错误提示和覆盖时长。参考码按整段标注匹配；点名前后的时间窗口只表示时间接近程度，不能当作真假标签，也不能否定同一课堂的另一轮签到。调节热词或语义模板后，应在独立课堂的普通授课时段上验证误报，再决定默认配置。

识别速度应分别从老师说出签到提示、念完完整签到码计时。平台创建签到的时间、音频解码的总耗时和音频块大小都不能代替这两项延迟。可在标注中增加 `speech_events`：每项包含 `kind`（`prompt` 或 `code`）、音频内的 `start`、`end`、`match_until`，码事件还需提供 `code`。`end` 是待识别提示或完整码的结束时间；`match_until` 明确该次事件的匹配范围，避免把后面无关的提示计作成功。重复念同一个码属于同次检测时，只标注第一次完整报码。

评分中的 `speech_latencies` 和汇总的 `prompt_latency`、`code_latency` 单独列出已标注事件、检出、漏检和延迟；错误码与此前已输出的码不能充当本次正确码的检出时间。没有语音时间标注时延迟保持未知。自动语音分段只有句段边界，包含重复报码或后续说话时，应注明实际测量的是整段结束后的等待，不能据此声称逐词延迟；负值保留供检查边界误差。正式比较识别延迟应使用经过核对的提示结束、首次完整报码结束时间，并保留漏检样本。

## 新增模块与离线验证

- `lessons.py`：连堂计划、监听窗口和可取消的时间边界。
- `accounts.py`、`multi_account.py`、`account_worker.py`：DPAPI 账号库、账号调度及独立工作进程。
- `reservation_webhooks.py`：每任务加密通知路由，损坏或缺失时停止推送。
- `diagnostics.py`：启动阶段音频、事件时间线与运行指标。
- `captcha_boost.py`：可关闭的轨迹增强；账号目录中的本地库不属于源码。

使用开发环境运行 `python -m pytest`。测试应使用临时账号、临时轨迹库、模拟服务与离线页面，禁止读写真实账号或向学校、机器人发请求。`tools/` 下部分人工检查脚本需要真实账号、模型或便携目录，不属于常规单元测试，不应无差别执行。

源码多账号测试可使用当前虚拟环境。贡献分支保留的 `tools/portable_tests.py` 与 `tools/build_continuous.py` 用于特定便携目录及已核验基础 EXE，普通源码开发不依赖这一路径。常规完整发行继续使用本页的 `tools/release.py` 流程。
