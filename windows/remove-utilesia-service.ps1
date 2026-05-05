#Requires -RunAsAdministrator
param(
    [string]$ServiceName = "UtilesIA",
    [string]$NssmExe = "nssm"
)

$ErrorActionPreference = "Stop"

function Resolve-Nssm {
    param(
        [string]$Candidate,
        [string]$SearchRoot
    )
    if ($Candidate.EndsWith(".exe", [System.StringComparison]::OrdinalIgnoreCase)) {
        if (-not (Test-Path -LiteralPath $Candidate)) {
            throw "No se encuentra NSSM en: $Candidate"
        }
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
    throw @"
NSSM no encontrado.
  Copie nssm.exe en $SearchRoot o use -NssmExe 'C:\ruta\nssm.exe'
  Descarga: https://nssm.cc
"@
}

$nssm = Resolve-Nssm -Candidate $NssmExe -SearchRoot $PSScriptRoot
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (-not $svc) {
    Write-Host "No existe el servicio '$ServiceName'."
    exit 0
}
if ($svc.Status -eq "Running") {
    Stop-Service -Name $ServiceName -Force
}
& $nssm remove $ServiceName confirm
Write-Host "Servicio '$ServiceName' eliminado."
