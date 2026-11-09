$ErrorActionPreference = 'SilentlyContinue'
$repo = 'D:\gazebo\_inspect\Warehouse4Way'
$last = ''
while ($true) {
    $state = (& git -C $repo status --porcelain 2>$null) -join "`n"
    if ($state -and $state -ne $last) {
        & git -C $repo add .
        & git -C $repo commit -m 'Auto-sync project changes' 2>$null | Out-Null
        & git -C $repo push origin main 2>$null | Out-Null
        $last = (& git -C $repo status --porcelain 2>$null) -join "`n"
    } elseif (-not $state) {
        $last = ''
    }
    Start-Sleep -Seconds 15
}
