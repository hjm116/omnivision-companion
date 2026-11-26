# 变更记录（CHANGELOG）

> 历史记录来自原 VERSIONS.txt。

OmniVision Companion v0.15.3 · 开源发布候选版（收口 v0.15.2 复验剩余 2 项问题 + 开源文档）
====================================================================================

【v0.15.3 修复与发布收口】
  1. [安装器彻底修复] install.ps1 强制归一化绝对路径（DataDir/DshHome/VenvDir/PluginDir），改用 PS5.1 安全的 [pscustomobject] 写入 omnivision.config.json，并增加写入后存在性 + JSON + dataDir/python 强自检（失败即报错，绝不静默跳过）
  2. [瞬断静默重试] host.js Sidecar.call() 对 sidecar 重启窗口期的瞬时网络错误（fetch failed / ECONNREFUSED）增加 1 次静默重试；pollCompanion() 对前 2 次瞬时抖动静默重试不污染 lastError，且恢复后自动清空 lastError
  3. [开源文档齐备] 新增仓库根目录 README.md：含醒目反作弊警告（DirectInput 扫描码边界）、隐私与本地数据说明（WASAPI 内录 + 抓屏）、SenseVoiceSmall 可选下载说明（主包保持 ~24 MB 不膨胀）与自定义盘符/复用 venv 安装指南

------------------------------------------------------------------------------------
OmniVision Companion v0.15.2 · 实机反馈打磨版（响应 2026-11-26 实测 5 项改进）
==========================================================================

【v0.15.2 改进清单（基于 Windows 11 + DSH 0.2.2-rc.5 19 项实测报告）】
  1. [安装器加固] install.ps1 对 profile.bundles 做全量去重（兼容字符串/对象条目并清理历史重复项）
  2. [非默认盘符/复用 venv] install.ps1 新增 -VenvDir 参数，并同时把 dataDir 与 python 写入 omnivision.config.json（升级时保留旧 profile），彻底避免回退 PATH 的 python
  3. [面板显示同步] client.js 默认对齐 standard，拓宽 settings namespace 匹配，并在执行 /omnivision status/profile/mode 时自动从返回文本校准 pill 档位与模式；同步修复 host.js vision_understand 使用实时 runtime.profile
  4. [快反射安全默认] FastReflexEngine / companion_reflex / companion_mode / /omnivision preset 新增 dryRun 参数且默认 true（仅检测记录不真按键），需显式传 dryRun:false 或 live 才发送真实按键
  5. [中文转写准确率] install.ps1 在 -InstallCompanionExtras 时自动从国内镜像(hf-mirror)下载 SenseVoiceSmall int8 模型（加 -SkipSenseVoiceModel 可跳过）；audio.py 支持多目录自动发现 SenseVoiceSmall，且 faster-whisper 回退默认升级为 small + 中英技术词提示词（大幅减少「偷肯」等中文同音错字）

--------------------------------------------------------------------------
OmniVision Companion v0.15.1 · 实机验收版（修复全部 7 项审查缺陷）
=================================================================

【v0.15.1 修复清单（对比 v0.15.0）】
  1. [致命修复] plugin/host.js 修复 5 处字符串换行符转义损坏，并通过严格 JS 词法/括号平衡扫描（0 错误）
  2. [致命修复] companion/manager.py ocr_crop() 修正 OcrEngine.run(img) 返回 (els, meta) 二元组契约与 score 过滤
  3. [高危修复] companion/manager.py 所有伴侣 HTTP 路由捕获预期异常，统一返回 HTTP 200 {ok:false, error}，兼容 host.js allowNotOk
  4. [高危修复] companion/driver.py 消除 dry_run_override 改写共享状态的并发竞态，并在 mouse_click 期间补充急停检查
  5. [高危修复] companion/capture.py 为主动抓帧(ocr_crop/contact_sheet)开启 allow_fallback_on_none=True 防止静态屏幕 DXGI 误报，并钳制负坐标
  6. [中危修复] plugin/host.js renderStatus() 补充伴侣状态行、pollCompanion() 增加防重入锁、新增 /omnivision reset 急停复位命令
  7. [中危修复] install.ps1 修复 PowerShell 5.1 下重复拷贝 presets 嵌套目录问题，并改为逐个容错安装可选伴侣依赖（保持 UTF-8 BOM 防止 PS5.1 中文乱码）

