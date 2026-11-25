# Download/update the public Pylon daemon source. Does not start services.
param([string]$Destination = (Join-Path $HOME 'pylon-server\daemon'))
$ErrorActionPreference = 'Stop'
$Url = 'https://github.com/frontjang-gif/pylon-server-daemon/archive/refs/heads/main.zip'
$Destination = [IO.Path]::GetFullPath($Destination)
$Temp = Join-Path ([IO.Path]::GetTempPath()) ("pylon-daemon-" + [guid]::NewGuid())

function Assert-NoReparsePoint([string]$Path) {
    $Current = $Path
    while ($Current) {
        if (Test-Path -LiteralPath $Current) {
            $Item = Get-Item -LiteralPath $Current -Force
            if ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Refusing reparse point: $Current"
            }
        }
        $Parent = [IO.Path]::GetDirectoryName($Current)
        if ($Parent -eq $Current) { break }
        $Current = $Parent
    }
}

try {
    Assert-NoReparsePoint $Destination
    New-Item -ItemType Directory -Path $Temp | Out-Null
    $Zip = Join-Path $Temp 'daemon.zip'
    Invoke-WebRequest -Uri $Url -OutFile $Zip -UseBasicParsing -TimeoutSec 180
    $Extracted = Join-Path $Temp 'extracted'
    # Validate entries before extracting.
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $Archive = [IO.Compression.ZipFile]::OpenRead($Zip)
    try {
        foreach ($Entry in $Archive.Entries) {
            $Name = $Entry.FullName
            if (!$Name.StartsWith('pylon-server-daemon-main/') -or
                $Name.Contains('\') -or $Name.Contains(':') -or
                ($Name.Split('/') -contains '..') -or
                (($Entry.ExternalAttributes -shr 16 -band 0xF000) -eq 0xA000)) {
                throw "Unexpected archive entry"
            }
        }
    }
    finally { $Archive.Dispose() }
    Expand-Archive -LiteralPath $Zip -DestinationPath $Extracted
    $Source = Join-Path $Extracted 'pylon-server-daemon-main'
    foreach ($Required in @('daemons/windows/agent.py','daemons/linux/agent.py','daemons/serve_mode.py')) {
        if (!(Test-Path -LiteralPath (Join-Path $Source $Required) -PathType Leaf)) {
            throw "Missing daemon source: $Required"
        }
    }
    $Files = @(Get-ChildItem -LiteralPath $Source -Recurse -File -Force | Where-Object {
        $Relative = $_.FullName.Substring($Source.Length + 1)
        $_.Name -notin @('config.json','agent-token','.env') -and
        !$_.Name.StartsWith('.env.') -and
        !(($Relative -split '[\\/]') | Where-Object { $_ -in @('data','.venv','.venvs','__pycache__') })
    })
    foreach ($File in $Files) {
        $Target = Join-Path $Destination ($File.FullName.Substring($Source.Length + 1))
        Assert-NoReparsePoint $Target
        if (Test-Path -LiteralPath $Target -PathType Container) {
            throw "File conflict: $Target"
        }
        $Parent = Split-Path -Parent $Target
        while ($Parent) {
            if (Test-Path -LiteralPath $Parent -PathType Leaf) { throw "Directory conflict: $Parent" }
            $Parent = [IO.Path]::GetDirectoryName($Parent)
        }
    }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    foreach ($File in $Files) {
        $Target = Join-Path $Destination ($File.FullName.Substring($Source.Length + 1))
        New-Item -ItemType Directory -Path (Split-Path -Parent $Target) -Force | Out-Null
        Copy-Item -LiteralPath $File.FullName -Destination $Target -Force
    }
    Write-Host "Daemon source installed at $Destination"
    Write-Host 'Local configuration preserved. No daemon was started.'
}
finally {
    if (Test-Path -LiteralPath $Temp) {
        Remove-Item -LiteralPath $Temp -Recurse -Force
    }
}
