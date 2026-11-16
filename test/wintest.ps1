# Waystone Windows test on a clean runner: download, sha256, unzip to a path with a space and an accent,
# start it like a double-click, wait for the AI, ask questions, single-instance check, screenshots, VC runtime check.
$ErrorActionPreference = "Stop"
$ws = $env:GITHUB_WORKSPACE
$out = Join-Path $ws "out"
New-Item -ItemType Directory -Force $out | Out-Null
$R = [ordered]@{}
function Save { $R | ConvertTo-Json -Depth 8 | Out-File -Encoding utf8 (Join-Path $out "result.json") }
function Log($m) { $l = "$(Get-Date -Format HH:mm:ss) $m"; Write-Host $l; Add-Content -Encoding utf8 (Join-Path $out "log.txt") $l }

$R.machine = [ordered]@{ cores = (Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfCores -Sum).Sum; logical = (Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfLogicalProcessors -Sum).Sum; os = (Get-CimInstance Win32_OperatingSystem).Caption; ram_gb = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1); cpus = $env:NUMBER_OF_PROCESSORS }

# 1. Download and sha256
$zip = Join-Path $env:RUNNER_TEMP "waystone.zip"
$t = Get-Date
if ($env:ZIP_FILE) { Move-Item -LiteralPath $env:ZIP_FILE -Destination $zip -Force } else { curl.exe -sSL --retry 3 -o $zip $env:ZIP_URL }
$R.download_s = [math]::Round(((Get-Date) - $t).TotalSeconds)
$R.zip_bytes = (Get-Item $zip).Length
$sha = (Get-FileHash -Algorithm SHA256 $zip).Hash.ToLower()
$R.sha256 = $sha
$R.sha_ok = ($env:ZIP_SHA -eq "" -or $sha -eq $env:ZIP_SHA.ToLower())
Log "download $($R.zip_bytes) bytes in $($R.download_s) s, sha ok: $($R.sha_ok)"
Save
if (-not $R.sha_ok) { throw "sha256 mismatch: $sha" }

