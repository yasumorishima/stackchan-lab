# スタックちゃん本体の接続先を 1 コマンドで切り替える。
#
#   .\switch-backend.ps1 ours      # 自前サーバー（RPi5）へ向ける
#   .\switch-backend.ps1 official  # 出荷時（公式 tenclass）サーバーへ戻す
#   .\switch-backend.ps1 status    # 現在の向き先を表示するだけ（書き込みなし）
#
# 前提: 本体を USB で接続（COM3）。RPi5 に ~/nvs_toggle.py と ~/nvs_tool/ があること。
#
# ⚠️ esptool を使うので本体は再起動し、AI エージェントは閉じる（status でも同じ）。
# ⚠️ 実行後、本体の画面で「AI エージェント」を開くと OTA が走って切替が定着する
#    （websocket/url は OTA 応答が毎回書き直すので、触るのは wifi/ota_url だけでよい）。
# ✅ 直前に必ず実機から吸い出し直すので、user が本体でした設定の巻き戻りは起きない。
param(
    [Parameter(Mandatory = $true)][ValidateSet("ours", "official", "status")][string]$Mode,
    [string]$Port = "COM3",
    [string]$Rpi = "pi@raspberrypi.local"
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$esptool = Join-Path $here "esptool.exe"
$rpi = $Rpi
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
# ダンプには Wi-Fi/MQTT の資格情報が平文で入る＝ローカル一時ファイルは最後に必ず消す
$localDump = Join-Path $env:TEMP "nvs_now_$stamp.bin"
$localOut = Join-Path $env:TEMP "nvs_out_$stamp.bin"
$localVerify = Join-Path $env:TEMP "nvs_verify_$stamp.bin"

function Cleanup {
    foreach ($f in @($localDump, $localOut, $localVerify)) {
        if (Test-Path $f) { Remove-Item $f -Force }
    }
}

try {
    Write-Host "== 1/6 実機から NVS を吸い出す（本体は再起動します）"
    & $esptool --port $Port read-flash 0x9000 0x4000 $localDump
    if ($LASTEXITCODE -ne 0) { throw "read-flash 失敗（本体の USB 接続と $Port を確認）" }

    Write-Host "== 2/6 RPi5 へ転送（chmod 600 の専用ディレクトリ）"
    ssh $rpi "bash -lc 'mkdir -p ~/nvs_switch; chmod 700 ~/nvs_switch'"
    if ($LASTEXITCODE -ne 0) { throw "RPi5 への ssh 失敗" }
    scp $localDump "${rpi}:nvs_switch/nvs_now.bin"
    if ($LASTEXITCODE -ne 0) { throw "scp 失敗" }
    # 吸い出したての実機イメージを復元点として時刻付きで残す
    ssh $rpi "bash -lc 'chmod 600 ~/nvs_switch/nvs_now.bin; cp -p ~/nvs_switch/nvs_now.bin ~/nvs_switch/before_$stamp.bin'"

    if ($Mode -eq "status") {
        Write-Host "== 現在の向き先"
        ssh $rpi "bash -lc 'timeout 30 python3 ~/nvs_toggle.py status ~/nvs_switch/nvs_now.bin'"
        return
    }

    Write-Host "== 3/6 NVS を編集（触るのは wifi/ota_url だけ）"
    if ($Mode -eq "ours") {
        # RPi5 の LAN IP は DHCP 動的なので毎回その場で取る（固定書きしない）
        $ip = ((ssh $rpi "hostname -I").Trim() -split "\s+") |
            Where-Object { $_ -like "192.168.*" } | Select-Object -First 1
        if (-not $ip) { throw "RPi5 の LAN IP (192.168.x.x) が取れない" }
        $url = "http://${ip}:8000/xiaozhi/ota/"
        Write-Host "   向け先 URL = $url"
        ssh $rpi "bash -lc 'set -e; timeout 30 python3 ~/nvs_toggle.py to-ours ~/nvs_switch/nvs_now.bin ~/nvs_switch/nvs_out.bin $url; chmod 600 ~/nvs_switch/nvs_out.bin'"
    }
    else {
        ssh $rpi "bash -lc 'set -e; timeout 30 python3 ~/nvs_toggle.py to-official ~/nvs_switch/nvs_now.bin ~/nvs_switch/nvs_out.bin; chmod 600 ~/nvs_switch/nvs_out.bin'"
    }
    if ($LASTEXITCODE -ne 0) { throw "NVS 編集失敗（書き込みは行っていない）" }

    Write-Host "== 4/6 Espressif 公式ツールで検証（通らなければ書き込まない）"
    $check = ssh $rpi "bash -lc 'timeout 30 python3 ~/nvs_tool/nvs_tool.py ~/nvs_switch/nvs_out.bin -i -d none --color never 2>&1'"
    $check | Write-Host
    if (($check | Select-String "CRC32: OK").Count -lt 2) {
        throw "公式ツールの整合性検査を通らない（書き込みは行っていない）"
    }

    Write-Host "== 5/6 実機へ書き込み"
    scp "${rpi}:nvs_switch/nvs_out.bin" $localOut
    if ($LASTEXITCODE -ne 0) { throw "scp（回収）失敗" }
    & $esptool --port $Port write-flash 0x9000 $localOut
    if ($LASTEXITCODE -ne 0) { throw "write-flash 失敗" }

    Write-Host "== 6/6 読み戻して一致確認"
    & $esptool --port $Port read-flash 0x9000 0x4000 $localVerify
    if ($LASTEXITCODE -ne 0) { throw "検証用 read-flash 失敗" }
    $h1 = (Get-FileHash $localOut -Algorithm MD5).Hash
    $h2 = (Get-FileHash $localVerify -Algorithm MD5).Hash
    if ($h1 -ne $h2) { throw "読み戻しが書込イメージと一致しない（md5 $h1 vs $h2）" }

    Write-Host ""
    Write-Host "✅ 切替完了（読み戻し一致 md5 $h1）"
    if ($Mode -eq "ours") {
        Write-Host "本体で「AI エージェント」を開くと OTA が自前サーバーへ走り、会話がこちらに来ます。"
    }
    else {
        Write-Host "本体で「AI エージェント」を開くと OTA が公式へ走り、出荷時の会話に戻ります。"
    }
    Write-Host "（復元点: RPi5 ~/nvs_switch/before_$stamp.bin）"
}
finally {
    Cleanup
}
