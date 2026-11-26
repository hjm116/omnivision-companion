# 通用视觉层（OmniVision）

把「看屏幕」这件事从「截一张图丢给模型」变成「一张带坐标的元素清单」。

## 做什么

`vision_look` 截一帧，返回：

- **场景**：`battle / menu / dialog / unknown`（按画面文字关键词猜，可 `sceneHint` 手动指定）
- **文字元素清单**：每个元素带 `id`、`box = x,y,w,h`、`center`（屏幕绝对坐标）、识别置信度、`interactive` 提示
- **变化区域**：相对上一次调用变了的块（32px 网格），适合判断「有没有新东西」
- **截图路径**：需要真看图时再用 `read_image` 读它（不主动塞图，省 token）
- **缓存标记**：同一界面第二次调用 `cached: true`，不跑 OCR

## 用法

```text
vision_look                                     # 整屏看一眼（第一次约 2s，之后同界面 ~35ms）
vision_look{region:"foreground"}                # 只看前台窗口
vision_look{region:"smart"}                     # 【推荐】前台窗口，取不到回退全屏（窗口场景省 ~60%）
vision_look{region:"window:文明"}                # 按窗口标题找
vision_look{region:"0,0,1280,720"}              # 指定矩形
vision_look{ocr:false}                          # 只要变化区域，不跑 OCR（约 60ms）
vision_look{icons:true}                         # 额外补「无字图标」候选（本地 OpenCV 兜底）
vision_look{rect:[100,200,800,600], maxElements:20}
vision_status                                   # 环境/缓存/截图目录自检
```

实测（2560×1440，CPU）：整屏首帧 2.2s / 窗口首帧 0.5–0.9s / 缓存命中 33–45ms。

## 语义层：vision_understand

`vision_look` 坐标准、字不一定准（OCR 会把「这工具」读成「这工月」）。`vision_understand`
把「缩略截图 + 元素清单」交给多模态模型，拿回**修正后的文字 + 场景 + 一句话概括**。

### 三个后端（可插拔）

| 后端 | 是什么 | 要网/要 key | 说明 |
|---|---|---|---|
| **local**（默认） | 本地 llama.cpp + Qwen2.5-VL-3B GGUF | 否 | 零依赖、离线、0 成本；首次调用会自动拉起 llama-server |
| cloud | 百炼 qwen4-max | 要 key | 更快更准，可选加速器，不再是必需品 |
| off | 纯 OCR + 规则 | 否 | 最省，只做场景兜底，不修字 |

回退链：`local` 失败 → `cloud`（有 key 才试）→ `off`；`cloud` 无 key → `local` → `off`。
回执里带 `backend` 字段（`local|cloud|off`）和 `attempts`（每次回退的原因），可追溯。

环境变量：

```
DSH_OMNIVISION_BACKEND        local | cloud | off      （默认 local）
DSH_OMNIVISION_LOCAL_ENDPOINT http://127.0.0.1:9883/v1
DSH_OMNIVISION_LOCAL_MODEL    qwen2.5-vl-3b
DSH_OMNIVISION_LLAMA_SERVER   <数据目录>\llama\llama-server.exe
DSH_OMNIVISION_LLAMA_MODEL    ...\models\qwen2.5-vl-3b\Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf
DSH_OMNIVISION_LLAMA_MMPROJ   ...\models\qwen2.5-vl-3b\mmproj-Qwen2.5-VL-3B-Instruct-Q8_0.gguf
```

### 实测（2026-11-23，5070 Ti / Vulkan）

| 项 | 结果 |
|---|---|
| 纯 C++ 跑（无 torch/transformers） | ✅ `pip list` 里没有 torch，llama.cpp 只是 exe + dll |
| 显存 | 空闲 6.7 / 16.3 GB（模型增量约 3.4GB，和 GPT-SoVITS 共存没压力） |
| llama-server 启动 | 3.5–5s（只在第一次调用/重启后） |
| 单帧模型耗时 | 0.8–1.1s（热） |
| 整次 `vision_understand` | 首次 8.2s（含启动）、之后 2.2–3.1s（含 OCR）；缓存命中 1.2s |
| 修错字 | ✅ `这工月是干嘛的` → `这工具是干嘛的`；`Bilibil液莺Night` → `Bilibili 夜莺Night`；`霜见进度备忘见进度备qskpyt` → `霜见_进度备忘.md` |
| 场景 | 桌面帧判 `desktop`（启发式，不靠 3B——3B 会把桌面判成 browser） |

**3B 的边界**（踩过的坑，别再试）：
- 给它整屏缩略图问「纠正元素」→ 它把清单原样抄回来，或者编「网页内容」。
- 给它裁图网格（每行放大到 48px 高）问「每行读什么」→ 能读对。
- 它偶尔会把相邻两行的内容读串（e8 读成 e9 的字），所以只认**我们真的裁过并发送过**的 id，单字符元素不进裁图。

## 结构层：ui_tree（Windows UIA，纯本机）

`vision_look` 看像素，`ui_tree` 读**结构**：控件名、角色、坐标、`enabled`、`selected`、焦点。
零模型、零网络、约 50–300ms。

```text
ui_tree                                    # 前台窗口，默认 depth 12 / filter interactive / 预算 300ms
ui_tree{region:"window:设置"}              # 指定窗口
ui_tree{filter:"all", depth:8}             # 全部节点、浅层
ui_tree{withOcr:true}                      # UIA 元素 + OCR 补 UIA 没有的文字（多花 1–2s）
```

### 实测（2026-11-23，uiautomation **2.0.29** 钉死）

| 窗口 | 节点 | 可交互 | 耗时 | 备注 |
|---|---|---|---|---|
| Edge 测试页（depth 12） | 344 | 37 | 301ms | 网页控件全在，**禁用按钮 `enabled=false`** |
| 记事本 | 65 | 21 | 71ms | 「最大化」也是 false（窗口已最大化） |
| 设置 | 186 | 50 | 180ms | 部分虚拟化项坐标为 0,0,0,0 → 已过滤 |
| 任务栏 | 47 | 15 | 48ms | 时钟/音量/网络都有名字 |
| dsh-pet | 23 | 0 | 23ms | **无树 → 自动回退 OCR，`source:"ocr"`** |

