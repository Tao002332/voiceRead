# 曼波环境补丁（幂等，可重复执行）
# 本机实际踩过的 4 个坑，全部固化在这里，重装环境后跑一遍即可。
# 用法： powershell -ExecutionPolicy Bypass -File scripts\patch_mambo_env.ps1
param(
    [string]$Root = ''
)
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'

if ([string]::IsNullOrWhiteSpace($Root)) {
    $Root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\mambo'))
}

$vpy = Join-Path $Root 'venv\Scripts\python.exe'
$sp = Join-Path $Root 'venv\Lib\site-packages'
$gsv = Join-Path $Root 'GPT-SoVITS'
if (-not (Test-Path $vpy)) { Write-Host "[ERROR] venv 不存在: $vpy" -ForegroundColor Red; exit 1 }
$idx = 'https://pypi.tuna.tsinghua.edu.cn/simple'
function Log($m) { Write-Host ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m) }

# ---------------------------------------------------------------- 坑 1：jieba_fast 需要 C 编译器
# 用纯 python 的 jieba 做同名垫片，中文分词照常工作
Log 'patch 1/4: jieba_fast shim'
$jf = Join-Path $sp 'jieba_fast'
New-Item -ItemType Directory -Force -Path $jf | Out-Null
[System.IO.File]::WriteAllText((Join-Path $jf '__init__.py'), @"
"""jieba_fast compatibility shim -> pure-python jieba."""
import jieba as _j
from jieba import *
from jieba import posseg, lcut, cut, initialize, setLogLevel, load_userdict
posseg = _j.posseg
__all__ = [n for n in dir(_j) if not n.startswith('_')]
"@, (New-Object System.Text.UTF8Encoding($false)))
[System.IO.File]::WriteAllText((Join-Path $jf 'posseg.py'), @"
"""jieba_fast.posseg compatibility shim -> jieba.posseg"""
from jieba.posseg import *
from jieba.posseg import cut, lcut, dt
"@, (New-Object System.Text.UTF8Encoding($false)))

# ---------------------------------------------------------------- 坑 2：缺依赖
Log 'patch 2/4: extra deps (matplotlib / resampy)'
& $vpy -m pip install --quiet matplotlib resampy -i $idx 2>$null

# ---------------------------------------------------------------- 坑 3：transformers 版本
# 新版 transformers 要求 torch>=2.6 才允许 torch.load，本机 torch 2.5.1+cu121
Log 'patch 3/4: transformers==4.51.3 (torch 2.5 compatible)'
$tv = & $vpy -c "import transformers;print(transformers.__version__)" 2>$null
if ($tv -notlike '4.51*') {
    & $vpy -m pip install --quiet "transformers==4.51.3" -i $idx 2>$null
    $tv = & $vpy -c "import transformers;print(transformers.__version__)" 2>$null
}
Log "       transformers = $tv"

# ---------------------------------------------------------------- 坑 4：语言识别强制下载 126MB
# fbaipublicfiles 在国内被阻断；split_lang 硬编码 model="full"，改成本地内置 lite 模型
Log 'patch 4/4: fast-langdetect -> lite (offline)'
$det = Join-Path $sp 'split_lang\detect_lang\detector.py'
if (Test-Path $det) {
    $t = [System.Text.Encoding]::UTF8.GetString([System.IO.File]::ReadAllBytes($det))
    if ($t -match 'model="full"') {
        $t = $t.Replace('model="full"', 'model="lite"')
        [System.IO.File]::WriteAllText($det, $t, (New-Object System.Text.UTF8Encoding($false)))
        Log '       split_lang patched'
    } else { Log '       split_lang already lite' }
}
$ls = Join-Path $gsv 'GPT_SoVITS\text\LangSegmenter\langsegmenter.py'
if (Test-Path $ls) {
    $t = [System.Text.Encoding]::UTF8.GetString([System.IO.File]::ReadAllBytes($ls))
    if ($t -notmatch 'model="lite"') {
        $t = $t.Replace('LangDetectConfig(cache_dir=', 'LangDetectConfig(model="lite", cache_dir=')
        [System.IO.File]::WriteAllText($ls, $t, (New-Object System.Text.UTF8Encoding($false)))
        Log '       langsegmenter patched'
    } else { Log '       langsegmenter already lite' }
}

# ---------------------------------------------------------------- 校验
Log 'verify ...'
& $vpy -c "import jieba_fast.posseg as p; import matplotlib, resampy, transformers; print('deps ok ->', '/'.join(w for w,f in p.lcut('曼波语音测试')), '| transformers', transformers.__version__)"
Log 'done'
