# 下载 Qwen3-ASR-1.7B 的 GGUF（模型 2.1GB + mmproj 0.35GB）。已存在的文件跳过。
# 目录和 daemon/config.py 的默认值一致；放别处就传 -Dir，再到管理面板里改路径。
#
# llama.cpp 不在这里下：去 https://github.com/ggml-org/llama.cpp/releases 取 Windows
# Vulkan 版，解压后把 llama-server.exe 的路径填进管理面板（默认 D:\models\llama.cpp\bin）。
param([string]$Dir = 'D:\models\qwen3-asr-1.7b')
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path $Dir | Out-Null

# 魔搭在国内快；Hugging Face 兜底
$bases = @(
  'https://modelscope.cn/models/ggml-org/Qwen3-ASR-1.7B-GGUF/resolve/master',
  'https://huggingface.co/ggml-org/Qwen3-ASR-1.7B-GGUF/resolve/main'
)

foreach ($file in 'Qwen3-ASR-1.7B-Q8_0.gguf', 'mmproj-Qwen3-ASR-1.7B-Q8_0.gguf') {
  $dest = Join-Path $Dir $file
  if (Test-Path $dest) { Write-Host "[skip] $dest"; continue }
  $ok = $false
  foreach ($b in $bases) {
    Write-Host "[get] $b/$file"
    # 先写 .part，下完再改名：半截文件不会被当成已下载而跳过
    & curl.exe -L --fail --retry 2 --connect-timeout 20 -C - -o "$dest.part" "$b/$file"
    if ($LASTEXITCODE -eq 0) { Move-Item -Force "$dest.part" $dest; $ok = $true; break }
  }
  if (-not $ok) { throw "download failed: $file" }
}
Get-ChildItem $Dir -Filter *.gguf |
  Select-Object Name, @{n='MB';e={[math]::Round($_.Length/1MB)}} | Format-Table -AutoSize