### 几条硬规则（都是踩出来的）

- **客户端必须是 `uiautomation`，不是 pywinauto 的 uia 后端**：同一个 Edge 窗口，pywinauto 只读到 48 个节点
  （浏览器外壳，网页内容为空），uiautomation 读到 291–344 个（含 Document 和网页控件）。这条差点让人误判「浏览器 UIA 没戏」。
- 版本钉死 `uiautomation==2.0.29`，别用最新版冒险（探针验证过的就是它）。
- `selected` 读不到时是 **null（未知）**，不是 false。网页里的「选项 B」是 TextControl，没有 SelectionItem 模式，
  选中态得问视觉/云端；原生 ListItem/TabItem 有（实测 TabItem=选中）。
- 300ms 是**预算不是硬等**：超了就返回已读到的部分并标 `truncated:true`，线程有硬上限，绝不卡死请求。
- 深度按窗口自适应：Edge/Chromium 用 12；浅层树 8 更快；默认 12。
- 无树应用（可交互=0）→ 自动回退 OCR，回执标 `source:"ocr"` 和 `fallback` 原因，不报错、不假装。
- 坐标为 0×0 的节点（虚拟化/离屏）直接丢弃，计入 `stats.droppedNoRect`。
- **遍历顺序（v0.4.1 修）**：先扫内容区（Chromium 的 `Chrome_RenderWidgetHostHWND`，找不到再退回 `Document`），
  再扫外壳（标题栏/工具栏/标签页/菜单）。外壳只扫到 6 层——浏览器外壳都在浅层，这样不会把内容区重复遍历一遍。
  内容区扫了 60 个节点仍无可交互控件（记事本正文那种）→ 判定「不是可点内容」，放弃并把预算还给外壳。
  内容区搜索走 UIA 服务端 FindFirst（0.15s 超时），**搜索耗时不计入扫描预算**。
  > 修之前：收藏夹菜单一开，DFS 先吃外壳、300ms 用光，页面按钮全在截断线后（实测 215 节点/20 可交互，全是外壳）。
  > 修之后：同一场景 71 节点 / 221ms / **完整**，`禁用按钮` 是 u2。
- 截断位置会写在回执里：`truncatedWhere: "content"|"shell"` + `coverage.{contentRoots,contentComplete,shellComplete,contentAbandoned}`，
  `note` 用人话说明「截断在外壳层（内容区已扫完）」还是「截断在内容区内部」——避免让人误以为页面里没有按钮。
- **首读重试（v0.4.2）**：Chromium 的无障碍树比页面渲染慢半拍，刚打开的页面第一次可能只有外壳。
  浏览器/Electron 窗口如果「内容区为空」，自动等 **200ms 重试一次**，回执标 `retried:true` 并在 note 里说明；
  重试还空就照常返回外壳（不无限等）。实测模拟首帧为空时，重试后 5 个内容控件全回来了。
- **预算余量**：Edge 满树实测 301ms（菜单关闭）～378ms（收藏夹菜单开着、节点变多）。
  常态用默认 300ms 会截断，但截断只影响「后面还没读到的部分」，已读到的可交互控件照常返回。
  经常开着菜单扫的话，把 `budgetMs` 提到 400 更省心（不设默认，按需给）。

与 `vision_look` 的分工：UIA 管「是什么、能不能点、在哪」，视觉/OCR 管「选中态、淡字、图文混排」——
**互补，不是替代**。`withOcr:true` 时 UIA 元素在前、OCR 只补 UIA 没覆盖的文字（完全落在 UIA 框里的不重复）。


### 场景图：relations + actions（v0.5.0）

`ui_tree` 现在除了元素列表，还给**关系**和**候选动作**（纯几何计算，零模型成本）：

```json
"actions": [{"id":"u1","action":"click"}, {"id":"u4","action":"type"}, {"id":"u2","action":null,"blocked":true}],
"relations": [{"b":"u4","rel":"labelFor","label":"用户名：","gap":10},
              {"a":"u2","rel":"rightOf","b":"u1","gap":10},
              {"a":"u4","rel":"below","b":"u1","gap":195}],
"relationsSkipped": null,
"labelForUnresolved": []
```

- **actions**：Button/Hyperlink/MenuItem→`click`，Edit/ComboBox→`type`，CheckBox/RadioButton→`toggle`，
  TabItem/ListItem/TreeItem/Tab→`select`，Slider/Spinner→`set`。
  `enabled=false` 的元素给 `action:null, blocked:true`——这是「能不能点」的真值。
- **relations**：`rightOf` / `below`（中心点 + 重叠判据）、`labelFor`（Text 标签 → Edit/ComboBox，
  纵向中心差 ≤20px、水平间距 ≤80px，或标签在正上方）。
  **labelFor 识别不出来就进 `labelForUnresolved`，不许乱标**——关系猜错比没有关系更害人。
- 只算可交互元素，**超过 30 个不算关系**（`relationsSkipped:"too_many"`），actions 照给。
- 实测（测试页）：`u2 rightOf u1（gap 10）`、`labelFor 用户名：→ u4（gap 10）`、禁用按钮 `action:null/blocked:true`。

### Set-of-Mark：vision_look{markIds:true}（v0.5.0）

另存一张 `xxx-marked.jpg`：元素红框 2px + 左上角白底黑字 id；**原帧不动、OCR 先跑完再画**。
回执多一个 `markedFrame` 路径，可直接拿去做 `vision_understand{frame:"…-marked.jpg"}` 问答引用 id。
默认 `false`，不带它时行为一字不变。

## 余光：vision_watch（v0.6.0）

