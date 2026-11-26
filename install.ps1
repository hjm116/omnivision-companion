<#
  OmniVision 安装器（Windows 11 x64）

  用法（在解压出来的目录里）：
    powershell -ExecutionPolicy Bypass -File install.ps1
    powershell -ExecutionPolicy Bypass -File install.ps1 -DataDir "D:\omnivision-data"
    powershell -ExecutionPolicy Bypass -File install.ps1 -Wheelhouse ".\wheels"   # 完全离线装 Python 依赖

  做的事：
    1. 找 Python 3.10–3.12
    2. 在数据目录建 venv，装 onnxruntime / rapidocr_onnxruntime / numpy / pillow
    3. 把小模型放进数据目录（不联网下载模型）
    4. 把插件装进 DSH profile 并跑一次 pnpm install
    5. 写一条「请完全退出 DSH 再打开」

  不会碰：~/.dsh/.credentials.yaml、其他插件的配置
#>
param(
  [string]$DataDir = (Join-Path $env:USERPROFILE '.dsh\omnivision'),
  [string]$DshHome = (Join-Path $env:USERPROFILE '.dsh'),
  [string]$Profile = 'desktop',
  [string]$Python = '',
  [string]$Wheelhouse = '',
  [string]$VenvDir = '',
  [string]$PluginDir = '',
  [switch]$InstallCompanionExtras,
  [switch]$SkipSenseVoiceModel
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$depName = '@local/dsh-omnivision'

function Resolve-FullPath([string]$p) {
  if (-not $p) { return '' }
  try {
    return $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($p).TrimEnd('\', '/')
  } catch {
    return [System.IO.Path]::GetFullPath($p).TrimEnd('\', '/')
  }
}

$DataDir = Resolve-FullPath $DataDir
$DshHome = Resolve-FullPath $DshHome
if ($VenvDir) { $VenvDir = Resolve-FullPath $VenvDir }
if ($PluginDir) { $PluginDir = Resolve-FullPath $PluginDir }


function Fail($msg) {
  Write-Host ""
  Write-Host "[失败] $msg" -ForegroundColor Red
  Write-Host "如果拿不准，把这一整屏截图发给发给你的人。" -ForegroundColor DarkGray
  exit 1
}
function Step($msg) { Write-Host ""; Write-Host "== $msg" -ForegroundColor Cyan }
function Info($msg) { Write-Host "  $msg" }

# 无 BOM 写 UTF-8：PowerShell 5.1 的 Set-Content -Encoding UTF8 会带 BOM，DSH 读 profile 会 JSON.parse 失败（v0.3.0 炸过）
function Write-Utf8NoBom([string]$path, [string]$text) {
  $enc = New-Object System.Text.UTF8Encoding($false)
  [System.IO.File]::WriteAllText($path, $text, $enc)
}


Write-Host "OmniVision 安装器" -ForegroundColor Green
Info "数据目录：$DataDir"
Info "DSH 目录：$DshHome"
Info "profile  ：$Profile"

# ---------------------------------------------------------------- 0. 检查
if (-not (Test-Path (Join-Path $DshHome 'profiles'))) {
  Fail "找不到 DSH 目录（$DshHome\profiles）。先装好并打开过一次 DSH，或用 -DshHome 指定正确位置。"
}

# ---------------------------------------------------------------- 1. Python
Step "1/5 找 Python（需要 3.10 / 3.11 / 3.12）"

function Get-PyVersion($exe, $pre) {
  # 返回 "3.12" 这种版本号；不能用就返回空
  try {
    $out = & $exe @($pre + @('-c')) "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -eq 0) {
      $t = ("$out" -replace '\s', '')
      if ($t -match '^3\.(10|11|12)$') { return $t }
    }
  } catch { }
  return ''
}

$pyExe = $null
$pyPre = @()
$pyVer = ''

if ($Python) {
  if (-not (Test-Path $Python)) { Fail "指定的 Python 不存在：$Python" }
  $pyVer = Get-PyVersion $Python @()
  if (-not $pyVer) { Fail "这个 Python 不是 3.10–3.12，或跑不起来：$Python" }
  $pyExe = $Python
} else {
  $cands = @()
  # 1) py launcher
  foreach ($v in @('3.12', '3.11', '3.10')) { $cands += @{ exe = 'py'; pre = @("-$v") } }
  # 2) 常见安装位置（官方安装包默认装这里，通常不在 PATH）
  foreach ($v in @('312', '311', '310')) {
    $cands += @{ exe = (Join-Path $env:LOCALAPPDATA "Programs\Python\Python$v\python.exe"); pre = @() }
    $cands += @{ exe = "C:\Python$v\python.exe"; pre = @() }
    $cands += @{ exe = "C:\Program Files\Python$v\python.exe"; pre = @() }
  }
  # 3) PATH 里的 python（排除 WindowsApps 的商店占位符）
  foreach ($name in @('python', 'python3')) {
    $found = (Get-Command $name -ErrorAction SilentlyContinue)
    if ($found -and $found.Source -and $found.Source -notmatch 'WindowsApps') {
      $cands += @{ exe = $found.Source; pre = @() }
    }
  }

  foreach ($c in $cands) {
    if ($c.exe -match '\\' -and -not (Test-Path $c.exe)) { continue }
    $ver = Get-PyVersion $c.exe $c.pre
    if ($ver) { $pyExe = $c.exe; $pyPre = $c.pre; $pyVer = $ver; break }
  }
}

if (-not $pyExe) {
  Fail "没找到 Python 3.10–3.12。装一个：https://www.python.org/downloads/windows/（勾选 Add python.exe to PATH），然后重跑。装完如果还找不到，用 -Python 参数直接指定 python.exe 的完整路径。"
}
Info "用 Python $pyVer（$pyExe）"

# ---------------------------------------------------------------- 2. 数据目录 + 小模型
Step "2/5 准备数据目录和小模型"
New-Item -ItemType Directory $DataDir -Force | Out-Null
New-Item -ItemType Directory (Join-Path $DataDir 'models\omniparser-icon') -Force | Out-Null
New-Item -ItemType Directory (Join-Path $DataDir 'models\rapidocr') -Force | Out-Null
New-Item -ItemType Directory (Join-Path $DataDir 'frames') -Force | Out-Null
New-Item -ItemType Directory (Join-Path $DataDir 'logs') -Force | Out-Null

Copy-Item (Join-Path $here 'models\omniparser-icon\*') (Join-Path $DataDir 'models\omniparser-icon') -Force
Copy-Item (Join-Path $here 'models\rapidocr\*.onnx') (Join-Path $DataDir 'models\rapidocr') -Force
if (Test-Path (Join-Path $here 'presets')) {
  $dstPresets = Join-Path $DataDir 'presets'
  if (Test-Path $dstPresets) { Remove-Item $dstPresets -Recurse -Force }
  Copy-Item (Join-Path $here 'presets') $dstPresets -Recurse -Force
}
Info "图标模型 + OCR 模型 + 伴侣预设已就位（不用联网下载）"

# ---------------------------------------------------------------- 3. venv + 依赖
Step "3/5 建 Python 环境并装依赖（onnxruntime / rapidocr / numpy / pillow）"
$venv = if ($VenvDir) { $VenvDir } else { Join-Path $DataDir 'venv' }
$venvPy = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
  Info "创建 venv：$venv"
  & $pyExe @($pyPre + @('-m', 'venv', $venv))
  if ($LASTEXITCODE -ne 0) { Fail "创建 venv 失败（Python $pyVer）。" }
} else {
  Info "venv 已存在，跳过创建"
}

$pipArgs = @('-m', 'pip', 'install', '--disable-pip-version-check', 'onnxruntime', 'rapidocr_onnxruntime', 'numpy', 'pillow')
if ($Wheelhouse) {
  if (-not (Test-Path $Wheelhouse)) { Fail "Wheelhouse 不存在：$Wheelhouse" }
  $pipArgs += @('--no-index', '--find-links', $Wheelhouse)
  Info "离线模式：从 $Wheelhouse 装"
} else {
  $pipArgs += @('-i', 'https://pypi.tuna.tsinghua.edu.cn/simple')
}
& $venvPy @pipArgs
if ($LASTEXITCODE -ne 0) {
  Fail "装 Python 依赖失败。要么网络不通（重试即可），要么用 -Wheelhouse 指定离线 wheel 目录。"
}
if ($InstallCompanionExtras) {
  Info "按需逐个安装伴侣扩展依赖：dxcam / pyaudiowpatch / sherpa-onnx（单个失败不影响其余安装）"
  $prevEap = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    foreach ($pkgName in @('dxcam', 'pyaudiowpatch', 'sherpa-onnx')) {
      $extraArgs = @('-m', 'pip', 'install', '--disable-pip-version-check', $pkgName)
      if ($Wheelhouse) { $extraArgs += @('--no-index', '--find-links', $Wheelhouse) }
      else { $extraArgs += @('-i', 'https://pypi.tuna.tsinghua.edu.cn/simple') }
      & $venvPy @extraArgs
      if ($LASTEXITCODE -ne 0) {
        Write-Host "  [提示] 可选扩展 $pkgName 安装未成功（不影响基础视觉与游戏驱动启动，可稍后单独补装）。" -ForegroundColor Yellow
      } else {
        Info "可选扩展 $pkgName 已就绪"
      }
    }
    if (-not $SkipSenseVoiceModel) {
      $svDir = Join-Path $DataDir 'models\sense-voice'
      New-Item -ItemType Directory $svDir -Force | Out-Null
      $svModel = Join-Path $svDir 'model.int8.onnx'
      $svTokens = Join-Path $svDir 'tokens.txt'
      if ((Test-Path $svModel) -and (Test-Path $svTokens)) {
        Info "SenseVoiceSmall 模型已存在，跳过下载（$svDir）"
      } else {
        Info "正在下载 SenseVoiceSmall int8 中文/多语语音模型（约 228 MB，优先国内镜像 hf-mirror）..."
        $mirrors = @(
          'https://hf-mirror.com/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main',
          'https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main'
        )
        $svOk = $false
        foreach ($baseUrl in $mirrors) {
          try {
            if (-not (Test-Path $svTokens)) {
              Invoke-WebRequest -Uri "$baseUrl/tokens.txt" -OutFile $svTokens -UseBasicParsing -TimeoutSec 60
            }
            if (-not (Test-Path $svModel)) {
              Invoke-WebRequest -Uri "$baseUrl/model.int8.onnx" -OutFile $svModel -UseBasicParsing -TimeoutSec 600
            }
            if ((Test-Path $svModel) -and ((Get-Item $svModel).Length -gt 50000000) -and (Test-Path $svTokens)) {
              $svOk = $true
              Info "SenseVoiceSmall 模型已就绪：$svDir"
              break
            }
          } catch {
            Write-Host "  [提示] 从 $baseUrl 下载失败：$($_.Exception.Message)" -ForegroundColor DarkYellow
          }
        }
        if (-not $svOk) {
          Write-Host "  [提示] SenseVoiceSmall 未能自动下载（离线或网络波动）；音频转写将回退 faster-whisper 或 vad-only。可稍后手动放 model.int8.onnx + tokens.txt 到 $svDir。" -ForegroundColor Yellow
        }
      }
    }
  } finally {
    $ErrorActionPreference = $prevEap
  }
}
& $venvPy -c "import onnxruntime, rapidocr_onnxruntime, numpy, PIL; print('依赖 OK')"
if ($LASTEXITCODE -ne 0) { Fail "依赖装完但导入失败。" }

