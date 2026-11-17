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

    $focusJob = if ($Mode -ne 'Test') {
        Start-Job -ScriptBlock {
            Add-Type -TypeDefinition @"
            using System;
            using System.Text;
            using System.Runtime.InteropServices;
            public class WinFocus {
                public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
                [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc enumProc, IntPtr lParam);
                [DllImport("user32.dll", CharSet = CharSet.Auto)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
                [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
                [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
                [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
                [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
                [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, IntPtr ProcessId);
                [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);
                [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);

                public static IntPtr FindWindowByKeywords(string[] keywords) {
                    IntPtr found = IntPtr.Zero;
                    EnumWindows((hWnd, lParam) => {
                        if (IsWindowVisible(hWnd)) {
                            var sb = new StringBuilder(256);
                            GetWindowText(hWnd, sb, 256);
                            string title = sb.ToString();
                            if (!string.IsNullOrEmpty(title)) {
                                if (title.IndexOf("cmd.exe", StringComparison.OrdinalIgnoreCase) >= 0 ||
                                    title.IndexOf("powershell", StringComparison.OrdinalIgnoreCase) >= 0 ||
                                    title.IndexOf("02-Start", StringComparison.OrdinalIgnoreCase) >= 0 ||
                                    title.IndexOf("WindowsTerminal", StringComparison.OrdinalIgnoreCase) >= 0) {
                                    return true;
                                }
                                foreach (var kw in keywords) {
                                    if (title.IndexOf(kw, StringComparison.OrdinalIgnoreCase) >= 0) {
                                        found = hWnd;
                                        return false;
                                    }
                                }
                            }
                        }
                        return true;
                    }, IntPtr.Zero);
                    return found;
                }

                public static bool ForceForeground(IntPtr hWnd) {
                    if (hWnd == IntPtr.Zero) return false;
                    IntPtr foreWnd = GetForegroundWindow();
                    uint foreThread = GetWindowThreadProcessId(foreWnd, IntPtr.Zero);
                    uint appThread = GetWindowThreadProcessId(hWnd, IntPtr.Zero);
                    if (foreThread != appThread) {
                        AttachThreadInput(foreThread, appThread, true);
                        BringWindowToTop(hWnd);
                        ShowWindow(hWnd, 9);
                        bool res = SetForegroundWindow(hWnd);
                        AttachThreadInput(foreThread, appThread, false);
                        return res;
                    } else {
                        BringWindowToTop(hWnd);
                        ShowWindow(hWnd, 9);
                        return SetForegroundWindow(hWnd);
                    }
                }
            }
"@
            for ($i = 0; $i -lt 150; $i++) {
                Start-Sleep -Milliseconds 500
                $h = [WinFocus]::FindWindowByKeywords(@('Gazebo Sim','Gazebo'))
                if ($h -ne [IntPtr]::Zero) {
                    Start-Sleep -Milliseconds 800
                    [WinFocus]::ForceForeground($h)
                    return
                }
            }
        }
    } else { $null }

    try {
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
    } finally {
        if ($focusJob) {
            $focusJob | Remove-Job -Force -ErrorAction SilentlyContinue
        }
    }
    if ($LASTEXITCODE -ne 0) { throw 'Gazebo command failed. Check the project log or setup instructions.' }
} catch { Write-Host $_ -ForegroundColor Red; Read-Host 'Press Enter to close'; exit 1 }
