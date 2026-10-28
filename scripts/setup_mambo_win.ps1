# 一键部署 GPT-SoVITS 运行环境（Windows，NVIDIA）
# 用法： powershell -ExecutionPolicy Bypass -File scripts\setup_mambo_win.ps1
param(
    [string]$Root = '',
    [string]$Py = ''
)
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ProgressPreference = 'SilentlyContinue'

# 默认按「脚本所在目录的上一级 / mambo」定位，整套拷到别的机器也不用改脚本
if ([string]::IsNullOrWhiteSpace($Root)) {
    $Root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\mambo'))
}
# 找 Python：显式 -Py > 环境变量 VOICEREAD_PY > 常见路径 > PATH
if ([string]::IsNullOrWhiteSpace($Py)) {
    $cands = @($env:VOICEREAD_PY, 'E:\python\python.exe') | Where-Object { $_ -and (Test-Path $_) }
    if ($cands) { $Py = $cands[0] }
}
if ([string]::IsNullOrWhiteSpace($Py)) {
    foreach ($c in 'python', 'py') {
        $g = Get-Command $c -ErrorAction SilentlyContinue
        if ($g) { $Py = $g.Source; break }
    }
}
if ([string]::IsNullOrWhiteSpace($Py)) {
    Write-Host '[ERROR] 找不到 Python，请用 -Py 指定 python.exe（建议 3.10）' -ForegroundColor Red
    exit 1
}
Write-Host "Python : $Py"
Write-Host "Root   : $Root"
$mirror = 'https://hf-mirror.com'
$repo = 'lj1995/GPT-SoVITS'

function Log($msg) { $ts = Get-Date -Format 'HH:mm:ss'; Write-Host "[$ts] $msg" }

New-Item -ItemType Directory -Force -Path $Root | Out-Null
$gsv = Join-Path $Root 'GPT-SoVITS'

# ---------------------------------------------------------------- 1. 源码
if (-not (Test-Path (Join-Path $gsv 'api_v2.py'))) {
    Log '下载 GPT-SoVITS 源码 ...'
    $zip = Join-Path $Root 'gpt-sovits.zip'
    curl.exe -L --retry 3 --silent --show-error -o $zip 'https://codeload.github.com/RVC-Boss/GPT-SoVITS/zip/refs/heads/main'
    if (-not (Test-Path $zip)) { Log '源码下载失败'; exit 1 }
    Log ('zip 大小 ' + [math]::Round((Get-Item $zip).Length / 1MB, 1) + ' MB，解压 ...')
    Expand-Archive -Path $zip -DestinationPath $Root -Force
    if (Test-Path (Join-Path $Root 'GPT-SoVITS-main')) {
        if (Test-Path $gsv) { Remove-Item $gsv -Recurse -Force }
        Rename-Item (Join-Path $Root 'GPT-SoVITS-main') 'GPT-SoVITS'
    }
    Remove-Item $zip -Force -ErrorAction SilentlyContinue
}
Log ('源码就绪: ' + (Test-Path (Join-Path $gsv 'api_v2.py')))

# ---------------------------------------------------------------- 2. 预训练模型
$files = @(
    'chinese-hubert-base/config.json',
    'chinese-hubert-base/preprocessor_config.json',
    'chinese-hubert-base/pytorch_model.bin',
    'chinese-roberta-wwm-ext-large/config.json',
    'chinese-roberta-wwm-ext-large/tokenizer.json',
    'chinese-roberta-wwm-ext-large/pytorch_model.bin',
    'gsv-v2final-pretrained/s1bert25hz-5kh-longer-epoch=12-step=369668.ckpt',
    'gsv-v2final-pretrained/s2G2333k.pth',
    'gsv-v2final-pretrained/s2D2333k.pth'
)
$pm = Join-Path $gsv 'GPT_SoVITS\pretrained_models'
foreach ($f in $files) {
    $dest = Join-Path $pm $f
    $dir = Split-Path $dest -Parent
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    if ((Test-Path $dest) -and ((Get-Item $dest).Length -gt 1024)) {
        Log ("skip    $f")
        continue
    }
    $url = "$mirror/$repo/resolve/main/$f"
    Log "fetch   $f"
    curl.exe -L --retry 3 --silent --show-error -o $dest $url
    if ((Test-Path $dest) -and ((Get-Item $dest).Length -gt 1024)) {
        Log ('ok      ' + $f + '  ' + [math]::Round((Get-Item $dest).Length / 1MB, 1) + 'MB')
    } else {
        Log "FAILED  $f"
    }
}

# ---------------------------------------------------------------- 3. venv + torch
$venv = Join-Path $Root 'venv'
if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) {
    Log '创建 venv ...'
    & $Py -m venv $venv
}
$vpy = Join-Path $venv 'Scripts\python.exe'
Log '升级 pip ...'
& $vpy -m pip install --quiet --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple

$hasTorch = & $vpy -c "import torch;print(torch.__version__)" 2>$null
if (-not $hasTorch) {
    Log '安装 torch (cu121) ...'
    & $vpy -m pip install --quiet torch torchaudio --index-url https://download.pytorch.org/whl/cu121
    $hasTorch = & $vpy -c "import torch;print(torch.__version__)" 2>$null
    if (-not $hasTorch) {
        Log '官方源失败，回退清华源（可能为 CPU 版本）...'
        & $vpy -m pip install --quiet torch torchaudio -i https://pypi.tuna.tsinghua.edu.cn/simple
        $hasTorch = & $vpy -c "import torch;print(torch.__version__)" 2>$null
    }
}
Log "torch: $hasTorch"

# ---------------------------------------------------------------- 4. GPT-SoVITS 依赖
Log '安装 GPT-SoVITS requirements（较慢）...'
Push-Location $gsv
& $vpy -m pip install --quiet -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
$rc = $LASTEXITCODE
Pop-Location
Log "requirements exit=$rc"

# ---------------------------------------------------------------- 5. 自检
Log '===== 环境自检 ====='
& $vpy -c "import torch,sys;print('torch',torch.__version__);print('cuda_available',torch.cuda.is_available());print('device',torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU')"
& $vpy -c "import transformers,librosa,ffmpeg;print('transformers',transformers.__version__)" 2>$null
Log '===== 完成 ====='
Log "GPT-SoVITS: $gsv"
Log "venv:       $vpy"
Log '下一步：准备 3~10 秒曼波参考音频，然后启动 api_v2.py（见 docs/曼波模型部署.md）'
