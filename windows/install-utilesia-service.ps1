#Requires -RunAsAdministrator
<#
  Instala UtilesIA como servicio de Windows con NSSM (Non-Sucking Service Manager).

  Requisitos:
  - Python venv en la carpeta del proyecto (.venv)
  - NSSM: si no esta en PATH ni en windows\nssm.exe, por defecto se descarga nssm-2.24 desde nssm.cc
    (-NoAutoDownload para solo modo manual / sin red)

  Uso (PowerShell como administrador):
    cd ...\UtilesIA\windows
    .\install-utilesia-service.ps1

  Opcional host/puerto:
    .\install-utilesia-service.ps1 -HostBinding "192.168.10.238" -Port 8787

  Opciones (log LLM, OCR): utilesia.env en la raiz del proyecto o nssm edit UtilesIA.
#>

param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$ServiceName = "UtilesIA",
    [string]$HostBinding = "0.0.0.0",
    [int]$Port = 8787,
    [string]$NssmExe = "nssm",
    [switch]$NoAutoDownload
)

$ErrorActionPreference = "Stop"

# NSSM: win64\nssm.exe dentro del zip. Varias URLs por si una falla (proxy / sitio).
$script:NssmZipUrls = @(
    "https://nssm.cc/release/nssm-2.24.zip",
    "https://nssm.cc/ci/nssm-2.24-101-g897c7ad.zip"
)

function Find-NssmPath {
    param(
        [string]$Candidate,
        [string]$SearchRoot
    )
    if ($Candidate.EndsWith(".exe", [System.StringComparison]::OrdinalIgnoreCase)) {
        if (-not (Test-Path -LiteralPath $Candidate)) { return $null }
        return (Resolve-Path -LiteralPath $Candidate).Path
    }
    $cmd = Get-Command $Candidate -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $extra = @(
        (Join-Path $SearchRoot "nssm.exe"),
        (Join-Path $SearchRoot "nssm\nssm.exe"),
        (Join-Path $SearchRoot "nssm\win64\nssm.exe"),
        (Join-Path ${env:ProgramFiles} "nssm\win64\nssm.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "nssm\win64\nssm.exe")
    )
    $choco = $env:ChocolateyInstall
    if ($choco) {
        foreach ($pkg in @("nssm", "NSSM")) {
            $tools = Join-Path $choco "lib\$pkg\tools"
            if (-not (Test-Path -LiteralPath $tools)) { continue }
            $hit = Get-ChildItem -LiteralPath $tools -Filter nssm.exe -Recurse -ErrorAction SilentlyContinue |
                Select-Object -First 1
            if ($hit) { return $hit.FullName }
        }
    }
    foreach ($p in $extra) {
        if (-not $p) { continue }
        if (Test-Path -LiteralPath $p) {
            return (Resolve-Path -LiteralPath $p).Path
        }
    }
    return $null
}

function Save-NssmReleaseToWindowsFolder {
    param([string]$DestinationDir)

    Write-Host "Descargando NSSM desde nssm.cc ..."
    $tmp = Join-Path $env:TEMP ("utilesia-nssm-" + [guid]::NewGuid().ToString())
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        $zip = Join-Path $tmp "nssm.zip"
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        } catch {}

        $downloaded = $false
        $lastErr = $null
        foreach ($zipUrl in $script:NssmZipUrls) {
            try {
                Invoke-WebRequest -Uri $zipUrl -OutFile $zip -UseBasicParsing
                $downloaded = $true
                break
            } catch {
                $lastErr = $_
                Write-Host "Aviso: no se pudo descargar $zipUrl"
            }
        }
        if (-not $downloaded) {
            throw "No se pudo descargar NSSM. Compruebe la conexion o copie nssm.exe manualmente. Ultimo error: $lastErr"
        }

        Expand-Archive -LiteralPath $zip -DestinationPath $tmp -Force

        $exe = Get-ChildItem -LiteralPath $tmp -Filter nssm.exe -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match '\\win64\\' } |
            Select-Object -First 1
        if (-not $exe) {
            $exe = Get-ChildItem -LiteralPath $tmp -Filter nssm.exe -Recurse -ErrorAction SilentlyContinue |
                Select-Object -First 1
        }
        if (-not $exe) {
            throw "nssm.exe no encontrado dentro del zip descargado."
        }

        $dest = Join-Path $DestinationDir "nssm.exe"
        Copy-Item -LiteralPath $exe.FullName -Destination $dest -Force
        Write-Host "OK: NSSM guardado en $dest"
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Get-NssmExecutable {
    param(
        [string]$Candidate,
        [string]$SearchRoot,
        [switch]$NoDownload
    )

    $p = Find-NssmPath -Candidate $Candidate -SearchRoot $SearchRoot
    if ($p) { return $p }

    if ($NoDownload) {
        throw "NSSM no encontrado. Copie win64\nssm.exe del zip de https://nssm.cc a: $(Join-Path $SearchRoot 'nssm.exe') o use -NssmExe 'C:\ruta\nssm.exe'"
    }

    Save-NssmReleaseToWindowsFolder -DestinationDir $SearchRoot
    $p = Find-NssmPath -Candidate "nssm" -SearchRoot $SearchRoot
    if (-not $p) {
        throw "NSSM se descargo pero no se localiza. Reinicie el script o copie nssm.exe manualmente a $SearchRoot"
    }
    return $p
}

$nssm = Get-NssmExecutable -Candidate $NssmExe -SearchRoot $PSScriptRoot -NoDownload:$NoAutoDownload
$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "No existe $python. Cree el venv: python -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt"
}

$logsDir = Join-Path $ProjectRoot "logs"
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null
$stdoutLog = Join-Path $logsDir "service-stdout.log"
$stderrLog = Join-Path $logsDir "service-stderr.log"

$uvicornArgs = "-m uvicorn app:app --host $HostBinding --port $Port"

Write-Host "NSSM: $nssm"
Write-Host "Proyecto: $ProjectRoot"
Write-Host "Python: $python"
Write-Host "Servicio: $ServiceName"
Write-Host "Arranque: $uvicornArgs"

$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "El servicio '$ServiceName' ya existe. Deteniendo y eliminando..."
    if ($existing.Status -eq "Running") {
        Stop-Service -Name $ServiceName -Force
    }
    & $nssm remove $ServiceName confirm
}

& $nssm install $ServiceName $python
& $nssm set $ServiceName AppParameters $uvicornArgs
& $nssm set $ServiceName AppDirectory $ProjectRoot
& $nssm set $ServiceName AppStdout $stdoutLog
& $nssm set $ServiceName AppStderr $stderrLog
& $nssm set $ServiceName Description "UtilesIA - FastAPI PDF a LLM (uvicorn)"
& $nssm set $ServiceName Start SERVICE_AUTO_START

Write-Host "Instalacion lista. Iniciando servicio..."
Start-Service -Name $ServiceName
Write-Host "OK. GET http://${HostBinding}:${Port}/settings (si HostBinding es 0.0.0.0 use 127.0.0.1 desde el mismo equipo)"
Write-Host "Logs: $stdoutLog , $stderrLog"
