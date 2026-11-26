# OmniVision Companion (`@local/dsh-omnivision` v0.15.3)

面向 **DeepSeek Harness (DSH)** 与 **DeepSeek-V4.1-Flash 原生多模态** 的开源全感官桌面伴侣底座——让你的鲸鱼娘真正拥有**看屏、听剧、陪玩/单机代打、鼠标穿透弹幕**能力，同时 100% 保持你原有的人设与说话风格。

> 默认发行包仅约 **24 MB**（内置 PP-OCRv4 与 OmniParser 图标轻量模型，不含 228 MB 语音模型，按需可选下载）。

---

## ⚠️ 重要安全警告（反作弊与键鼠控制边界）

* **严禁用于含内核级反作弊的联机网游**：本插件的游戏操作模块（`companion_game_act` / `companion_reflex`）底层调用 Windows DirectInput Set-1 硬件扫描码（`SendInput`）与相对鼠标位移，**仅限单机游戏、视觉小说、模拟经营或明确允许自动化的本地场景**。对带反作弊保护的在线竞技/网游启用自动键鼠可能导致输入被屏蔽或账号封禁，后果由使用者自行承担。
* **四重防误触安全护栏（默认全部开启）**：
  1. **前台窗口白名单锁**：仅在锁定的目标窗口处于前台焦点时执行键鼠操作，切窗自动松开所有按键并中止。
  2. **硬编码时长与步数上限**：单键长按硬上限 `5000ms`，连招序列硬上限 `20 步 / 30000ms`。
  3. **快反射默认干跑（`dryRun: true`）**：本地 20–60 FPS 快反射引擎启动时默认只检测记录、不发送真实按键，确认规则无误后需显式传入 `dryRun: false` 或命令加 `live` 才真正按键。
  4. **全局一键急停（`F12`）**：按下键盘 `F12`、点击悬浮面板红色「一键急停」按钮或执行 `/omnivision stop`，立即松开所有按键并切回 `passive` 待机；恢复需显式执行 `/omnivision reset`。

---

## 🔒 隐私与本地数据说明

* **采集范围可控**：
  * **画面采集**（`dxcam` DXGI / PIL 回退）仅在你主动调用工具或开启伴侣模式时抓取，强烈建议在面板中填写**窗口标题关键词**（如 `PotPlayer`、`Edge` 或游戏名），切出该窗口时自动停止采集与操作。
  * **系统音频内录**（Windows WASAPI Loopback）仅在 `watch`（陪追剧）模式下运行，只监听系统扬声器输出，不调用麦克风，且音频切片仅在内存中进行本地离线识别（`sherpa-onnx` SenseVoiceSmall 或 `faster-whisper`），**不上传原始音频文件**。
* **数据流向透明**：
  * 本地 OCR、静帧差分（64×40）、系统音频转写均在你的个人电脑本地完成。
  * 仅在命中画面变化或新台词时，按你设定的每小时预算上限（默认 120 帧/小时）将 2×2 宫格图路径与转写文本注入当前 DSH 会话，交由你自己配置的模型处理。
  * 所有本地截图默认落在 `<DataDir>\frames`，可随时手动清空。

---

## ✨ 核心能力一览

* **陪追剧 / 看番（`watch` 模式）**：
  * **2×2 时间戳宫格图**（1280×720）：将过去 14 秒内变化最大的 4 帧拼成 1 张图，Token 成本直降 75%，静止画面零发图。
  * **免虚拟声卡听觉流**：Windows 原生 WASAPI Loopback + 本地 **SenseVoiceSmall int8** 实时中/英/日/粤转写（可读性接近字幕级别）。
  * **人设解耦 + `[SILENT]` 静默协议**：不写死任何性格；平淡片段自动回复 `[SILENT]` 跳过播报，不吵不剧透。
* **单机游戏代打 / 陪玩（`game_turn` / `game_action` 模式）**：
  * **慢节奏游戏（回合/卡牌/Galgame）**：DeepSeek-V4.1-Flash 原生直看高清帧 + 按需 `vision_ocr_crop` 2× 放大核对极小数值，告别全屏 OCR 幻视。
  * **动作游戏快慢双系统**：慢系统负责高层策略，本地 `FastReflexEngine`（20–60 FPS）加载 `presets/games/*.json` 处理低血喝药/闪避等毫秒级反射。
* **100% 向后兼容 `v0.14` 视觉层**：完整保留 `vision_look`、`vision_understand`、`ui_tree`、`vision_action`、`vision_watch`、`vision_profile`、`vision_status`。

---

## 🚀 5 分钟安装与升级（Windows 11 x64 + DSH ≥ 0.2.2）

解压本压缩包后，在目录内打开 PowerShell：

### 1. 标准轻量安装（默认约 24 MB，不含语音模型）
```powershell
powershell -ExecutionPolicy Bypass -File install.ps1 -Profile desktop
```

### 2. 完整伴侣安装（按需安装 `dxcam` / `pyaudiowpatch` / `sherpa-onnx` 并自动从国内镜像下载 ~228 MB SenseVoiceSmall 中文语音模型）
```powershell
powershell -ExecutionPolicy Bypass -File install.ps1 -Profile desktop -InstallCompanionExtras
```
> 如只需装 Python 扩展但不想自动下载 228 MB 语音模型，可追加 `-SkipSenseVoiceModel`。

### 3. 自定义数据盘（不占 C 盘）与复用已有 venv
```powershell
powershell -ExecutionPolicy Bypass -File install.ps1 `
  -Profile desktop `
  -DataDir "F:\dsh-data\omnivision-v15\data" `
  -PluginDir "F:\dsh-data\omnivision-v15\plugin" `
  -VenvDir "F:\dsh-data\omnivision\venv" `
  -InstallCompanionExtras
