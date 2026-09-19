# PC 에서 함대를 돌리는 대안 (GitHub Actions 분 한도를 넘길 때).
# Windows 작업 스케줄러에 10분 간격으로 등록:
#   $a = New-ScheduledTaskAction -Execute powershell.exe -Argument '-NoProfile -ExecutionPolicy Bypass -File "C:\Users\qmaec\Documents\trend-blog\scripts\fleet_local.ps1"'
#   $t = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 10)
#   $s = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1)
#   Register-ScheduledTask -TaskName TrendBlogFleet -Action $a -Trigger $t -Settings $s
#
# GitHub Actions 의 fleet.yml 과 **동시에 켜지 마세요** — 같은 블로그를 같은 날 두 번 씁니다.
# PC 로 옮길 때는 fleet.yml 의 schedule 블록을 지우고 푸시하세요.
#
# 이 스크립트는 실행 전에 git pull, 실행 후 data/ 를 커밋·푸시해서 GitHub 쪽 주간 보고·관리 에이전트가
# 같은 이력을 보게 합니다.

$ErrorActionPreference = "Continue"
Set-Location (Join-Path $PSScriptRoot "..")
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$logDir = "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir ("fleet-" + (Get-Date -Format "yyyyMMdd") + ".log")

"[$(Get-Date -Format 'HH:mm:ss')] fleet tick" | Out-File $log -Append -Encoding utf8
git pull -q --rebase --autostash origin main 2>&1 | Out-File $log -Append -Encoding utf8

python -m src.fleet_run 2>&1 | Out-File $log -Append -Encoding utf8
$code = $LASTEXITCODE
"[$(Get-Date -Format 'HH:mm:ss')] exit=$code" | Out-File $log -Append -Encoding utf8

git add data/ 2>&1 | Out-Null
git diff --staged --quiet
if ($LASTEXITCODE -ne 0) {
    git commit -q -m "chore: 함대 이력 갱신 (PC $(Get-Date -Format 'yyyy-MM-dd HH:mm'))" 2>&1 | Out-File $log -Append -Encoding utf8
    git push -q origin main 2>&1 | Out-File $log -Append -Encoding utf8
}
exit $code
