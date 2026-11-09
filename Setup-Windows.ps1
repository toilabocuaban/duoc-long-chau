$ErrorActionPreference='Stop'
$distro='Gazebo-Ubuntu'
try {
    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
        throw 'WSL is missing. Install WSL 2 with Ubuntu 24.04 using the instructions in README-VI.txt, restart Windows, then rerun setup.'
    }
    $installed=@(& wsl.exe --list --quiet 2>$null) | ForEach-Object { ($_ -replace '\x00','').Trim() }
    if ($installed -notcontains $distro) {
        $installDir=Join-Path $env:LOCALAPPDATA 'Warehouse4Way\Ubuntu'
        New-Item -ItemType Directory -Force -Path (Split-Path $installDir) | Out-Null
        & wsl.exe --install Ubuntu-24.04 --name $distro --location $installDir --no-launch --web-download
        if ($LASTEXITCODE -ne 0) { throw 'Ubuntu installation failed. Update/install WSL, restart Windows if requested, then rerun this setup.' }
    }
    $linuxRoot=(& wsl.exe -d $distro -u root -- wslpath -u $PSScriptRoot | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Cannot start Ubuntu. Verify WSL 2 is enabled.' }
    & wsl.exe -d $distro -u root -- bash "$linuxRoot/setup-ubuntu.sh"
    if ($LASTEXITCODE -ne 0) { throw 'Gazebo setup failed. Check Internet access and the error above.' }
    Write-Host 'Ready. Open 02-Start-Warehouse.cmd.' -ForegroundColor Green
    Read-Host 'Press Enter to close'
} catch { Write-Host $_ -ForegroundColor Red; Read-Host 'Press Enter to close'; exit 1 }
