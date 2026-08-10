<#
.USAGE
    1. Open PowerShell as Administrator (some info, like driver details
       and event logs, needs elevation to read fully).
    2. You may need to allow the script to run once:
           Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
    3. Run it:
           .\Get-ModbusDiagnostics.ps1
    4. It creates ModbusDiagnostics_<timestamp>.txt in the same folder.
       Send that file back.
#>

$stamp      = Get-Date -Format "yyyyMMdd_HHmmss"
$outFile    = Join-Path $PSScriptRoot "ModbusDiagnostics_$stamp.txt"

function Write-Section {
    param([string]$Title)
    "`r`n" + ("=" * 70) + "`r`n$Title`r`n" + ("=" * 70) | Out-File -FilePath $outFile -Append -Encoding UTF8
}

function Write-Safe {
    param([scriptblock]$Block)
    try {
        & $Block | Out-File -FilePath $outFile -Append -Encoding UTF8
    } catch {
        "  [Could not collect this section: $($_.Exception.Message)]" | Out-File -FilePath $outFile -Append -Encoding UTF8
    }
}

# Start fresh
"Modbus / RS-485 Diagnostics" | Out-File -FilePath $outFile -Encoding UTF8
"Generated: $(Get-Date)" | Out-File -FilePath $outFile -Append -Encoding UTF8
"Computer: $env:COMPUTERNAME" | Out-File -FilePath $outFile -Append -Encoding UTF8

# --- OS info ---------------------------------------------------------
Write-Section "OS / System Info"
Write-Safe { Get-CimInstance Win32_OperatingSystem |
    Select-Object Caption, Version, OSArchitecture, LastBootUpTime | Format-List }

# --- COM ports --------------------------------------------------------
Write-Section "COM Ports (Win32_SerialPort)"
Write-Safe { Get-CimInstance Win32_SerialPort |
    Select-Object DeviceID, Name, Description, MaxBaudRate | Format-Table -AutoSize }

Write-Section "COM Ports (PnP Devices, Ports category)"
Write-Safe { Get-PnpDevice -Class Ports -PresentOnly |
    Select-Object Status, Class, FriendlyName, InstanceId | Format-Table -AutoSize -Wrap }

# --- USB-to-serial adapter driver details ------------------------------
Write-Section "USB Serial Adapter Driver Details (FTDI / Prolific / CH340 / CP210x etc.)"
Write-Safe {
    $ports = Get-PnpDevice -Class Ports -PresentOnly
    foreach ($p in $ports) {
        "`nDevice: $($p.FriendlyName)"
        Get-PnpDeviceProperty -InstanceId $p.InstanceId -KeyName 'DEVPKEY_Device_DriverVersion' -ErrorAction SilentlyContinue |
            Select-Object @{n='DriverVersion';e={$_.Data}}
        Get-PnpDeviceProperty -InstanceId $p.InstanceId -KeyName 'DEVPKEY_Device_DriverDate' -ErrorAction SilentlyContinue |
            Select-Object @{n='DriverDate';e={$_.Data}}
        Get-PnpDeviceProperty -InstanceId $p.InstanceId -KeyName 'DEVPKEY_Device_Manufacturer' -ErrorAction SilentlyContinue |
            Select-Object @{n='Manufacturer';e={$_.Data}}
    }
}

# --- Which process might be holding a COM port -------------------------
Write-Section "Processes That May Be Holding Serial Ports Open"
Write-Safe {
    "Note: Windows doesn't expose a simple built-in COM port -> PID map."
    "Common culprits to manually check: Modbus Poll, PuTTY, Arduino IDE, other terminal/monitoring tools."
    Get-Process | Where-Object {
        $_.ProcessName -match 'mbpoll|putty|termite|realterm|arduino|python|pythonw'
    } | Select-Object Id, ProcessName, StartTime | Format-Table -AutoSize
}

# --- Recent relevant Event Log entries ---------------------------------
Write-Section "Device/USB/Serial Related Event Log Entries"
Write-Safe {
    Get-WinEvent -FilterHashtable @{ LogName = 'System' } -MaxEvents 2000 -ErrorAction SilentlyContinue |
        Where-Object { $_.ProviderName -match 'usbhub|usbccgp|kernel-pnp|serial|ftdibus|serenum' } |
        Select-Object TimeCreated, ProviderName, Id, LevelDisplayName, Message |
        Format-List
}

# --- Python / pymodbus environment --------------------------------------
Write-Section "Python Environment"
Write-Safe { "python --version:"; python --version 2>&1 }
Write-Safe { "python location:"; (Get-Command python -ErrorAction SilentlyContinue).Source }
Write-Safe { "pip show pymodbus:"; python -m pip show pymodbus 2>&1 }
Write-Safe { "Installed serial-related pip packages:"; python -m pip list 2>&1 | Select-String -Pattern 'pymodbus|pyserial' }

# --- Network/loopback info (only relevant if using Modbus TCP gateway) ---
Write-Section "Network Adapters (only relevant if using a Modbus TCP/RS485 gateway)"
Write-Safe { Get-NetIPConfiguration | Select-Object InterfaceAlias, IPv4Address, IPv4DefaultGateway | Format-Table -AutoSize }

# --- Wrap up -------------------------------------------------------------
Write-Section "Done"
"Diagnostics written to: $outFile" | Out-File -FilePath $outFile -Append -Encoding UTF8

Write-Host "`nDiagnostics complete." -ForegroundColor Green
Write-Host "Output file: $outFile" -ForegroundColor Green
