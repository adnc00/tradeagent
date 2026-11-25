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
    # Binance'taki en hacimli 100 USDT-M vadeliyi demo hesapta tam otomatik izle
    .\scripts\install-watch-task.ps1 -Universe -Live -DemoAuto

.EXAMPLE
    # Görevi kaldır
    .\scripts\install-watch-task.ps1 -Remove
#>
param(
    [string]$Symbols,
    [switch]$Universe,
    [int]$Top = -1,
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

if (-not $Symbols -and -not $Universe) { throw "-Symbols veya -Universe gerekli, örn. -Symbols 'BTC/USDT,ETH/USDT'" }
if ($Symbols -and $Universe) { throw "-Symbols ve -Universe birlikte verilemez" }
foreach ($value in @($Symbols, $Tf, $Htf) | Where-Object { $_ }) {
    if ($value -notmatch '^[A-Za-z0-9/:.,_-]+$') { throw "Geçersiz karakter: $value" }
}
$config = Join-Path $project "config.yaml"
if (-not (Test-Path $config)) { throw "config.yaml bulunamadı: $config (config.example.yaml'ı kopyalayın)" }
if (-not $Python) { $Python = (Get-Command python -ErrorAction Stop).Source }

$logDir = Join-Path $project "data"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "watch.log"

if ($Universe) { $watchArgs = "watch --universe" } else { $watchArgs = "watch --symbols $Symbols" }
if ($Top -ge 0) { $watchArgs += " --top $Top" }
$watchArgs += " --tf $Tf --htf $Htf"
if ($Live) { $watchArgs += " --live" }
if ($DemoAuto) { $watchArgs += " --demo-auto" }
$command = "cd /d `"$project`" && `"$Python`" -m tradeagent.cli --config `"$config`" $watchArgs >> `"$log`" 2>&1"

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c $command" -WorkingDirectory $project
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

# A running instance keeps its old arguments (MultipleInstances IgnoreNew): stop it first.
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "tradeagent 7/24 izleme ($watchArgs)" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host "Görev kuruldu ve başlatıldı: $TaskName"
Write-Host "  Komut : tradeagent $watchArgs"
Write-Host "  Kayıt : $log"
Write-Host "Bilgisayar uykuya geçerse izleme durur; uyku ayarını kapatmayı unutmayın."