后台盯屏幕变化：**纯差分、不跑 OCR、不调模型**。变了就往当前会话投一条 `[余光]` 消息（`agent.followup`），模型下一轮自然看到。

```text
vision_watch{action:"start"}                                    # 全屏盯，默认 2s 采样
vision_watch{action:"start", windowMatch:"记事本", cooldownMs:60000, maxPerHour:10}
vision_watch{action:"status"}                                   # 计数：累计/队列/四类过滤/注入成功失败
vision_watch{action:"snap"}                                     # 强制截一张（不受阈值/冷却限制）
vision_watch{action:"stop"}
```

### 实测（2026-11-23）

| 场景 | 结果 |
|---|---|
| 置顶窗口**静止** 10 秒 | **0 事件**（17 次采样全判静止） |
| 置顶窗口**动画** 10 秒 | 17 事件，score 0.08–0.11，帧与窗口名正确 |
| followup 注入 | 3 条成功、0 失败（事件 → `agent.followup` → 会话） |
| 冷却 cooldownMs=5000 | 过滤 15 次 |
| 配额 maxPerHour=3 | 配额过滤 12 次，事件停在 3 |
| 静默 quietUntil="23:59" | 静默过滤 11 次，0 注入 |
| 窗口 windowMatch=不存在 | 窗口过滤 13 次 |
| stop | 线程退出，`running:false` |

### 刹车与实现要点

- 默认：`intervalMs 2000 / threshold 0.02 / cooldownMs 45000 / maxPerHour 40`；另有 **≥8 块最小变化量**（防圆角/阴影透出动态壁纸的零星闪烁）。
- **静止零事件**是硬指标：分数低于阈值或变化块太少都直接丢，不进队列。
- `windowMatch` 命中时**只抓那个窗口区域**（省 CPU、避开动态壁纸），事件里的窗口名用命中窗口的标题，不依赖前台。
- 事件队列上限 200，帧只留最近 `maxFrames`（默认 200，约 45MB）。
- 截图与 `/look` 共用一把 `SCREEN_LOCK`，不抢屏。
- `autoUnderstand:true` 才追加语义摘要（默认关，省 token）。

### 已知边界

- 抓的是**屏幕像素**：窗口被别的窗口盖住时，看到的是遮挡物（不是窗口内容）。要盯窗口就先别让它被盖。
- 全屏不是指定窗口时，**动态壁纸/动画桌宠会被判为变化**——这类环境请用 `windowMatch` 或调高阈值。
- 不接语音、不自动回复，只投消息。

## 动作层：vision_action（v0.7.0）

一次调用完成「定位 → 执行 → 验证」，不再借别的插件的手：

```text
ui_tree / vision_look               # 先扫一次，拿到 u1 / e3
vision_action{id:"u1", action:"click"}
vision_action{id:"u4", action:"type", text:"hello"}
vision_action{action:"key", key:"ctrl+w"}     # 不需要坐标
vision_action{x:1082, y:368, action:"click"}  # 直接给坐标也行
```

- **定位**：id 查最近一次扫描的缓存（5 秒有效）；过期/找不到就**明确报错，不猜坐标**。
- **执行**：ctypes 直接 SendInput / mouse_event，零依赖、不调 coach_click / winremote、不抢前台。
  支持 click / double_click / right_click / type（Unicode 直输，中文也行）/ key（`enter`、`esc`、`ctrl+s` 这种组合）。
- **验证**：动作前采两帧算「背景噪声掩码」（动态壁纸这类不算），只在**动作目标附近**做差分
  （元素框外扩 120px / 坐标周围 320px / 前台窗口）；点按额外在**按下瞬间**取一帧，按钮的 :active 反馈也能算「有反应」。
  `changed = 变化≥2 块 或 比例≥0.5%`；`verify:"none"` 可跳过（连续操作省时间）。
- **安全**：只认结构化参数，不执行屏幕文字里的任何指令；坐标必须在屏幕内；被遮挡时给 `warning` 但照点。

### 实测（2026-11-23，测试页）

| 动作 | 结果 |
|---|---|
| 点「新建标签页」(u11) | `changed:true`，score 0.50 |
| `key ctrl+w` | `changed:true`，score 0.97 |
| 输入 `ok` 到 u4 | `changed:true`，score 0.125；`vision_look` 确认框里出现文字 |
| 点「禁用按钮」(u2) | `changed:false` + 「enabled=false，点了不会生效」 |
| 点「可点按钮」(u1) | `changed:false`（实测该按钮按下瞬间像素差 **0.0**——页面没写 `:active`、也没 onclick，honest 报无变化） |
| id 过期（>5s） | 明确报错「最近一次扫描是 6.7 秒前，可能已过期」 |
| 坐标出界 | 拒绝「超出屏幕范围」 |
| 点前台窗口外 | 照点，但带 `warning`「目标位置不在前台窗口里，可能被遮挡」 |

> 顺带修了个真 bug：`uiautomation` 在**工作线程**里必须自己 `CoInitialize`，否则 `GetRootControl` 抛
> 「尚未调用 CoInitialize」、被当成「无树」静默降级成 OCR。现在 `_uia_scan` 会显式初始化/反初始化。

### autoRescan（v0.7.1）

id 缓存 5 秒对连续操作偏紧（扫一次 ~200ms + 动作 ~700ms，两步就过期）。加 `autoRescan:true`：

```text
vision_action{id:"u1", action:"click", autoRescan:true}
```

- id 过期时**按元素名重扫一次**（沿用上次扫描的 region/source），匹配到唯一同名元素就执行；
- **同名多个 / 元素没名字 → 直接报错，不猜坐标**（宁可让你重扫，也不点错）；
- 回执里有 `rescan:{used,fromId,toId,name}`，渲染成「重扫：u1 → u7（按「可点按钮」匹配）」；
- 默认 `false`：不带它时行为和之前一字不差（过期仍明确报错，并在错误里提示可以加 autoRescan）。

