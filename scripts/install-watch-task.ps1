<#
.SYNOPSIS
    tradeagent watch'u Windows Görev Zamanlayıcı'ya ekler: oturum açınca başlar, çökerse 1 dk sonra yeniden başlar.

.EXAMPLE
    # Bildirim modu (emir yalnızca sizin execute + ONAY onayınızla gider)
    .\scripts\install-watch-task.ps1 -Symbols "BTC/USDT,ETH/USDT"

.EXAMPLE
    # Binance Demo Trading hesabında tam otomatik (config.yaml: mode: live, crypto.exchange: binanceusdm, crypto.testnet: true)
    .\scripts\install-watch-task.ps1 -Symbols "BTC/USDT:USDT,ETH/USDT:USDT" -Live -DemoAuto

.EXAMPLE
    # Görevi kaldır
    .\scripts\install-watch-task.ps1 -Remove
#>
param(
    [string]$Symbols,
    [string]$Tf = "15m",
    [string]$Htf = "1d",
    [switch]$Live,
    [switch]$DemoAuto,
    [switch]$Remove,
    [string]$TaskName = "tradeagent-watch",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $PSScriptRoot

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Görev kaldırıldı: $TaskName"
    } else {
        Write-Host "Görev zaten yok: $TaskName"
    }
    return
}

if (-not $Symbols) { throw "-Symbols gerekli, örn. -Symbols 'BTC/USDT,ETH/USDT'" }
foreach ($value in @($Symbols, $Tf, $Htf)) {
    if ($value -notmatch '^[A-Za-z0-9/:.,_-]+$') { throw "Geçersiz karakter: $value" }
}
$config = Join-Path $project "config.yaml"
if (-not (Test-Path $config)) { throw "config.yaml bulunamadı: $config (config.example.yaml'ı kopyalayın)" }
if (-not $Python) { $Python = (Get-Command python -ErrorAction Stop).Source }

$logDir = Join-Path $project "data"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "watch.log"

$watchArgs = "watch --symbols $Symbols --tf $Tf --htf $Htf"
if ($Live) { $watchArgs += " --live" }
if ($DemoAuto) { $watchArgs += " --demo-auto" }
$command = "cd /d `"$project`" && `"$Python`" -m tradeagent.cli --config `"$config`" $watchArgs >> `"$log`" 2>&1"

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c $command" -WorkingDirectory $project
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "tradeagent 7/24 izleme ($watchArgs)" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host "Görev kuruldu ve başlatıldı: $TaskName"
Write-Host "  Komut : tradeagent $watchArgs"
Write-Host "  Kayıt : $log"
Write-Host "Bilgisayar uykuya geçerse izleme durur; uyku ayarını kapatmayı unutmayın."
