# OmniVision Companion v0.15.3 — Claude 测试与审计指南

本压缩包由原 `omnivision-light-v0.14.0.zip` 与 `v0.15.3` 全感官桌面伴侣升级模块完整合并而成，保留 100% 原模型权重与既有工具，并面向 **DeepSeek-V4.1-Flash 原生多模态** 重构了陪追剧与单机游戏代打能力。

## 1. 快速离线自动化测试（任意 OS / 沙盒均可运行）

解压后在项目根目录执行：

```bash
python3 tests/test_companion_smoke.py
```

若环境有 Node.js（≥ 18），同时执行 JS 语法与静态检查：

```bash
node tests/test_host_syntax.mjs
```

### 离线测试覆盖的核心断言
1. `py_compile` 检查 `plugin/sidecar/omnivisiond.py` 与 `plugin/sidecar/companion/*.py` 全部通过。
2. `VisionSensor`：验证 64×40 静帧过滤、`staticSkips` 计数，以及 2×2 时间戳宫格图准确输出 `1280×720` JPEG。
3. `SystemAudioListener`：验证缺失可选依赖（`pyaudiowpatch`/`sherpa_onnx`）时不崩溃、优雅降级，以及台词缓冲与格式化输出正常。
4. `SafeGameDriver`（`dry_run=True`）：
   - 验证单键长按超过 `5000ms` 时被硬截断（`clamped=True`, `durationMs=5000`）。
   - 验证 `sequence` 超过 20 步时直接拒绝执行。
   - 验证前台窗口变更时触发 `WINDOW_FOCUS_LOST` 并自动释放全部按键。
   - 验证触发 `emergency_stop` 后拒绝所有后续输入，直到显式调用 `reset_stop`。
5. `FastReflexEngine`：加载 `presets/games/generic_hp_potion.json`，输入合成低血量图像验证自动触发 `q` 喝药，紧接下一帧验证冷却锁生效不重复触发。
6. `CompanionManager`：验证达到 `maxFramesPerHour` 上限后触发预算熔断（`budgetSkips >= 1`），以及 `barge_in` 暂停/恢复状态切换。
7. `host.js` & `client.js`：验证 `writeFileSync`/`statSync` 导入修复、`isSilentReply("[SILENT]") === true` 以及所有新工具与斜杠命令完整注册。

## 2. 建议请 Claude 重点 Code Review 的清单

- **向后兼容性**：检查 `omnivisiond.py` 原有 `/look`、`/understand`、`/ui_tree`、`/act`、`/watch/*`、`/profile`、`/health` 路由是否零破坏。
- **人设解耦协议**：检查 `plugin/prompts/watch_protocol.md` 和 `plugin/prompts/game_protocol.md` 是否未硬编码人格，并确认 `[SILENT]` 在前端弹幕层 (`client.js`) 与后端 (`host.js`) 均被过滤。
- **反作弊与系统安全边界**：确认 `SafeGameDriver` 无法绕过前台窗口锁、5 秒单键上限、30 秒连招上限与 `F12` 全局急停。
- **依赖轻量化**：确认基础模式只需原包依赖（`onnxruntime`, `rapidocr_onnxruntime`, `numpy`, `pillow`），`dxcam` / `pyaudiowpatch` / `sherpa-onnx` 均为按需可选依赖。

## 3. Windows 11 + DSH 实机联调步骤

```powershell
# 基础安装（沿用原依赖）
powershell -ExecutionPolicy Bypass -File install.ps1

# 如需完整追剧音频内录 + DXGI 高速抓帧，加 -InstallCompanionExtras
powershell -ExecutionPolicy Bypass -File install.ps1 -InstallCompanionExtras
```

完全退出 DSH（含系统托盘）重新启动后依次验证：
1. 对话输入 `用 vision_status 看状态`，确认返回 `companion` 状态块且无 `codeStale` 警告。
2. 播放一段带人声对白的视频，点击右下角悬浮胶囊 → 填入播放器窗口关键词 → 点「陪追剧」，验证 2×2 宫格图与 `[SILENT]` 低打扰机制。
3. 打开窗口化单机游戏，切到「慢代打」测试一次 `companion_game_act`（可先传 `dryRun: true` 验证参数），按键盘 `F12` 或点面板红色「一键急停」确认立即松键并切回 `passive`。
