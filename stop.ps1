# Stop this package only. Runtime files and accounts remain on disk.
[CmdletBinding()]
param()

. (Join-Path $PSScriptRoot 'start.ps1')

function Invoke-BundleStop {
    param([string]$ProjectRoot)
    $paths = Get-BundlePaths $ProjectRoot
    Assert-BundleRuntime $paths -RequirePrepared
    $docker = Get-BundleDockerPath
    $context = Get-BundleLocalContext $docker
    if ((Get-BundleEngineState $docker $context) -ne 'linux') {
        throw 'The local Linux Docker engine is not running. Nothing was stopped.'
    }
    $compose = Get-BundleComposeArguments $paths $context
    $null = Invoke-BundleChecked $docker ($compose + @('config', '--quiet')) 'Validating bundle configuration'
    $null = Invoke-BundleChecked $docker ($compose + @('stop')) 'Stopping this bundle' 120
    Write-Host 'This bundle is stopped. Runtime data, credentials and login state were retained.'
}

if ($MyInvocation.InvocationName -ne '.') {
    $ErrorActionPreference = 'Stop'
    try { Invoke-BundleStop -ProjectRoot $PSScriptRoot; exit 0 }
    catch { Write-Host ('STOP: ' + $_.Exception.Message) -ForegroundColor Red; exit 1 }
}