# 2. Unzip to a folder with a space and an accent
$e = [char]0x00E9
$dest = "C:\Users\runneradmin\T${e}l${e}chargements\Waystone test"
New-Item -ItemType Directory -Force $dest | Out-Null
# First run as a real buyer: internet mark on the zip, Explorer's own extraction engine, Defender and Smart App Control state
Set-Content -Path $zip -Stream Zone.Identifier -Value "[ZoneTransfer]`r`nZoneId=3"
Log "zone identifier set: $((Get-Content -Path $zip -Stream Zone.Identifier) -join ' ')"
try { $mp = Get-MpComputerStatus | Select-Object RealTimeProtectionEnabled, AntivirusEnabled, AMServiceEnabled, BehaviorMonitorEnabled, IoavProtectionEnabled; $R.defender = $mp; Log "defender: $($mp | ConvertTo-Json -Compress)" } catch { Log "defender status not readable: $_" }
try { $sac = (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy" -ErrorAction Stop).VerifiedAndReputablePolicyState; $R.smart_app_control = $sac; Log "smart app control state (0 off, 1 on, 2 evaluation): $sac" } catch { Log "smart app control state not readable: $_" }
$t = Get-Date
Expand-Archive -LiteralPath $zip -DestinationPath $dest -Force
$R.unzip_s = [math]::Round(((Get-Date) - $t).TotalSeconds)
Remove-Item $zip
$app = Get-ChildItem -LiteralPath $dest -Recurse -Filter "Start Waystone.exe" | Select-Object -First 1
$R.exe = $app.FullName
Log "unzipped in $($R.unzip_s) s: $($app.FullName)"

# 3. Start like a double-click (ShellExecute, working folder = the exe's folder)
$data = Join-Path $env:APPDATA "Waystone"
$run = Join-Path $data "en-cours.json"
if (Test-Path $run) { Remove-Item $run }
$t0 = Get-Date
$p1 = Start-Process -FilePath $app.FullName -WorkingDirectory $app.DirectoryName -PassThru
$url = $null
for ($i = 0; $i -lt 300 -and -not $url; $i++) {
  Start-Sleep 1
  if (Test-Path $run) { try { $url = (Get-Content -Raw $run | ConvertFrom-Json).url } catch {} }
  if ($p1.HasExited) { break }
}
if (-not $url) { $R.start_error = "no interface address (exited: $($p1.HasExited), code $($p1.ExitCode))"; Save; Copy-Item -ErrorAction SilentlyContinue (Join-Path $data "journal.txt") $out; throw $R.start_error }
$R.interface_s = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
$R.url = $url
Log "interface $url after $($R.interface_s) s"
$st = $null
for ($i = 0; $i -lt 900; $i++) {
  try { $st = Invoke-RestMethod "${url}api/status" -TimeoutSec 10 } catch {}
  if ($st -and ($st.chat -eq "ready" -or $st.error)) { break }
  if ($st -and $st.embed -eq "ready" -and -not $R.embed_ready_s) { $R.embed_ready_s = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1) }
  Start-Sleep 1
}
$R.status = $st
$R.chat_ready_s = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
$R.models_ready = ($st.chat -eq "ready" -and $st.embed -eq "ready")
$pinger = Start-Job -ArgumentList $url -ScriptBlock { param($u) while ($true) { try { Invoke-RestMethod "${u}api/ping" -Method Post -Body "{}" -ContentType "application/json" -TimeoutSec 5 | Out-Null } catch {}; Start-Sleep 15 } }
Log "status after $($R.chat_ready_s) s: $($st | ConvertTo-Json -Compress -Depth 4)"
Save

# 4. Questions through the app's own interface
$qs = Get-Content -Raw -Encoding utf8 (Join-Path $ws "test\questions.json") | ConvertFrom-Json
$R.answers = @()
foreach ($q in $qs) {
  $tq = Get-Date
  $quick = $null
  try { $quick = Invoke-RestMethod "${url}api/quick" -Method Post -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes((@{ q = $q.q } | ConvertTo-Json))) -TimeoutSec 120 } catch {}
  $quick_s = [math]::Round(((Get-Date) - $tq).TotalSeconds, 1)
  $t = Get-Date
  try {
    $a = Invoke-RestMethod "${url}api/ask" -Method Post -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes((@{ q = $q.q } | ConvertTo-Json))) -TimeoutSec 900
    $R.answers += [ordered]@{ kind = $q.kind; q = $q.q; s = [math]::Round(((Get-Date) - $t).TotalSeconds, 1); quick_s = $quick_s; quick_status = $quick.status; quick_urgent = $quick.urgent; quick_pages = @($quick.blocks | ForEach-Object { $_.card_id }); llm_timings = $a.debug.llm_timings; embed_ms = $a.debug.embed_ms; llm_ms = $a.debug.llm_ms; ai_ms = $a.debug.ai_check_ms; status = $a.status; urgent = $a.urgent; triage = $a.debug.triage; ai_check = $a.debug.ai_check; reason = $a.debug.reason; warning = "$($a.warning)".Substring(0, [math]::Min(400, "$($a.warning)".Length)); message = $a.message; pages = @($a.blocks | ForEach-Object { $_.card_id }); choices = @($a.choices | ForEach-Object { $_.title }) }
  } catch { $R.answers += [ordered]@{ kind = $q.kind; q = $q.q; quick_s = $quick_s; quick_status = $quick.status; error = "$_" } }
  Log "asked [$($q.kind)] in $([math]::Round(((Get-Date) - $t).TotalSeconds, 1)) s"
}
Save

# 5. Single instance: a second double-click must not start a second server
$servers1 = @(Get-CimInstance Win32_Process -Filter "Name like 'llama-server%'").Count
Copy-Item (Join-Path $data "journal.txt") (Join-Path $out "journal-after-questions.txt") -ErrorAction SilentlyContinue
if (-not (Test-Path $run)) { $R.app_gone_after_questions = $true; Save; throw "the guide stopped during the questions" }
$pid1 = (Get-Content -Raw $run | ConvertFrom-Json).pid
$t = Get-Date
$p2 = Start-Process -FilePath $app.FullName -WorkingDirectory $app.DirectoryName -PassThru
$p2.WaitForExit(60000) | Out-Null
Start-Sleep 5
$R.single_instance = [ordered]@{
  second_exited = $p2.HasExited; second_exit_s = [math]::Round(((Get-Date) - $t).TotalSeconds, 1); second_exit_code = $(if ($p2.HasExited) { $p2.ExitCode } else { $null })
  guide_processes = @(Get-Process "Start Waystone" -ErrorAction SilentlyContinue).Count
  model_servers_before = $servers1; model_servers_after = @(Get-CimInstance Win32_Process -Filter "Name like 'llama-server%'").Count
  same_pid_in_run_file = ((Get-Content -Raw $run | ConvertFrom-Json).pid -eq $pid1)
}
$R.single_instance.ok = ($R.single_instance.second_exited -and $R.single_instance.guide_processes -eq 1 -and $R.single_instance.model_servers_after -eq $servers1 -and $R.single_instance.same_pid_in_run_file)
if (-not $p2.HasExited) { Stop-Process -Id $p2.Id -Force }
Log "single instance: $($R.single_instance | ConvertTo-Json -Compress)"
Save