实测：扫一次 → 等 7 秒 → `autoRescan:true` 点 u1，重扫匹配到 u1 并点击，`changed:true`；
不带 autoRescan 时仍报「最近一次扫描是 15.2 秒前…或加 autoRescan:true」。

## 连招与重试：vision_action{batch} / retryOnNoChange（v0.14.0）

```text
vision_action{batch:[
  {x:588,y:414, action:"click"},
  {x:588,y:414, action:"type", text:"hello"},
  {id:"u5", action:"click", autoRescan:true},
], continueOnError:false, stepWaitMs:150}

vision_action{id:"u1", action:"click", retryOnNoChange:true}
```

- **batch**：顺序执行，默认每步 verify；失败（ok:false 或 verify 要求变化但 changed:false）默认停止，
  回执 `steps` 只含已执行步骤 + `failedAt`；`continueOnError:true` 继续；上限 10 步；步间默认 150ms。
- **retryOnNoChange**：默认 false；开启后第一次 `changed:false` → 等 500ms → 按元素名重扫 → 再点一次（只一次），
  回执 `retried:true` + `firstAttempt` + `rescan`。坐标动作不重试（没名字可重扫）。
- 单步默认行为不变（不传这两个参数时，回执字段与旧版一致）。

实测（Tk 靶子）：三步连招 2373ms 全部成功；过期 id 首步失败 → `failedAt=0` 且后续不执行；
`continueOnError:true` 时后续仍执行；11 步被拒；retry stub 单测确认 changed=false 只重试一次。

## 文字变化：vision_watch{textTrack}（v0.8.0）

余光不只说「画面变了」，还说**哪行字变了**——只为变化区域跑一次 OCR，不做全屏三带扫描。

```text
vision_watch{action:"start", textTrack:true}    # 默认开
vision_watch{action:"start", textTrack:false}   # 退化成纯像素事件（和 v0.7 一样）
```

事件里多两个字段：

```json
"textChanges": [{"op":"add","text":"hello 大肥鱼","conf":0.97},
                {"op":"remove","text":"请输入用户名","conf":0.99},
                {"op":"update","from":"标题 A","to":"标题 B","conf":0.98}],
"textSkipped": "too_large"   // 区域太大没跑 OCR 时才有
```

### 做法与防坑

- 变化判定通过后，取变化块 bbox 并集外扩 20px → 只裁这块做 OCR（复用现有 OcrEngine，不新写）；
- **两遍新鲜抓图取交集**（间隔 120ms）才算「这一帧真实内容」，单次抖动不进结果；
- 基线用**上次同区域的稳定文字集**（区域重叠 ≥50% 才复用），没有再退回上一帧裁剪；
- `conf < 0.6` 丢弃；变化区域 **> 50% 屏幕**直接跳过（全屏刷新，报了也没用）；
- `add` / `remove` / `update` 按位置（≤60px 水平、≤30px 垂直）配对；`textTrack:false` 时行为与 v0.7 一字不差。

### 实测（Tk 置顶窗口，文字逐次变化）

| 场景 | 结果 |
|---|---|
| 「标题 A」→「新的标题文字」（只改一两个字） | `update` 标题A → 新的标题文字（conf 1.0） |
| 空白区域出现「hello 大肥鱼」 | `add` 「hello 大肥鱼」（OCR 碎片已合并成一行，不再拆成 hel/ello） |
| 该行被清空 | `remove` 「hello 大肥鱼」 |
| `textTrack:false` | 事件无 textChanges |

### v0.8.1 修的三处（都是复核实测暴露的）

1. **before 坐标系混用**：窗口截图的原点不是屏幕原点，`_scale` 还拿「图宽/屏幕宽」当缩放比
   → before 裁出来的永远是标题栏一角（只能读成 `n-text`），于是所有变化都报成 `add`。
   现在每帧记录 `origin`（全屏=屏幕原点 / 窗口=窗口左上角）和 `exp_w`，裁图按图自己的基准算。
2. **OCR 切词**：同一行的碎片（hel + ello）按纵向重叠 ≥50% 合并成一行，横向间距 ≤12px 直接拼、≤40px 补空格。
3. **update 配不上**：配对从「同位置」改成「同一行（纵向重叠 ≥50%）」，OCR 边距 20 → 40px，
   只改一两个字也能读到旧文本、配成 update。
4. **合并顺序（v0.8.2）**：行内碎片先归组、**组内按 x 坐标升序**再拼，不再信 OCR 返回顺序——
   之前会出现 `-大肥鱼hello`，现在是 `-hello 大肥鱼`。

## 档位热切换 + 浮窗面板（v0.10.0，T3a/T3c）

## 档位的三个入口（v0.11.1，T3b 定版）

档位唯一真源是 **cordis config**（插件 `Config` 的 `profile`，标 `volatile`）：

```yaml
# profile 的 cordis.patch.yml
- id: omnivision
  name: "@local/dsh-omnivision"
  config:
    profile: energy      # energy / standard / performance
```

三个入口都最终写这一份配置、并热切 sidecar：

| 入口 | 用法 | 说明 |
|---|---|---|
| **右下角面板** | 点「省电/标准/性能」 | 走 `/omnivision profile` 命令；「状态」按钮直接显示在面板里 |
| **工具** | `vision_profile{action:"set", profile:"energy"}` | 给 agent 用；`action:"get"` 看当前 |
| **命令** | `/omnivision profile energy` | 人在对话里用；`/omnivision watch on|off|status` 也在这里 |

- 读取优先级：**cordis config > 旧 `omnivision.config.json` > standard**（老用户升级不丢设置；首次启动会把旧值迁进 config）。
- `vision_status` 同时显示「配置档位」和「实际生效档位」，不一致会标 ⚠。
- 改 profile 的另一种方式：手改 profile 的 `cordis.patch.yml` 里的 `config.profile`，然后重启 DSH（等同于设置页那条路，但没有 GUI）。

### ❌ 设置页入口：上游限制，已放弃

