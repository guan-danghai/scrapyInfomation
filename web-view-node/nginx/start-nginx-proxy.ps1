# Nginx reverse proxy to Node on 127.0.0.1:5000
# Usage:
#   .\start-nginx-proxy.ps1              -> HTTP on port 8335
#   .\start-nginx-proxy.ps1 -Https       -> HTTPS 443 + HTTP 80 redirect (needs certs + usually Admin)
# Stop: .\start-nginx-proxy.ps1 -Stop

param(
    [switch]$Stop,
    [switch]$Https
)

$ErrorActionPreference = 'Stop'

function Find-NginxPrefix {
    $roots = @(
        $env:NGINX_HOME,
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages')
    ) | Where-Object { $_ -and (Test-Path $_) }

    foreach ($base in $roots) {
        if ($base -match 'WinGet\\Packages') {
            $dir = Get-ChildItem -Path $base -Directory -Filter 'nginxinc.nginx*' -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending | Select-Object -First 1
            if ($dir) {
                $ver = Get-ChildItem -Path $dir.FullName -Directory -Filter 'nginx-*' -ErrorAction SilentlyContinue |
                    Sort-Object { [version]($_.Name -replace '^nginx-','') } -Descending | Select-Object -First 1
                if ($ver) { return $ver.FullName }
            }
        }
        elseif (Test-Path (Join-Path $base 'conf\mime.types')) {
            return $base
        }
    }
    return $null
}

$prefix = Find-NginxPrefix
if (-not $prefix) {
    Write-Error 'Nginx not found. Set NGINX_HOME to nginx root (folder containing conf/mime.types).'
}

$confName = if ($Https) { 'nginx.caizhaowang-https.conf' } else { 'nginx.caizhaowang.conf' }
$confSrc = Join-Path $PSScriptRoot $confName
$confDest = Join-Path $prefix "conf\$confName"
if (-not (Test-Path $confSrc)) {
    Write-Error "Missing config: $confSrc"
}

if ($Https) {
    $sslChain = Join-Path $prefix 'conf\ssl\fullchain.pem'
    $sslKey = Join-Path $prefix 'conf\ssl\privkey.pem'
    if (-not (Test-Path $sslChain) -or -not (Test-Path $sslKey)) {
        Write-Error "HTTPS needs PEM files. Run gen-dev-ssl.cmd (needs openssl) or copy fullchain.pem + privkey.pem to:`n  $(Join-Path $prefix 'conf\ssl')"
    }
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Warning 'Ports 80/443 usually require Administrator. If nginx fails to start, right-click CMD/PowerShell -> Run as administrator.'
    }
}

Copy-Item -Path $confSrc -Destination $confDest -Force
Write-Host "Synced config to: $confDest"

Push-Location $prefix
try {
    if ($Stop) {
        $oldEap = $ErrorActionPreference
        $ErrorActionPreference = 'SilentlyContinue'
        try {
            & .\nginx.exe -s stop 2>&1 | Out-Null
            if ($LASTEXITCODE -ne 0) { & .\nginx.exe -s quit 2>&1 | Out-Null }
        }
        finally {
            $ErrorActionPreference = $oldEap
        }
        Write-Host 'Stop: if nginx was running it should exit; no pid file means it was already stopped.'
        return
    }

    $test = Start-Process -FilePath .\nginx.exe -ArgumentList @('-t', '-p', '.', '-c', "conf\$confName") -WorkingDirectory $prefix -Wait -PassThru -NoNewWindow
    if ($test.ExitCode -ne 0) { exit $test.ExitCode }

    Start-Process -FilePath .\nginx.exe -ArgumentList @('-p', '.', '-c', "conf\$confName") -WorkingDirectory $prefix -WindowStyle Hidden
    Write-Host ''
    Write-Host ('Nginx started. prefix: ' + $prefix)
    if ($Https) {
        Write-Host 'Open: https://127.0.0.1/  (Node must listen on 5000; browser may warn on self-signed cert)'
        Write-Host 'Stop: start-nginx-proxy.cmd -Stop'
    }
    else {
        Write-Host 'Open: http://127.0.0.1:8335/ (Node must listen on 5000)'
        Write-Host 'Stop: start-nginx-proxy.cmd -Stop'
    }
}
finally {
    Pop-Location
}