-----------------------------------------------------------------
OmniVision Companion v0.15.0 · 合并升级版（基于 v0.14.0）
======================================================

【v0.15.0 新增与修复摘要】
  - 修复 host.js 缺失 writeFileSync / statSync 导入导致的配置写回与代码过期检测静默失效
  - 新增 sidecar/companion/ 全感官引擎（适配 DeepSeek-V4.1-Flash 原生多模态，不再强制经由千问/全屏 OCR 转译）：
      * capture.py : DXGI (dxcam) 抓屏 + 64x40 静帧跳过 + 2x2 时间戳宫格图 (1280x720)
      * audio.py   : 免虚拟声卡 Windows WASAPI Loopback + 本地 ASR 环形缓冲（可选依赖优雅降级）
      * driver.py  : DirectInput Set-1 硬件扫描码驱动 + 3D 相对视角 + 前台焦点锁 + 5s/30s 硬上限 + 全局 F12 急停
      * reflex.py  : 本地 20-60 FPS 快反射规则引擎（加载 presets/games/*.json）
      * manager.py : 四模式状态机 (passive / watch / game_turn / game_action) + 每小时帧预算熔断 + Barge-in + 局部放大 ocr_crop
  - 新增 5 个 DSH 工具：companion_mode / companion_watch_frame / companion_game_act / companion_reflex / vision_ocr_crop
  - 升级 client.js 悬浮面板：四模式一键切换 + 窗口白名单锁定 + 红色一键急停 + 鼠标穿透弹幕层（自动过滤 [SILENT]）
  - 新增 tests/ 跨平台自动化冒烟测试套件与 CLAUDE-TEST-GUIDE.md 验收手册

------------------------------------------------------
以下为 v0.14.0 原始基线记录：

OmniVision v0.14.0 · 开发与测试环境
====================================

【开发端环境】（本项目的所有实测数据都出自这台机器）
  OS          Microsoft Windows 11 专业版 10.0.26200
  DSH         0.2.2-rc.5
  Node        v24.21.0（DSH 自带 runtime）
  pnpm        11.28.5（DSH 自带 runtime）
  Python      3.12.14（系统安装，非 DSH 自带）
  GPU         NVIDIA GeForce RTX 5070 Ti 16GB（驱动 617.42）
  屏幕        2560 × 1440 @ 100%

【插件运行依赖】（安装器自动安装；版本随源更新，这里记录实测版本）
  OmniVision 插件        0.8.2（发行包版本，与压缩包名一致）
  onnxruntime           1.32.0
  rapidocr-onnxruntime  1.4.4
  numpy                 2.3.5
  pillow                12.3.0
  opencv-python         5.1.0.95
  uiautomation          2.0.29（固定，别升级）
  ONNX 线程预算         默认 4（12 核）/ 3（4 核）；OMNIVISION_ONNX_THREADS 可覆盖
  性能档位              energy / standard（默认）/ performance；可热切换（约 1 秒，不重启 DSH）
  设置页入口不可用        DSH 0.2.2-rc.5 内置插件页不渲染第三方 Config；档位改用 面板 / vision_profile / /omnivision 命令

【模型版本】
  OmniParser icon_detect ONNX    11.6 MB（fp32，随轻量包）
  RapidOCR PP-OCRv4              det / rec / cls 三件（随轻量包）
  Qwen2.5-VL-3B-Instruct        Q4_K_M + mmproj Q8（2.66 GB，完整版）
  llama.cpp                     Vulkan b12633（105 MB，完整版）

【实测基线】（本机数据，供对照）
  整屏 OCR              约 1–2 s（CPU）
  图标层                约 100–150 ms
  本地语义（完整版）    模型 1 s + OCR 2 s ≈ 2–3 s/帧
  云端语义（可选）      6–12 s（含服务器排队）
  缓存命中              40–50 ms

【排查时请先看】
  1. DSH 版本是否 ≥ 0.2.2
  2. Python 是否 3.10–3.12
  3. 跑一次 vision_status，把输出贴给发你插件的人
  4. 版本对不上时，以上表为基准对照

【卸载】
  删掉数据目录，再从 profile 的 package.json 里移除 @local/dsh-omnivision（或让 DSH 插件管理器移除）

【版本号说明】
  插件代码版本与发行包版本必须一致（v0.14.0）。
  发现 package.json 里的 version 与压缩包名不符时，以压缩包名为准。