实测 DSH **0.2.2-rc.5**：设置 →「内置插件」页只渲染**内置插件**（玩 Toy / Agent 预设 / 桌宠），
在里面搜 `omnivision` 没有任何结果；第三方 bundle 导出的 `Config` **没有 GUI 入口**。
（`@deepseek-ai/dsh-client-ui-settings-plugins` 的 tab 只挂内置清单。）

结论：**不再做自建设置页**。`Config` 仍然保留——它让档位进入 profile 配置、可被手改、可被 `settings.describe()` 读到，
但没有官方 GUI；要改档位就用上面三个入口。

### client 服务访问清单（踩过的坑）

client 工厂的 `inject` 数组必须声明**所有**用到的服务，**子服务要单独声明**：

```js
inject: ['slots', 'remote', 'remote.commands', 'remote.settings']
```

- 漏 `remote` → `cannot get property "remote" without inject`
- 漏 `remote.commands` → `cannot get property "remote.commands" without inject`
- 面板要用 `settings.describe()` 读档位，就必须再声明 `remote.settings`

## T3e 探针结论（2026-11-24，只探未接）

- `ctx.remote.settings` 存在：`dsh-api-settings-controller` 注册命名空间 `settings`，@Remote 方法
  `describe / update / replace / mutate / openSettingsDocument`，`describe()` **返回值**（含 schema、revision、value）。
- 但只覆盖「有 Config schema 且字段带 volatile」的插件（`dsh-settings` 的 `volatileForm`）；本插件目前没有 Config，
  所以现在读不到自己的档位。要接：给插件导出一个 `Config`，把 `profile` 标 volatile，存进 profile 的 cordis config。
- `settings/document-updated` 在转发白名单里，但 payload 只有 `(ns, revision)`，**不带新值**；收到后要再 `describe()`。
- 更省事的一条：`commands.execute()` 返回 `{commandId, result:{kind,text}}`（`dsh-commands` L347–350），
  面板直接读 `r.result.text` 就能显示状态——v0.10.5 已按这条实现，不再依赖任何新通道。

> **T3d 探针结论（2026-11-24）：自定义 remote 事件不通，别再试。**
> `ctx.remote.$on` 的合法事件名 = `@deepseek-ai/dsh-api-remotes/lib/index.js` 里硬编码的
> `API_REMOTE_FORWARDED_EVENTS` 白名单（L21–151），注释写明「the legal key set of ctx.remote.$on」；
> `registerRemoteEvents` 只接受**一个** application-selected source（api-gateway L882），插件没有扩展入口。
> 白名单里有 `commands/change` / `plugin-manager/changed` / `settings/document-updated` 等，但没有我们的。
> 结论：档位状态只能走命令结果（出现在对话里）；面板的「上次选择」用 localStorage 持久化，
> **不再谎报「当前档位」**，要权威值就看对话里的 `/omnivision status` 输出。


> **client 插件踩坑记录**：`ctx.remote` / `ctx.slots` / `ctx.commands` 这类服务**必须在 client 工厂的
> `inject` 数组里声明**，否则浏览器报 `cannot get property "x" without inject`。
> **子服务要单独声明**：`ctx.remote.commands` 需要同时写 `'remote'` 和 `'remote.commands'` 两个依赖
> （cordis 把子服务当独立依赖），只写 `remote` 会报 `cannot get property "remote.commands" without inject`。
> 正确写法：`inject: ['slots', 'remote', 'remote.commands']`（参考 `dsh-client-ui-commands` 的
> `static inject = ['inputTriggers', 'sessions', 'remote', 'remote.commands']`）。
> 用 `ctx.get('sessions')` 访问的可以不在 inject 里（走 get 容错）。


- **热切换**：`vision_profile{action:"set", profile:"energy"}` 或 `/omnivision profile energy`
  → sidecar `/profile` 立即换线程并**重建 OCR 引擎**（约 1.2 秒），不用重启 DSH；
  同时写回 `omnivision.config.json`，重启后保持。
- 实测：standard → energy，线程 4→1，全屏 OCR 1078ms → 2123ms（真换了引擎，不是只改配置）。
- **浮窗面板**（`client.js`，`shell.overlay` 槽，右下角）：三档切换 / 开光余光 / 刷新状态。
  动作走 `/omnivision` 命令；客户端代码只在 DSH 启动时加载，装完/升级后需重启 DSH 才出现。

## 模型名与套餐额度（2026-11-25 核对）

### 三条线统一用 `qwen4-max`

| 线 | 位置 | 实际值 |
|---|---|---|
| 子代理默认路由 | profile `cordis.patch.yml` 的 `tool-subagent.agentOptions` | `provider: bailian` / `model: qwen4-max` |
| 语义层 cloud | sidecar `LLM_DEFAULT_MODEL` | `qwen4-max` |
| 外脑脚本 | `qwen-ask.py`（数据目录 `tools/`）的 `DEFAULT_MODEL` | `qwen4-max` |

- 三处配置/源码 grep `qwen4-max-preview` **0 命中**（我们没引用已下线的 preview）；
- 2026-11-25 18:5x 复测：`qwen4-max` 在模型列表且 HTTP 200；`qwen4-max-preview` 已不在列表，
  但请求会被服务端映射回 `qwen4-max`（响应里 `model` 是 `qwen4-max`）——暂时不报错，但不能依赖。
- `vision_status` 的「云端：<模型>」显示的就是实际请求用的模型名。

### Lite 套餐：20,000 Credits/月、1–2 个 Agent 并发

- **本地估算，不是官方用量 API**（百炼没有给我们可查的用量接口，不假装有）：
  - 每次 cloud 调用把响应里的 `usage.total_tokens` 累加进 `<dataDir>\usage.json`，按月自动重置；
  - `vision_status` 显示：`本月模型：调用 N 次｜tokens ≈ M｜≈ C / 20000 Credits（X%）`；
  - 估算口径：`Credits ≈ totalTokens / tokensPerCredit`，默认 `tokensPerCredit=1000`（**粗估**，
    可用环境变量 `DSH_OMNIVISION_TOKENS_PER_CREDIT` 调整；月预算用 `DSH_OMNIVISION_MONTHLY_CREDITS`，默认 20000）；
  - 到 **80%** 时 `vision_status` 追加一行 `⚠ 套餐用量已到 X%…`；
  - usage.json 用 `utf-8-sig` 读，手工编辑带 BOM 也不会清零。
