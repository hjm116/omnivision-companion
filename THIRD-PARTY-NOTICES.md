# 第三方组件与许可（THIRD-PARTY NOTICES）

本项目的自有代码采用 **MIT License**（见根目录 `LICENSE`）。以下组件/模型为第三方作品，各自遵循其原始许可；本项目不修改、不打包其源代码，仅按许可使用。

---

## Python 依赖

| 组件 | 用途 | 许可 | 说明 |
|---|---|---|---|
| [RapidOCR](https://github.com/RapidAI/RapidOCR)（`rapidocr_onnxruntime`） | PP-OCRv4 文字识别 | Apache-2.0 | 本项目使用其 det/cls/rec 推理管线 |
| [ONNX Runtime](https://github.com/microsoft/onnxruntime)（`onnxruntime`） | 所有 ONNX 模型推理 | MIT | |
| [uiautomation](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows) | Windows UIA 结构树 | Apache-2.0 | |
| [OpenCV](https://github.com/opencv/opencv-python)（`cv2`） | 图像处理 | Apache-2.0 | |
| [Pillow](https://github.com/python-pillow/Pillow)（`PIL`） | 截图/图像编码 | HPND（历史 MIT 系） | |
| [NumPy](https://github.com/numpy/numpy) | 数值计算 | BSD-3-Clause | |
| [PyYAML](https://github.com/yaml/pyyaml)（`yaml`） | 配置解析 | MIT | |
| [dxcam](https://github.com/ra1nty/DXcam)（可选） | DXGI 高速抓屏 | MIT | `-InstallCompanionExtras` 按需安装 |
| [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch)（可选） | WASAPI 回环内录 | MIT | 同上 |
| [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)（可选） | 本地语音转写 | Apache-2.0 | 同上 |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper)（可选兜底） | 本地语音转写备选 | MIT | 同上 |

## 模型权重

| 模型 | 用途 | 许可 | 来源 |
|---|---|---|---|
| PP-OCRv4（det / rec / cls） | 文字识别 | Apache-2.0 | [RapidOCR models](https://github.com/RapidAI/RapidOCR) |
| OmniParser `icon_detect` | UI 图标检测 | MIT | [microsoft/OmniParser](https://github.com/microsoft/OmniParser) |
| SenseVoiceSmall（int8） | 中文/多语语音转写 | Apache-2.0 | [k2-fsa/sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)（模型分发） |

**模型权重不打包进主发布包**（主包 ~24MB），由 `install.ps1 -InstallCompanionExtras` 按需下载（`-SkipSenseVoiceModel` 可跳过）。

## 架构参考（仅思路，无代码）

- [dsh-screen-attention](https://github.com/EXarchive/dsh-screen-attention)：DirectML 推理后端选择、硬编码扫描码边界等设计思路（AGPL-3.0，本项目**未链接其代码**，仅学习架构）
- [screen-use](https://github.com/tongriyaotxt/screen-use)：策略链（UIA → 视觉兜底）思路
- [dsh-dsgirl](https://github.com/xiazhiyehuo/dsh-dsgirl)：`hear` 的「人声/音乐/环境音」语义判断思路
- [dsh-feiyu-commander](https://github.com/yuanstarstar/dsh-feiyu-commander)：SenseVoice 模型复用路径思路

## 声明

- 本项目仅用于**单机/离线游戏与允许自动化的场景**；严禁用于含内核级反作弊的联机游戏。
- 本项目不包含任何第三方 API Key；语音合成等服务需用户自备凭据。