# 6. Screenshots with Chrome headless against the local page
$gen = ($R.answers | Where-Object { $_.ai_check -eq "other_emergency" } | Select-Object -First 1)
if (-not $gen) { $gen = ($R.answers | Where-Object { $_.kind -eq "generic_candidate" } | Select-Object -First 1) }
$chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
if (-not (Test-Path $chrome)) { $chrome = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" }
try { node (Join-Path $ws "test\shots.js") $url $chrome $out $qs[1].q $gen.q 2>&1 | ForEach-Object { Log "$_" } } catch { Log "screenshots failed: $_" }

Stop-Job $pinger -ErrorAction SilentlyContinue
# 7. Close the guide, keep its log
try { Invoke-RestMethod "${url}api/quit" -Method Post -TimeoutSec 10 | Out-Null } catch {}
$p1.WaitForExit(60000) | Out-Null
$R.quit_ok = $p1.HasExited
$R.model_servers_left = @(Get-CimInstance Win32_Process -Filter "Name like 'llama-server%'").Count
Copy-Item (Join-Path $data "journal.txt") (Join-Path $out "journal-main.txt") -ErrorAction SilentlyContinue
Log "quit ok: $($R.quit_ok), model servers left: $($R.model_servers_left)"
Save

# 8. VC runtime hidden from System32 on this throwaway machine: the app must still reach ready with its bundled DLLs
$R.vc_check = [ordered]@{}
$dlls = "msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"
$sys = Join-Path $env:SystemRoot "System32"
$hidden = @()
try {
  foreach ($d in $dlls) {
    $f = Join-Path $sys $d
    if (Test-Path $f) { takeown /f $f | Out-Null; icacls $f /grant "Administrators:F" | Out-Null; Rename-Item $f "$d.hidden"; $hidden += $d }
  }
  $R.vc_check.hidden = $hidden
  if (Test-Path $run) { Remove-Item $run }
  $p3 = Start-Process -FilePath $app.FullName -WorkingDirectory $app.DirectoryName -PassThru
  $u3 = $null
  for ($i = 0; $i -lt 120 -and -not $u3; $i++) { Start-Sleep 1; if (Test-Path $run) { try { $u3 = (Get-Content -Raw $run | ConvertFrom-Json).url } catch {} } }
  $s3 = $null
  for ($i = 0; $i -lt 600 -and $u3; $i++) { try { $s3 = Invoke-RestMethod "${u3}api/status" -TimeoutSec 10 } catch {}; if ($s3 -and ($s3.error -or $s3.chat -eq "ready")) { break }; Start-Sleep 1 }
  $R.vc_check.status = $s3
  $R.vc_check.bundled_dlls = @(Get-ChildItem -LiteralPath $app.DirectoryName -Recurse -Include $dlls -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName.Substring($app.DirectoryName.Length) })
  $R.vc_check.ready = ($s3.chat -eq "ready" -and $s3.embed -eq "ready" -and -not $s3.error)
  if ($u3) {
    Set-Location (Join-Path $ws "test")
    try { node (Join-Path $ws "test\vcshot.js") $chrome $u3 (Join-Path $out "6-vc-missing.png") 2>&1 | ForEach-Object { Log "$_" } } catch {}
    try { Invoke-RestMethod "${u3}api/quit" -Method Post -TimeoutSec 10 | Out-Null } catch {}
  }
  Start-Sleep 5
  if ($p3 -and -not $p3.HasExited) { Stop-Process -Id $p3.Id -Force }
  Copy-Item (Join-Path $data "journal.txt") (Join-Path $out "journal-vc.txt") -ErrorAction SilentlyContinue
} catch { $R.vc_check.error = "$_" }
finally { foreach ($d in $hidden) { Rename-Item (Join-Path $sys "$d.hidden") $d -ErrorAction SilentlyContinue } }
Log "vc check: $($R.vc_check | ConvertTo-Json -Compress -Depth 5)"
Save

$R.pass = ($R.sha_ok -and $R.models_ready -and $R.single_instance.ok -and $R.quit_ok -and $R.vc_check.ready -and -not ($R.answers | Where-Object { $_.error }))
Save
Log "PASS: $($R.pass)"
if ($R.pass) { exit 0 } else { exit 1 }