- **并发**：插件不主动限流；同时派多个子代理（agent team / 并行工具）可能撞上 Lite 的 1–2 并发上限，
  表现为排队或请求报错——要并行就分批，不要一次拉起一堆。
- 语音插件（pet-voice-gsv 云端 TTS）不计入这里，它自己走 Token Plan TTS，额度同样吃 Lite 的 Credits。

## OCR 提速（方案 A，2026-11-25）

> 来源：screen-attention 四件套。RapidOCR 库内部推理改不了，所以 A 阶段只做「参数 + 缓存」。

### A1. det 模式自适应（不是简单改短边数字）

RapidOCR 的 `limit_type=min` **只把小图放大到 limit，不会缩小大图**——实测 det-only 在
736 / 512 / 384 三个 limit 下耗时都是 ~330–350ms，因为 2560×1440 原样进了 det。

因此大图改用 `limit_type=max`：库内部按长边自适应到 960/1500/2000，2560×1440 会缩到 2000×1125。
两张全屏帧实测（原生 vs max 模式，按文本多重集）：

| 帧 | 原生 min736 | max 模式 | 文字一致率 |
|---|---|---|---|
| 181002_before（99 条） | 1984ms | 1274ms | **100.0%** |
| 181003_after（93 条） | 1408ms | 1254ms | **100.0%** |

> 识别图**不缩**：试过整图预缩到 1600 → 一致率 54.5%、1280 → 10.1%、1024 → 7.1%（小字全丢）。
> 所以「不降分辨率」这条边界是硬的，只让 det 走缩图，rec 仍在原分辨率上。

### A2. 框级签名缓存

- 每个 OCR 结果框内缩 10% → 灰度缩到 **24×8** → FNV-1a（64 位）；
- 下一次同尺寸 + 同粗签名（16×9 灰度 FNV）时，**逐框重算签名验证**；
  全部一致 → 直接复用文本，**0 次 OCR**；任一框变了 → 回落全量 OCR；
- 缓存 2000 条上限，满了清空；命中率/框数在 `vision_status` 的「OCR 缓存」行。

实测（引擎层，同一张帧连跑两次）：

| 场景 | 冷跑（全量） | 二跑（框缓存） | 文字 |
|---|---|---|---|
| 全屏 2560×1440（99 框） | 1979ms | **23ms** | 100% 一致 |
| 窗口 1296×828（42 框） | 1170ms | **8ms** | 100% 一致 |
| 框内涂黑模拟改字 | — | **回落全量 1283ms** | 未拿旧文字 |

> 注意：`vision_look` 上面还有一层元素缓存，通常第二次直接命中它（几十毫秒）；
> OCR 框缓存主要服务 **watch 文字追踪** 和「画面变了但文字没变」的场景（动态壁纸、宠物动画、光标）。

### 方案 B 试验结论：**不切换推理路径**（2026-11-25，同图对比未通过门槛）

按「库版 vs 自研版同图文字一致才切」的验收线，逐项验证：

| 试验 | 结果 |
|---|---|
| 自建 det→crop→cls→rec（全用库原语） | 与库版**文字 100% 一致**（99 vs 99），但耗时 312+956≈1268ms，与库版持平——**没有收益** |
| 批量 rec batch=12 / 24 | 比 batch=6 **更慢**：1730ms / 2520ms（batch 越大，padding 宽度被最长框撑大） |
| batch=6（库默认） | 最优：det 335ms + rec 876ms(125 框) + cls 54ms |
| 关掉 cls（省 54ms） | 文字 103 vs 99（**4 条边缘分数跨过 0.5**），一致率 97% → **不满足 ±2% 门槛** |
| skipDet 复用上一帧框 | 受控「同一窗口只改一行字」：旧框裁新图 → 文字 `态：已点击1`（少一个「状」），一致率 75% → **框随文字宽度变化，复用会裁歪** |

- `rec` 的成本是 125 个裁剪逐批推理（≈7ms/框），批量不能摊薄单框卷积计算；
- `det` 335ms、`cls` 54ms、crop+后处理约 300ms，剩下都是 rec；
- **结论**：方案 B 不切。要再快只有两条路：缩 OCR 区域（已证明可靠）或换模型/量化（未验证）。

### 下一步（可选）

绕过 RapidOCR 直接 onnxruntime：批量 rec（[N,3,48,160] 一次推理）+ skipDet。
先把「库版 vs 自研版同图文字一致」验证通过才能切；预期全屏进 200–400ms。

## 性能与线程预算（v0.8.3，T1）

默认不让 ONNX 吃满全核——低端机一次全屏 OCR 就风扇起飞。

```text
默认：max(1, min(核数, 4, max(2, 核数-1)))    # 12 核→4，4 核→3，2 核→2
覆盖：环境变量 OMNIVISION_ONNX_THREADS=1..64
查看：vision_status 里一行「ONNX 线程：4（自动，12 核）」
```

> 规格原写 `min(4, 核数/2)`，但 4 核机器算出来是 2——12 核实测 2 线程比 4 线程慢 54%，超过「≤20%」验收，
> 所以 4 核用 3（慢约 11%，CPU 只占 3 核）。

实测（2560×1440 全屏、同一张图、同一进程交替取中位）：

| 层 | threads=4 | threads=12 | 结论 |
|---|---|---|---|
| OCR | 1.18s / CPU 4.89s | 1.42s / CPU 14.30s | 4 线程**更快**且 CPU −66% |
| 图标 | 82ms / CPU 328ms | 140ms / CPU 1469ms | 同上，14 个框完全一致 |
| 识别结果 | 96 条文本 | 96 条文本 | **文本集合完全一致** |