# 用包里自带的 OCR 模型覆盖（保证版本一致，也避免首次运行再去下载）
$siteModels = Join-Path $venv 'Lib\site-packages\rapidocr_onnxruntime\models'
if (Test-Path $siteModels) {
  Copy-Item (Join-Path $here 'models\rapidocr\*.onnx') $siteModels -Force
  Info "OCR 模型已同步进 venv"
}

# ---------------------------------------------------------------- 4. 装插件进 profile
Step "4/5 把插件登记进 DSH profile"
$pluginDir = if ($PluginDir) { $PluginDir } else { Join-Path $DataDir 'plugin' }
$savedOldProfile = ''
$prevCfgPath = Join-Path $pluginDir 'omnivision.config.json'
if (Test-Path $prevCfgPath) {
  try {
    $prevObj0 = Get-Content $prevCfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($prevObj0.profile) { $savedOldProfile = [string]$prevObj0.profile }
  } catch {}
}
if (Test-Path $pluginDir) { Remove-Item $pluginDir -Recurse -Force }
Copy-Item (Join-Path $here 'plugin') $pluginDir -Recurse -Force
Get-ChildItem $pluginDir -Recurse -Filter '__pycache__' -Directory | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# 告诉插件数据目录与 venv python 路径（非默认盘符/复用 venv 时绝不回退 PATH），同时保留已有 profile
$cfgPath = Join-Path $pluginDir 'omnivision.config.json'
$cfgParent = Split-Path -Parent $cfgPath
if (-not (Test-Path $cfgParent)) { New-Item -ItemType Directory $cfgParent -Force | Out-Null }
$cfgObj = [pscustomobject]@{
  dataDir = $DataDir.Replace('\', '/')
  python  = $venvPy.Replace('\', '/')
}
if ($savedOldProfile) {
  $cfgObj | Add-Member -NotePropertyName profile -NotePropertyValue $savedOldProfile -Force
}
$cfgJson = $cfgObj | ConvertTo-Json -Depth 5
try {
  Write-Utf8NoBom $cfgPath $cfgJson
} catch {
  Fail "写入 $cfgPath 失败：$($_.Exception.Message)"
}
if (-not (Test-Path $cfgPath)) {
  Fail "写入自检失败：$cfgPath 未生成，请检查目录写权限。"
}
try {
  $cfgCheck = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
  if (-not $cfgCheck.dataDir -or -not $cfgCheck.python) {
    Fail "写入自检失败：$cfgPath 缺少 dataDir 或 python 字段。"
  }
} catch {
  Fail "写入自检失败：$cfgPath 不是合法 JSON（$($_.Exception.Message)）。"
}
Info "已写并校验配置：$cfgPath（dataDir=$($cfgCheck.dataDir) | python=$($cfgCheck.python)）"

$profileDir = Join-Path $DshHome "profiles\$Profile"
$pkgPath = Join-Path $profileDir 'package.json'
if (-not (Test-Path $pkgPath)) { Fail "找不到 profile 配置：$pkgPath" }
$pkg = Get-Content $pkgPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $pkg.dependencies) { $pkg | Add-Member -NotePropertyName dependencies -NotePropertyValue ([pscustomobject]@{}) -Force }
$link = 'link:' + ($pluginDir.Replace('\', '/'))
$pkg.dependencies | Add-Member -NotePropertyName $depName -NotePropertyValue $link -Force
if (-not $pkg.dsh) { Fail "profile 的 package.json 里没有 dsh 节点，DSH 版本可能太老。" }
$rawBundles = @($pkg.dsh.profile.bundles | Where-Object { $null -ne $_ -and "$_" -ne '' })
$dedupBundles = @()
$seenKeys = @{}
$hasDep = $false
foreach ($b in $rawBundles) {
  $key = if ($b -is [string]) { $b.Trim() } elseif ($b.name) { [string]$b.name } elseif ($b.id) { [string]$b.id } else { ($b | ConvertTo-Json -Compress) }
  if (-not $seenKeys.ContainsKey($key)) {
    $seenKeys[$key] = $true
    $dedupBundles += $b
  }
  if ($key -eq $depName) { $hasDep = $true }
}
if (-not $hasDep) {
  $dedupBundles += $depName
}
$pkg.dsh.profile.bundles = @($dedupBundles)
        $bakPath = $pkgPath + '.omnivision-bak'
        Copy-Item $pkgPath $bakPath -Force
        Write-Utf8NoBom $pkgPath ($pkg | ConvertTo-Json -Depth 30)
        $nodeCheck = Get-ChildItem (Join-Path $DshHome 'dsh-runtimes') -Recurse -Filter 'node.exe' -ErrorAction SilentlyContinue |
          Where-Object { $_.FullName -match '\\node\\bin\\node\.exe$' } | Select-Object -First 1
        $writeOk = $true
        if ($nodeCheck) {
          & $nodeCheck.FullName -e "JSON.parse(require('fs').readFileSync(process.argv[1],'utf8'))" $pkgPath | Out-Null
          if ($LASTEXITCODE -ne 0) { $writeOk = $false }
        }
        $pkgBytes = [System.IO.File]::ReadAllBytes($pkgPath)
        if ($pkgBytes.Length -ge 3 -and $pkgBytes[0] -eq 0xEF -and $pkgBytes[1] -eq 0xBB -and $pkgBytes[2] -eq 0xBF) { $writeOk = $false }
        if (-not $writeOk) {
          if (Test-Path $bakPath) { Copy-Item $bakPath $pkgPath -Force }
          Fail "profile 的 package.json 写入自检失败（BOM 或 JSON 损坏），已恢复备份 $bakPath。"
        }
Info "profile 已登记：$depName → $link"

$nodeExe = Get-ChildItem (Join-Path $DshHome 'dsh-runtimes') -Recurse -Filter 'node.exe' -ErrorAction SilentlyContinue |
  Where-Object { $_.FullName -match '\\node\\bin\\node\.exe$' } | Select-Object -First 1
$pnpmJs = Get-ChildItem (Join-Path $DshHome 'dsh-runtimes') -Recurse -Filter 'pnpm.cjs' -ErrorAction SilentlyContinue |
  Select-Object -First 1
if (-not $nodeExe -or -not $pnpmJs) { Fail "找不到 DSH 自带的 node/pnpm（$DshHome\dsh-runtimes）。DSH 版本可能太老。" }
Info "跑 pnpm install（第一次可能要几十秒）"
Push-Location $profileDir
try {
  & $nodeExe.FullName $pnpmJs.FullName install
  if ($LASTEXITCODE -ne 0) { Fail "pnpm install 失败。检查网络后重跑本脚本。" }
} finally {
  Pop-Location
}

# ---------------------------------------------------------------- 5. 完成
Step "5/5 完成"
Info "插件目录：$pluginDir"
Info "数据目录：$DataDir"
Info "Python  ：$venv"
Write-Host ""
Write-Host "请【完全退出 DSH】（托盘也要退），再重新打开。" -ForegroundColor Yellow
Write-Host "打开后随便问一句「用 vision_status 看看视觉层状态」验证。" -ForegroundColor Yellow
Write-Host ""
Write-Host "想换数据目录：重跑本脚本加 -DataDir 'D:\你的目录'" -ForegroundColor DarkGray
Write-Host "卸载：删掉 $DataDir，再从 profile 的 package.json 里去掉 $depName（或让 DSH 插件管理器移除）。" -ForegroundColor DarkGray
