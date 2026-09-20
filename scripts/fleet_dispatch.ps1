# GitHub 예약 실행(cron)은 10분 간격으로 걸어도 실제로는 1~3시간씩 밀립니다 (2026-09-20 관측: 3시간 이상 공백).
# 함대 실행기는 catch-up 이라 그날 안에는 처리되지만, "블로그마다 정해진 시각" 은 흐려집니다.
#
# 이 스크립트는 PC 가 켜져 있을 때 10분마다 돌면서:
#   1) git pull 로 최신 이력을 받고
#   2) 지금 돌 차례인 블로그가 있는지 로컬에서 확인한 뒤 (Actions 분을 쓰지 않음)
#   3) 있으면 GitHub 워크플로(fleet.yml)를 직접 dispatch 합니다.
# 실제 글 작성은 GitHub 에서 하므로 PC 는 '알람 시계' 역할만 합니다. PC 가 꺼져 있으면 GitHub cron 이 느리게라도 처리합니다.
#
# 등록 (한 번만, PowerShell):
#   $a = New-ScheduledTaskAction -Execute powershell.exe -Argument '-NoProfile -ExecutionPolicy Bypass -File "C:\Users\qmaec\Documents\trend-blog\scripts\fleet_dispatch.ps1"'
#   $t = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 10)
#   $s = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
#   Register-ScheduledTask -TaskName TrendBlogFleetDispatch -Action $a -Trigger $t -Settings $s

$ErrorActionPreference = "Continue"
Set-Location (Join-Path $PSScriptRoot "..")
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:Path += ";C:\Program Files\GitHub CLI"

$logDir = "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir ("fleet-dispatch-" + (Get-Date -Format "yyyyMMdd") + ".log")
function Log($t) { "[$(Get-Date -Format 'HH:mm:ss')] $t" | Out-File $log -Append -Encoding utf8 }

git pull -q --rebase --autostash origin main 2>&1 | Out-Null

$due = & python -m src.fleet_run --dry-run 2>$null | Select-String -Pattern '^\s+·' | ForEach-Object { $_.Line.Trim() }
if (-not $due) { Log "대상 없음"; exit 0 }

Log ("대상: " + ($due -join " | "))
# 이미 돌고 있거나 대기 중인 실행이 있으면 또 보내지 않습니다.
$active = & gh run list --workflow=fleet.yml --limit 3 --json status --jq '[.[] | select(.status != "completed")] | length' 2>$null
if ([int]$active -gt 0) { Log "이미 실행 중/대기 중 ($active) — 건너뜀"; exit 0 }

& gh workflow run fleet.yml 2>&1 | Out-File $log -Append -Encoding utf8
Log "dispatch 요청 (exit=$LASTEXITCODE)"
exit 0