线程超订阅反而拖慢：小模型上 12 线程抢核，墙钟和 CPU 双输。

## 三档：energy / standard / performance（v0.9.0，T2）

配置文件 `omnivision.config.json` 里加 `"profile"` 字段，或在 `vision_watch` 里传 `profile`。

| 档 | watch 间隔 | textTrack | OCR 线程 | 语义默认 | OCR 实测 |
|---|---|---|---|---|---|
| energy | 5s | off | 1 | off | 2.09s |
| standard（默认） | 2s | on | 4（12 核） | local | 0.88s |
| performance | 1s | on | 全核 | cloud（有 key 才用） | 1.10s |

（同一张 2560×1440 全屏图，全屏 OCR 3 次取中位。）

规则：

- **显式参数优先**：`vision_watch{profile:"energy", intervalMs:700}` → 采样 700ms，其余按 energy；
- `vision_watch{profile:"energy"}` 只影响这一次 watch，不动全局档位；
- 全局档位改 `omnivision.config.json` 后**要重启 DSH**（OCR 线程是 sidecar 启动时定的）；
- `vision_status` 显示「档位：standard（watch 2s + textTrack on｜语义默认 local）」和实际 ONNX 线程数；
- energy 档下 `vision_understand` 默认 `backend:"off"`（不联网、不花钱），仍可显式传 local/cloud 覆盖；
- 七件套默认路径不变：standard 档行为和 v0.8.3 一致。

> 注意：performance 档的 OCR 比 standard 还慢（1.10s vs 0.88s）——全核线程超订阅，和 T1 的结论一致。
> 它的价值在 watch 更密的采样和云端语义，不在 OCR 更快。

## 问答模式：vision_understand{question:"…"}

传 `question` 就走「看图问答」，**不传就还是上面的清洗行为，一字不改**（封版回归线）。

```text
vision_understand{question:"哪个按钮是禁用的？"}
vision_understand{question:"登录按钮对应哪个输入框？", frame:"F:/.../xxx.jpg"}
```

- **只支持 cloud**。`local` / `off` 会明确拒绝：「问答只支持 cloud：本地 3B 看缩略图不可靠，off 模式不联网」——
  不硬答、不降级成胡编。显式传 `backend:"cloud"` 就行（默认 backend 是 local，所以问答要带上它）。
- 回执带 `mode: "ask"`、`backend`、`usage`；同一帧同一个问题第二次走缓存（0 请求）。
- 提示词在 `prompts/ask.md`：只按看得到的答，看不到就说看不出来，不许猜。
- 成本：每次约 1200–1500 tokens、2–4s（图片占大头）；换问题要再花一次。
- 图片分辨率治不了「淡字/禁用态」——那是原图就淡（opacity .35），不是缩略问题。
- 问答结果不进 `elements` 清单，避免污染清洗语义。


本地栈：llama.cpp **Vulkan 预编译版**（37MB，不用装 CUDA runtime、不碰 torch）+ Q4_K_M 1.84GB + mmproj 806MB。
llama-server 由 sidecar 按需启动（已经在跑就复用），日志在 `logs/llama-server.log`。

**local 怎么工作（重要）**：3B 看 1024 缩略图基本是瞎的——实测会把元素清单原样抄回来、场景判成 browser。
所以 local 走的是**裁图网格**：把元素框从原图裁出来放大到 48px 高、左边标 id，拼成一列再问
「每行读的什么」。这条路它能改对（`这工月`→`这工具`、`Bilibil液莺Night`→`Bilibili 夜莺Night`）。
场景和概括不用模型：场景走启发式（含桌面兜底），概括留空。

实测（5070 Ti，Vulkan）：llama-server 启动 3.5s；模型单次 1.0–1.1s（热）；整次 `vision_understand`
首次 8.2s（含启动）、之后 2.2s（含 OCR）。显存增量约 3.4GB（空闲时 6.7/16.3GB）。


```text
vision_understand                              # 整屏理解（约 8-12s，一次请求）
vision_understand{region:"window:Edge"}        # 只看某个窗口
vision_understand{frame:"C:/.../xxx.jpg"}      # 用已有截图理解，不截屏（复核历史帧）
vision_understand{thumbWidth:800}              # 更快更省（小字可能看不清）
vision_understand{thumbWidth:1600}             # 小字要看清
```

- **默认不发请求**：只有显式调用这个工具才会调模型；`vision_look` 永远不会联网。
- **缓存**：语义结果按「场景指纹 + 区域 + 尺寸 + 缩略宽」缓存，同一帧第二次 0 请求（约 1.2s，主要是重跑 OCR）。
- **降级**：没有 key、模型 404、超时、返回空正文、返回非 JSON —— 都会退回原始 OCR 元素并标 `degraded:true`，不抛错。
- **成本**：缩略图 800px 宽约 400 image tokens、1024 约 660、1280 约 1000；直接发 2560 原图约 3600。
  实测一次整屏理解 prompt 1.5–2.2k tokens、输出 250–500 tokens。
- **关思考**：请求体固定带 `enable_thinking:false`。不关的话 qwen4-max 会把输出额度全烧在
  reasoning 上，正文返回空字符串（实测 27s 后 content:""），看起来像"模型超时"，其实是空回。
- **场景表**：`desktop|browser|editor|game|dialog|unknown`，提示词在 `prompts/understand.md`，
  改完即时生效（按 mtime 重读）。

### 参数策略（成本 / 精度 / 耗时）

| 场景 | thumbWidth | 实测总耗时 | 取舍 |
|---|---|---|---|
| 日常 / 快 | 800 | ~7.9s | 小字不修（「这工月」这种修不了） |
| **默认** | **1024** | 6–12s | 平衡点，推荐 |
| 要全 | 1280 | ~14.6s | 确认数最多（19 个），最准 |

以上是 **cloud** 后端的数字。`local` 后端不看 thumbWidth，走裁图网格，热调用约 2.2s、首调 8.2s。

