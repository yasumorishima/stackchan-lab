<#
.SYNOPSIS
    M5 スタックちゃん（M5STACK-K151 / CoreS3）へ公式ファームウェアを USB 書き込みする。

.DESCRIPTION
    M5Burner が使用する公開 API からファームウェア一覧を取得し、指定バージョン
    （既定は最新）のバイナリをダウンロードして esptool で書き込みます。
    M5Burner（GUI）も Python も不要です。

    既定では一覧表示のみで、書き込みは -Flash を明示したときだけ実行します。

.PARAMETER Port
    シリアルポート。例: COM3

.PARAMETER Version
    書き込むバージョン。例: V1.4.4。省略時は published な最新版。

.PARAMETER EsptoolPath
    esptool.exe のパス。省略時はカレント配下から探し、無ければ GitHub から取得します。

.PARAMETER Flash
    実際に書き込む。指定しない場合は取得と検証のみ（ドライラン）。

.EXAMPLE
    # 一覧とダウンロードだけ（書き込まない）
    .\flash-official-firmware.ps1 -Port COM3

.EXAMPLE
    # 最新版を書き込む
    .\flash-official-firmware.ps1 -Port COM3 -Flash

.NOTES
    フラッシュ全体を書き換えるため NVS（Wi-Fi 設定等）は初期化されます。
    サーボのゼロ点はサーボ本体側に保持されるため失われません。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Port,
    [string]$Version,
    [string]$EsptoolPath,
    [switch]$Flash
)

$ErrorActionPreference = 'Stop'

$ApiUrl      = 'https://m5burner-api.m5stack.com/api/firmware'
$CdnBase     = 'https://m5burner-cdn.m5stack.com/firmware'
$FirmwareKey = 'StackChan-UserDemo'   # 出荷時ファームウェア（author: M5Stack）
$WorkDir     = Join-Path $PSScriptRoot 'work'

New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

Write-Host '==> ファームウェア一覧を取得' -ForegroundColor Cyan
$all   = Invoke-RestMethod -Uri $ApiUrl -TimeoutSec 60
$entry = $all | Where-Object { $_.name -eq $FirmwareKey } | Select-Object -First 1
if (-not $entry) { throw "ファームウェア '$FirmwareKey' が一覧に見つかりません。" }

$published = @($entry.versions | Where-Object { $_.published })
if ($published.Count -eq 0) { throw '公開済みバージョンがありません。' }

$published | ForEach-Object { '    {0,-10} {1}  {2}' -f $_.version, $_.published_at, $_.file }

$target = if ($Version) {
    $published | Where-Object { $_.version -eq $Version } | Select-Object -First 1
} else {
    $published | Select-Object -Last 1
}
if (-not $target) { throw "バージョン '$Version' が見つかりません。" }

Write-Host ("==> 対象: {0} ({1})" -f $target.version, $target.published_at) -ForegroundColor Cyan

$binPath = Join-Path $WorkDir ("stackchan-{0}.bin" -f $target.version)
if (Test-Path $binPath) {
    Write-Host "    既にダウンロード済み: $binPath"
} else {
    Write-Host "    ダウンロード中..."
    Invoke-WebRequest -Uri "$CdnBase/$($target.file)" -OutFile $binPath -TimeoutSec 600
}

# マージ済みフルイメージであること（先頭が ESP イメージのマジックナンバー 0xE9）を確認
$head = [System.IO.File]::ReadAllBytes($binPath)[0]
if ($head -ne 0xE9) {
    throw ("先頭バイトが 0x{0:X2} です（0xE9 を期待）。イメージが壊れている可能性があります。" -f $head)
}
Write-Host ("    OK: {0} bytes, magic=0xE9" -f (Get-Item $binPath).Length) -ForegroundColor Green

# esptool を用意する
if (-not $EsptoolPath) {
    $found = Get-ChildItem -Path $WorkDir -Recurse -Filter 'esptool.exe' -ErrorAction SilentlyContinue |
             Select-Object -First 1
    if ($found) {
        $EsptoolPath = $found.FullName
    } else {
        Write-Host '==> esptool を取得（単体実行ファイル、Python 不要）' -ForegroundColor Cyan
        $rel = Invoke-RestMethod -Uri 'https://api.github.com/repos/espressif/esptool/releases/latest' `
                                 -Headers @{ 'User-Agent' = 'stackchan-lab' } -TimeoutSec 60
        $asset = $rel.assets | Where-Object { $_.name -match 'windows-amd64' } | Select-Object -First 1
        if (-not $asset) { throw 'esptool の Windows バイナリが見つかりません。' }
        $zip = Join-Path $WorkDir $asset.name
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zip -TimeoutSec 900
        Expand-Archive -Path $zip -DestinationPath (Join-Path $WorkDir 'esptool') -Force
        $EsptoolPath = (Get-ChildItem -Path (Join-Path $WorkDir 'esptool') -Recurse -Filter 'esptool.exe' |
                        Select-Object -First 1).FullName
    }
}
Write-Host "    esptool: $EsptoolPath"

Write-Host '==> チップと通信できるか確認（読み取りのみ）' -ForegroundColor Cyan
& $EsptoolPath --port $Port flash-id
if ($LASTEXITCODE -ne 0) {
    throw "チップと通信できません。リセットボタンを約 2 秒長押しして緑色 LED が点灯したら離し、ダウンロードモードに入れて再実行してください。"
}

if (-not $Flash) {
    Write-Host ''
    Write-Host '取得と検証まで完了しました（書き込みは行っていません）。' -ForegroundColor Yellow
    Write-Host '書き込むには -Flash を付けて再実行してください。'
    return
}

Write-Host '==> 書き込み（途中で USB を抜かないこと）' -ForegroundColor Cyan
& $EsptoolPath --port $Port write-flash 0x0 $binPath
if ($LASTEXITCODE -ne 0) { throw '書き込みに失敗しました。ダウンロードモードに入れて再実行してください。' }

Write-Host ''
Write-Host ("書き込み完了: {0}" -f $target.version) -ForegroundColor Green
Write-Host '起動ログの "app_init: App version:" 行でバージョンを確認してください。'
