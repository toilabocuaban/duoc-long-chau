param(
    [ValidateSet('Run','Inspect','View','Test','FourFloor')][string]$Mode='Run',
    [string]$View='rack',
    [switch]$SoftwareRendering
)
$ErrorActionPreference='Stop'
$distro='Gazebo-Ubuntu'
try {
    $drive=$PSScriptRoot.Substring(0,1).ToLower()
    $linuxRoot="/mnt/$drive" + (($PSScriptRoot.Substring(2)) -replace '\\','/')
    & wsl.exe -d $distro -u gazebo -- test -d $linuxRoot
    if ($LASTEXITCODE -ne 0) { throw 'Run 01-Setup-Windows.cmd first.' }
    if ($Mode -eq 'View') {
        $partition=if ($View -in @('shuttle_detail','lift_detail','equipment','inbound_detail','outbound_detail')) {'warehouse_equipment'} else {'warehouse_v1'}
        & wsl.exe -d $distro -u gazebo -- env "GZ_PARTITION=$partition" python3 "$linuxRoot/view.py" $View
    } elseif ($Mode -eq 'Test') {
        & wsl.exe -d $distro -u gazebo -- bash "$linuxRoot/test.sh"
    } else {
        $script=if ($Mode -eq 'Inspect') {'inspect-equipment.sh'} elseif ($Mode -eq 'FourFloor') {'four-floor.sh'} else {'run.sh'}
        $software=if ($SoftwareRendering) {'1'} else {'0'}
        & wsl.exe -d $distro -u gazebo -- env "WAREHOUSE_SOFTWARE_RENDERING=$software" bash "$linuxRoot/$script"
    }
    if ($LASTEXITCODE -ne 0) { throw 'Gazebo command failed. Check the project log or setup instructions.' }
} catch { Write-Host $_ -ForegroundColor Red; Read-Host 'Press Enter to close'; exit 1 }
