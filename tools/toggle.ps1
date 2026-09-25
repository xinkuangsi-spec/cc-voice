# cc-voice 开关：没在跑就启动，跑着就关闭。
#
# 判定用命名互斥量而不是扫进程命令行 —— 和守护进程自己做单实例判断用的是同一个
# 权威源。扫命令行会误判：任何提到过这个路径的编辑器/终端/脚本都会被当成守护进程。
#
# 全程静默，不弹窗：屏幕上那枚悬浮岛本身就是状态指示 —— 出现即已启动，消失即已关闭。

$ErrorActionPreference = 'SilentlyContinue'
$Root = Split-Path -Parent $PSScriptRoot

function Test-Running {
    try {
        $m = [System.Threading.Mutex]::OpenExisting('Local\cc-voice-daemon')
        $m.Dispose(); return $true
    } catch { return $false }
}

if (Test-Running) {
    # 先走面板的退出接口：守护进程会顺手关掉 llama-server，显存立刻释放
    $port = 8731
    $cfg = Join-Path $Root 'config.json'
    if (Test-Path -LiteralPath $cfg) {
        $p = (Get-Content -LiteralPath $cfg -Raw -Encoding utf8 | ConvertFrom-Json).panel_port
        if ($p) { $port = $p }
    }
    Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$port/api/control" `
                      -Body '{"action":"quit"}' -TimeoutSec 2 | Out-Null
    for ($i = 0; $i -lt 30 -and (Test-Running); $i++) { Start-Sleep -Milliseconds 100 }
    if (Test-Running) {
        # 面板没响应才强杀；它拉起的 llama-server 不会跟着死，要一起收掉。
        # venv 里的 pythonw.exe 只是启动器，真正的解释器是它的子进程、路径在
        # venv 之外，所以按「python 进程 + 命令行带本仓库的 ccvoice.py」认
        $script = Join-Path $Root 'daemon\ccvoice.py'
        $daemons = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
            Where-Object { $_.CommandLine -like "*$script*" }
        foreach ($d in $daemons) {
            Get-CimInstance Win32_Process -Filter "ParentProcessId=$($d.ProcessId) AND Name='llama-server.exe'" |
                ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
            Stop-Process -Id $d.ProcessId -Force
        }
    }
    exit 0
}

$py = Join-Path $Root '.venv\Scripts\pythonw.exe'
$script = Join-Path $Root 'daemon\ccvoice.py'
if ((Test-Path -LiteralPath $py) -and (Test-Path -LiteralPath $script)) {
    Start-Process -FilePath $py -ArgumentList "`"$script`" --announce" `
                  -WorkingDirectory (Join-Path $Root 'daemon') -WindowStyle Hidden
}