```
安装器会自动写入并校验 `omnivision.config.json`（包含 `dataDir` 与 `python` 绝对路径），并对 DSH profile 的 `bundles` 自动去重。

### 4. 完全重启 DSH 验证
**连系统托盘图标一起完全退出 DSH 后重新打开**，在对话中发送：
> `用 vision_status 看看视觉层状态`

看到输出中包含 `伴侣底座：模式 passive｜...` 且右下角出现悬浮胶囊即可开始使用。

---

---

## 🩺 安装失败排查（按报错对号入座）

| 现象 | 原因 | 处理 |
|---|---|---|
| `profile 的 package.json 写入自检失败…已恢复备份` | 写入被拦截 / 磁盘满 / 杀软 | 看 `.omnivision-bak` 是否已恢复；确认磁盘空间后重跑 |
| `pnpm install 失败` | 网络 / 镜像 | 检查网络重跑；或手动在 `<DshHome>\profiles\desktop` 跑一遍 `node pnpm.cjs install` |
| `找不到 DSH 目录` | DSH 未启动过，或路径不对 | 先打开一次 DSH；或用 `-DshHome` 指定 |
| `找不到 DSH 自带的 node/pnpm` | DSH 版本太老（< 0.2.2） | 升级 DSH |
| `找不到 Python 3.10–3.12` | 未装 Python | 装 3.10/3.11/3.12（勾选 Add to PATH），或用 `-Python` 指定 |
| `SenseVoiceSmall 未能自动下载` | 网络 / 镜像不可达 | 不影响基础功能；可 `-SkipSenseVoiceModel`，或手动放模型（见下） |
| `9882 / 9883 端口被占用` | 其他程序占用 | 释放端口，或改配置 |
| 装完工具没出现 | 未完全重启 DSH | 连系统托盘一起退出再打开 |

**手动放 SenseVoice 模型**（离线 / 下载失败时）：

```
<DataDir>\models\sense-voice\model.int8.onnx   ← 约 228 MB
<DataDir>\models\sense-voice\tokens.txt        ← 约 0.3 MB
```

- 国内镜像（推荐）：
  - `https://hf-mirror.com/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/model.int8.onnx`
  - `https://hf-mirror.com/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/tokens.txt`
- 海外：把 `hf-mirror.com` 换成 `huggingface.co`，路径相同。

## 🧪 自测（tests/）

不需要 Windows 或 GPU（跨平台离线冒烟），改完代码先跑这个：

```bash
# Python 侧：companion 逻辑 + 安全护栏 + OCR 契约 + 音频降级
python tests/test_companion_smoke.py

# JS 侧：host.js / client.js 词法 + 结构完整性
node tests/test_host_syntax.mjs
```

全 PASS 再提 PR。这两个脚本也是 CI 的入口（可加 GitHub Actions）。

## 🎮 常用斜杠命令与工具速查

| 命令 / 工具 | 说明 |
| :--- | :--- |
| `/omnivision mode watch <窗口关键词>` | 开启陪追剧模式（2×2 宫格 + WASAPI 音频转写） |
| `/omnivision mode game_turn <窗口关键词>` | 开启慢节奏单机游戏代打（回合/卡牌/Galgame） |
| `/omnivision mode game_action <窗口关键词>` | 开启快慢双系统动作游戏模式 |
| `/omnivision mode passive` | 切回待机，停止采集与音频监听并松开按键 |
| `/omnivision stop`（或键盘 `F12`） | 全局一键急停，立即释放所有按键并锁定驱动 |
| `/omnivision reset` | 解除急停锁定，允许恢复游戏控制 |
| `/omnivision preset list` | 查看 `presets/games/` 下的社区游戏预设 |
| `/omnivision preset <id>` | 启动快反射预设（**默认 dryRun 仅记录**，末尾加 `live` 才真按键） |
| `/omnivision bargein pause 20` | 插话优先：暂停自动推帧 20 秒 |
| `vision_ocr_crop` | 对指定矩形 `[x,y,w,h]` 放大 2 倍局部跑 RapidOCR（专治小字/血条数字） |

---

## 🧩 社区共建

欢迎提交 PR 补充：
* `presets/games/*.json`：各类单机游戏的 ROI 快反射规则模板
* `presets/tts/*.json`：Edge-TTS / GPT-SoVITS / Fish-Speech / CosyVoice 语音后端配置模板

## 卸载
1. 删除你的数据目录（默认 `~/.dsh/omnivision` 或你指定的 `-DataDir` / `-PluginDir`）；
2. 在 DSH 插件管理器中移除 `@local/dsh-omnivision`（或从 `profiles/<profile>/package.json` 的 `dependencies` 与 `dsh.profile.bundles` 中删去该条目）。

---

## 📄 License

本项目自有代码采用 **MIT License**（见 [LICENSE](LICENSE)）。

第三方依赖与模型（RapidOCR / ONNX Runtime / uiautomation / OpenCV / Pillow / NumPy / sherpa-onnx / dxcam / PyAudioWPatch / PP-OCRv4 / OmniParser / SenseVoiceSmall）各自遵循其原始许可，完整清单见 [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)。

> 架构思路参考：[dsh-screen-attention](https://github.com/EXarchive/dsh-screen-attention)、[screen-use](https://github.com/tongriyaotxt/screen-use)、[dsh-dsgirl](https://github.com/xiazhiyehuo/dsh-dsgirl)、[dsh-feiyu-commander](https://github.com/yuanstarstar/dsh-feiyu-commander)（均为思路借鉴，未链接其代码）。