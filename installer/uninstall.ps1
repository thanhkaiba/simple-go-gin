# Camera attendance: stop it and remove it from this Windows computer. Started by Uninstall.bat.
$AppHome = if ($env:FACE_ATTENDANCE_HOME) { $env:FACE_ATTENDANCE_HOME } else { Join-Path $env:LOCALAPPDATA "FaceAttendance" }

Get-CimInstance Win32_Process |
    Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine.Contains($AppHome) } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path ([Environment]::GetFolderPath("Startup")) "Camera attendance.lnk")
Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path ([Environment]::GetFolderPath("Desktop")) "Camera attendance.url")
Write-Host "Stopped; it no longer starts at login."

if (Test-Path $AppHome) {
    $answer = Read-Host "Also delete all data (attendance records, photos, settings) in $AppHome? [y/N]"
    if ($answer -eq "y" -or $answer -eq "Y") {
        Start-Sleep -Seconds 2  # let the stopped processes release their files
        Remove-Item -Recurse -Force $AppHome
        Write-Host "Everything was deleted."
    } else {
        foreach ($d in "venv", "tools", "models", "run.ps1") {
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $AppHome $d)
        }
        Write-Host "The program was removed. Records, photos and settings were kept in $AppHome;"
        Write-Host "installing again picks them up."
    }
}
