# Generate self-signed PEM for local HTTPS (writes to <nginx>/conf/ssl/).
# Prefers openssl if in PATH; otherwise uses Node + selfsigned (npm install in web-view-node).

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
    Write-Error 'Nginx not found. Set NGINX_HOME.'
}

$sslDir = Join-Path $prefix 'conf\ssl'
New-Item -ItemType Directory -Force -Path $sslDir | Out-Null

$openssl = Get-Command openssl -ErrorAction SilentlyContinue
if ($openssl) {
    $key = Join-Path $sslDir 'privkey.pem'
    $cert = Join-Path $sslDir 'fullchain.pem'
    $cnf = Join-Path $env:TEMP 'nginx-dev-ssl.cnf'
    @"
[req]
distinguished_name = req_distinguished_name
x509_extensions = v3_req
prompt = no

[req_distinguished_name]
CN = ztb.resoftcss.com.cn

[v3_req]
keyUsage = keyEncipherment, dataEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names

[alt_names]
DNS.1 = ztb.resoftcss.com.cn
DNS.2 = localhost
IP.1 = 127.0.0.1
"@ | Set-Content -Path $cnf -Encoding ASCII

    & openssl req -x509 -nodes -days 825 -newkey rsa:2048 -keyout $key -out $cert -config $cnf
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Remove-Item $cnf -ErrorAction SilentlyContinue
    Write-Host "OK: $cert"
    Write-Host 'Next: start-nginx-proxy.cmd -Https (Admin for 80/443).'
    return
}

$webRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pemScript = Join-Path $PSScriptRoot 'gen-dev-pem.cjs'
if (-not (Test-Path $pemScript)) {
    Write-Error "Missing $pemScript"
}

Push-Location $webRoot
try {
    if (-not (Test-Path (Join-Path $webRoot 'node_modules\selfsigned'))) {
        Write-Host 'Installing dev dependency selfsigned...'
        & npm install --no-fund --no-audit 2>&1 | Out-Host
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    & node $pemScript $sslDir
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
finally {
    Pop-Location
}

Write-Host 'Next: start-nginx-proxy.cmd -Https (Admin for 80/443).'
