# 安装 GPT-SoVITS 推理依赖（跳过需要 MSVC 编译的日/韩/粤语包）
# 用法： powershell -ExecutionPolicy Bypass -File scripts\install_gsv_deps.ps1
param(
    [string]$Root = ''
)
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ProgressPreference = 'SilentlyContinue'

if ([string]::IsNullOrWhiteSpace($Root)) {
    $Root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\mambo'))
}

$vpy = Join-Path $Root 'venv\Scripts\python.exe'
if (-not (Test-Path $vpy)) { Write-Host "[ERROR] venv 不存在: $vpy" -ForegroundColor Red; exit 1 }
$idx = 'https://pypi.tuna.tsinghua.edu.cn/simple'

function Log($m) { Write-Host ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m) }

# 中文朗读必需的包（不含 pyopenjtalk / g2pk2 / ko_pron / ToJyutping）
$pkgs = @(
    'numpy<2.0',
    'scipy',
    'librosa==0.10.2',
    'numba',
    'ffmpeg-python',
    'tqdm',
    'cn2an',
    'pypinyin',
    'g2p_en',
    'torchaudio',
    'sentencepiece',
    'transformers>=4.51,<5',
    'peft<0.18.0',
    'chardet',
    'PyYAML',
    'psutil',
    'jieba_fast',
    'jieba',
    'split-lang',
    'fast_langdetect>=0.3.1',
    'wordsegment',
    'rotary_embedding_torch',
    'fastapi[standard]>=0.115.2',
    'x_transformers',
    'torchmetrics<=1.5',
    'pydantic<=2.10.6',
    'ctranslate2>=4.0,<5',
    'av>=11',
    'onnxruntime-gpu',
    'funasr>=1.3.7',
    'modelscope',
    'pytorch-lightning>=2.4',
    'opencc-python-reimplemented'
)

$failed = @()
foreach ($p in $pkgs) {
    Log "install $p"
    & $vpy -m pip install --quiet --no-warn-script-location $p -i $idx 2>$null
    if ($LASTEXITCODE -ne 0) {
        Log "  FAIL $p"
        $failed += $p
    }
}

Log '===== 关键模块导入检查 ====='
$probe = @'
mods = ["numpy","scipy","librosa","soundfile","numba","torch","torchaudio","transformers",
        "sentencepiece","jieba","pypinyin","cn2an","yaml","fastapi","uvicorn","pydantic",
        "peft","x_transformers","torchmetrics","ctranslate2","av","onnxruntime","funasr",
        "modelscope","opencc","g2p_en","split_lang","wordsegment"]
ok, bad = [], []
for m in mods:
    try:
        __import__(m); ok.append(m)
    except Exception as e:
        bad.append(f"{m}: {type(e).__name__}")
print("OK  ", len(ok), "->", " ".join(ok))
print("BAD ", len(bad), "->", " ".join(bad))
'@
$probePath = Join-Path $Root '_probe.py'
Set-Content -Path $probePath -Value $probe -Encoding UTF8
& $vpy $probePath
Remove-Item $probePath -ErrorAction SilentlyContinue

Log '===== 完成 ====='
if ($failed.Count -gt 0) {
    Log ('失败包: ' + ($failed -join ', '))
} else {
    Log '全部安装成功'
}