耗时线按 **≤15s、典型 6–8s** 算：多出来的波动在服务器排队，不在本地。
真要卡死在某个数上，就往下调 `thumbWidth`，别指望模型那边的抖动。

原则：**宁可少修，不许编**。模型只在看得清时才返回，看不清的元素原样保留（不打 ✓）。
写进 `prompts/understand.md` 的规则，不要为了让确认数变多而放松它。

## 图标层：icons:true（默认关）

`vision_look{icons:true}` 会在文字元素之外补一层「**可点控件**」：

- 模型：`OmniParser icon_detect`（microsoft/OmniParser-v2.0 的 icon_detect，社区 ONNX 导出），
  纯 onnxruntime，**不用 torch、不用转换**。文件 `models/omniparser-icon/model.onnx`，11.57MB。
- 输入：等比缩到最长边 1280、**补灰边到 32 的倍数**（这个导出不吃任意宽高，720×1280 会报 Concat 错）。
- 后处理：conf 0.3、NMS 0.45（ONNX 不带 NMS），坐标映射回原图。
- **合并策略**（`blend_icons`）：模型框到的控件不直接塞进列表，先看框里有没有文字——
  - 文字框 ≥85% 落在控件框里 → 给**那条文字元素**打 `interactive:true` 并记下 `widget`（按钮的点击范围）。
    元素数量不变，但「这是按钮」的信息保住了。
  - 控件框里没有文字 → 作为独立的「无字图标」元素保留。
  - 实测：一次整屏 21 个控件框 → 并入 8 个、独立 13 个，**一个不丢**。
- 耗时：桌面整屏约 140–150ms，小窗口约 110–120ms（CPU）。模型加载 54ms，只加载一次。
- 取向：**只走模型，不和 OpenCV 并集**。模型框的是「控件」、OpenCV 框的是「轮廓」；混在一起会把准头弄脏。
  模型文件不存在或推理报错时才退回 OpenCV（`iconEngine:"opencv"` 可强制指定）。

实测对照（同一张桌面整屏 / 浏览器窗口）：

| 界面 | OpenCV 兜底 | OmniParser 模型 |
|---|---|---|
| 桌面 2560×1440 | 17 个：桌面快捷方式基本框到、任务栏图标框到，但把壁纸角色也当图标（误报） | 12 个：侧栏行/按钮/标签片、任务栏图标；桌面小图标（48px 缩到 24px）漏检 |
| 浏览器 1455×1021 | 0 个：纯平 UI 找不到轮廓 | 13 个：标签页、地址栏、搜索框、收藏、分享、头像、三个窗口按钮、⋯、四个导航按钮全中 |

结论：模型精度高、漏小图标；OpenCV 反过来。所以**默认关、按需开**，先纯模型跑真实场景攒漏检率。

点按不重复造轮子：元素的 `center` 直接喂 `cv_act{click:[x,y], coordSpace:"screen"}`，
或已锚定的窗口里用 `coach_click{x,y}`。

## 实现

```
一帧截图（PIL ImageGrab，DPI 感知）
 ├─ 文字层：RapidOCR（PP-OCRv4，onnxruntime CPU），批内 max_wh_ratio 钳到 8
 ├─ 图标层（可选 icons:true）：OmniParser icon_detect（YOLOv8 ONNX，1280 输入）→ 可点控件框；
 │                        模型缺失/报错才退 OpenCV 轮廓兜底
 ├─ 元素层：box → 按行排序 → id/center/interactive
 ├─ 时序层：8 倍缩小灰度图的 32px 块差分 → changes
 ├─ 缓存层：dHash(9×8) 指纹，汉明距离 ≤ 5 视为同一界面 → 直接复用元素清单
 └─ 出口：JSON（文本清单 + 截图路径）
```

纯 ONNX，**没有 torch**。运行时在 `<数据目录>\venv`，模型随 `rapidocr_onnxruntime`
装在 venv 里，截图落 `<数据目录>\frames`（只有缓存未命中才写）。

## 已知限制

- 只支持 Windows（依赖 `ImageGrab`、DPI 感知、`EnumWindows`）。
- **被遮挡的窗口拍不到**：抓的是屏幕像素，窗口被别的窗口盖住时就只拍到盖住它的那个。
  要读后台窗口先用 `coach_anchor` / 把窗口提到前面，或直接传 `frame` 用历史截图。
- 文字层只认「有文字」的元素。`icons:true` 补的是**可点控件**（模型），不是「所有图标」：
  桌面 48px 快捷方式缩到 1280 输入后只有 24px，会漏；纯图标界面反而认得准。
- 一个控件只出一条元素（文字 + 可点标记），不会重复；但 `interactive` 是提示不是保证，
  点不动的情况照样存在（比如被禁用、被遮挡）。
- 场景判别有两层：没调语义层时是关键词启发式 + **桌面兜底**（整屏时，右下角有 HH:MM 时钟 /
  左边缘一列图标 / 底部一排任务栏图标 → 判 `desktop`）；调了 `vision_understand` 就用模型判的。
  窗口区域不会触发桌面兜底。
- 语义层只修**模型看得清**的字：缩略图太小的小字它会跳过（不返回即不改），想让它修就调大 `thumbWidth`。
- 全屏首帧 OCR 约 2 秒（CPU）；界面变了就是一次新 OCR。缓存只救「同一界面重复看」。
- 长文本行识别可能截断/粘连（`max_wh_ratio` 钳制的代价）。
- `interactive` 只是「像按钮」的提示，不代表一定能点。
- 缓存按「区域 + 尺寸 + 是否 OCR + 是否图标」分组，最多留 48 条，超了淘汰最旧的。

## 配置

- `DSH_OMNIVISION_PYTHON`：python 可执行文件，默认 `<数据目录>\venv\Scripts\python.exe`
- `DSH_OMNIVISION_DIR`：数据目录（venv/frames 的上级），默认 `<数据目录>`
