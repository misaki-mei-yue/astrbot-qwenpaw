# Double-click start.cmd, or run .\start.ps1 from PowerShell.
[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [switch]$BuildQwenPaw,
    [ValidateRange(10, 3600)][int]$ReadyTimeoutSeconds = 600
)

function ConvertTo-BundleArgument {
    param([AllowEmptyString()][string]$Value)
    if ($Value.IndexOf([char]0) -ge 0) { throw 'Invalid process argument.' }
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Invoke-BundleCommand {
    param([string]$File, [string[]]$Arguments, [int]$TimeoutSeconds = 60, [string]$Phase = '', [AllowNull()][object]$InputText = $null)
    if ($null -ne $InputText -and $InputText -isnot [string]) { throw 'Invalid process input.' }
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $File
    $info.Arguments = (($Arguments | ForEach-Object { ConvertTo-BundleArgument $_ }) -join ' ')
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.RedirectStandardInput = $null -ne $InputText
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $info
    try {
        if (-not $process.Start()) { throw 'Unable to start the required program.' }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        $inputTask = $null
        $inputStream = $null
        $inputClosed = $false
        if ($info.RedirectStandardInput) {
            # PowerShell 5.1/.NET Framework lacks StandardInputEncoding. Write UTF-8
            # bytes directly, so system code pages cannot alter the Python source.
            $inputBytes = [System.Text.Encoding]::UTF8.GetBytes($InputText)
            $inputStream = $process.StandardInput.BaseStream
            $inputTask = $inputStream.WriteAsync($inputBytes, 0, $inputBytes.Length)
        }
        $timer = [System.Diagnostics.Stopwatch]::StartNew()
        $nextNotice = 20
        while (-not $process.WaitForExit(1000)) {
            if ($inputTask -and $inputTask.IsCompleted -and -not $inputClosed) {
                if ($inputTask.IsFaulted -or $inputTask.IsCanceled) {
                    if (-not $process.HasExited) { $process.Kill() }
                    throw 'Unable to pass the setup script to the container.'
                }
                $inputStream.Close()
                $inputClosed = $true
            }
            if ($timer.Elapsed.TotalSeconds -ge $TimeoutSeconds) {
                $process.Kill()
                $null = $process.WaitForExit(2000)
                return [pscustomobject]@{ ExitCode = 124; Output = ''; Error = '' }
            }
            if ($Phase -and $timer.Elapsed.TotalSeconds -ge $nextNotice) {
                Write-Host "Still working: $Phase"
                $nextNotice += 20
            }
        }
        return [pscustomobject]@{
            ExitCode = $process.ExitCode
            Output = $stdout.GetAwaiter().GetResult()
            Error = $stderr.GetAwaiter().GetResult()
        }
    }
    finally { $process.Dispose() }
}

function Invoke-BundleChecked {
    param([string]$File, [string[]]$Arguments, [string]$Phase, [int]$TimeoutSeconds = 60, [AllowNull()][object]$InputText = $null)
    $result = Invoke-BundleCommand -File $File -Arguments $Arguments -TimeoutSeconds $TimeoutSeconds -Phase $Phase -InputText $InputText
    if ($result.ExitCode -ne 0) {
        # Never echo native output: Compose and application errors can contain secrets.
        throw "$Phase failed (exit $($result.ExitCode)). Existing data was retained; services were not automatically stopped."
    }
    return $result
}

function Assert-BundleNoLinks {
    param([string]$Path)
    $candidate = [System.IO.Path]::GetFullPath($Path)
    while ($candidate) {
        if (Test-Path -LiteralPath $candidate) {
            $item = Get-Item -LiteralPath $candidate -Force
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Move the package and runtime data to an ordinary directory, without links or junctions.'
            }
        }
        $parent = [System.IO.Path]::GetDirectoryName($candidate)
        if ($parent -eq $candidate) { break }
        $candidate = $parent
    }
}

function Get-BundlePaths {
    param([string]$ProjectRoot)
    Assert-BundleNoLinks $ProjectRoot
    $root = Get-Item -LiteralPath $ProjectRoot -Force
    if (-not $root.PSIsContainer) { throw 'Package directory is unavailable.' }
    if ($root.FullName -eq [System.IO.Path]::GetPathRoot($root.FullName)) {
        throw 'Extract the package into its own directory, not the drive root.'
    }
    $source = $root.FullName.TrimEnd([char[]]'\/')
    $runtime = Join-Path $source 'runtime'
    $compose = Join-Path $source 'deploy\compose.bundle.yaml'
    $prepare = Join-Path $source 'deploy\prepare.py'
    foreach ($path in @($compose, $prepare)) {
        Assert-BundleNoLinks $path
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw 'This package is incomplete. Download or extract the complete release again.'
        }
    }
    Assert-BundleNoLinks $runtime
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($source.ToLowerInvariant()))
        $suffix = ([System.BitConverter]::ToString($bytes)).Replace('-', '').Substring(0, 10).ToLowerInvariant()
    }
    finally { $sha.Dispose() }
    return [pscustomobject]@{
        Root = $source; Runtime = $runtime; Compose = $compose; Prepare = $prepare
        Env = (Join-Path $runtime '.env'); Marker = (Join-Path $runtime '.combined-setup.json')
        Project = "astrbot-qwenpaw-bundle-$suffix"
    }
}

function Assert-BundleRuntime {
    param($Paths, [switch]$RequirePrepared)
    foreach ($path in @($Paths.Runtime, $Paths.Env, $Paths.Marker)) { Assert-BundleNoLinks $path }
    if (Test-Path -LiteralPath $Paths.Runtime) {
        if (-not (Test-Path -LiteralPath $Paths.Runtime -PathType Container)) {
            throw 'The runtime path is not a directory. Nothing was changed.'
        }
        $hasFiles = @(Get-ChildItem -LiteralPath $Paths.Runtime -Force).Count -gt 0
        if ($hasFiles -and -not (Test-Path -LiteralPath $Paths.Marker -PathType Leaf)) {
            throw 'The runtime directory contains unrecognized data. Use a new package directory; nothing was changed.'
        }
    }
    if (Test-Path -LiteralPath $Paths.Marker -PathType Leaf) {
        if ((Get-Item -LiteralPath $Paths.Marker).Length -gt 4096) { throw 'Invalid runtime marker.' }
        try { $marker = Get-Content -LiteralPath $Paths.Marker -Raw | ConvertFrom-Json }
        catch { throw 'Invalid runtime marker. Nothing was changed.' }
        if ($marker.mode -ne 'fresh' -or $marker.format -ne 1) {
            throw 'This runtime belongs to a different deployment. Nothing was changed.'
        }
    }
    elseif ($RequirePrepared) { throw 'This package has not been started yet. Nothing was stopped.' }
    if ($RequirePrepared -and -not (Test-Path -LiteralPath $Paths.Env -PathType Leaf)) {
        throw 'The private runtime configuration is missing. Nothing was stopped.'
    }
}

function Get-BundleDockerPath {
    $command = Get-Command docker.exe -ErrorAction SilentlyContinue
    if (-not $command) {
        throw 'Install Docker Desktop, enable Linux containers, then run this package again.'
    }
    return $command.Source
}

function Get-BundleLocalContext {
    param([string]$Docker)
    $result = Invoke-BundleChecked $Docker @('context', 'show') 'Checking Docker context'
    $context = $result.Output.Trim()
    if (-not $context -or $context -notmatch '^[A-Za-z0-9_.-]+$') { throw 'Unable to identify the local Docker context.' }
    $result = Invoke-BundleChecked $Docker @('context', 'inspect', $context, '--format', '{{json .Endpoints.docker.Host}}') 'Checking local Docker endpoint'
    try { $endpoint = $result.Output.Trim() | ConvertFrom-Json }
    catch { throw 'Unable to inspect the Docker endpoint.' }
    if ($endpoint -isnot [string] -or -not $endpoint.StartsWith('npipe://', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Select the local Docker Desktop context first. This launcher will not manage a remote Docker engine.'
    }
    return $context
}

function Get-BundleEngineState {
    param([string]$Docker, [string]$Context)
    $result = Invoke-BundleCommand $Docker @('--context', $Context, 'info', '--format', '{{.OSType}}') 8
    if ($result.ExitCode -ne 0) { return 'unavailable' }
    $kind = $result.Output.Trim().ToLowerInvariant()
    if ($kind -eq 'linux') { return 'linux' }
    if ($kind -eq 'windows') { return 'windows' }
    return 'unavailable'
}

function Get-BundleDesktopPath {
    $candidates = @()
    foreach ($base in @($env:ProgramFiles, $env:ProgramW6432, $env:LOCALAPPDATA)) {
        if ($base) { $candidates += (Join-Path $base 'Docker\Docker\Docker Desktop.exe') }
    }
    foreach ($path in $candidates) {
        if (Test-Path -LiteralPath $path -PathType Leaf) { return $path }
    }
    throw 'Docker Desktop is not installed. Install it yourself, then retry; no software was installed.'
}

function Start-BundleDesktop {
    param([string]$Path)
    Start-Process -FilePath $Path -WindowStyle Hidden | Out-Null
}

function Wait-BundleEngine {
    param([string]$Docker, [string]$Context, [int]$TimeoutSeconds = 180)
    $state = Get-BundleEngineState $Docker $Context
    if ($state -eq 'linux') { return }
    if ($state -eq 'windows') { throw 'Switch Docker Desktop to Linux containers, then retry.' }
    Write-Host 'Starting installed Docker Desktop; waiting for the Linux engine...'
    Start-BundleDesktop (Get-BundleDesktopPath)
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    while ($timer.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        Start-Sleep -Seconds 2
        $state = Get-BundleEngineState $Docker $Context
        if ($state -eq 'linux') { return }
        if ($state -eq 'windows') { throw 'Switch Docker Desktop to Linux containers, then retry.' }
    }
    throw 'Docker Desktop did not become ready. Check its window and WSL/virtualization, then retry.'
}

function Find-BundlePython {
    foreach ($candidate in @(
        @{ Name = 'py.exe'; Prefix = @('-3') },
        @{ Name = 'python.exe'; Prefix = @() },
        @{ Name = 'python3.exe'; Prefix = @() }
    )) {
        $command = Get-Command $candidate.Name -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        $result = Invoke-BundleCommand $command.Source ($candidate.Prefix + @('-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)')) 15
        if ($result.ExitCode -eq 0) { return [pscustomobject]@{ File = $command.Source; Prefix = $candidate.Prefix } }
    }
    throw 'Install Python 3.10 or newer (with Python launcher or PATH enabled), then retry.'
}

function Get-BundleComposeArguments {
    param($Paths, [string]$Context)
    return @('--context', $Context, 'compose', '--project-directory', $Paths.Root,
             '--project-name', $Paths.Project, '--env-file', $Paths.Env, '-f', $Paths.Compose)
}

function Get-BundleHealth {
    Add-Type -AssemblyName System.Net.Http
    $handler = New-Object System.Net.Http.HttpClientHandler
    $handler.UseProxy = $false
    $handler.AllowAutoRedirect = $false
    $client = New-Object System.Net.Http.HttpClient($handler)
    $client.Timeout = [System.TimeSpan]::FromSeconds(8)
    $client.MaxResponseContentBufferSize = 65536
    $response = $null
    try {
        # Default completion buffers the bounded body within the same 8-second timeout.
        $response = $client.GetAsync('http://127.0.0.1:18080/bundle-health').GetAwaiter().GetResult()
        if ([int]$response.StatusCode -ne 200) { return $null }
        return ($response.Content.ReadAsStringAsync().GetAwaiter().GetResult() | ConvertFrom-Json)
    }
    catch { return $null }
    finally { if ($response) { $response.Dispose() }; $client.Dispose(); $handler.Dispose() }
}

function Test-BundleHealthReady {
    param($Health)
    if (-not $Health -or $Health.status -ne 'ready' -or -not $Health.services) { return $false }
    foreach ($name in @('astrbot', 'qwenpaw', 'napcat')) {
        $service = $Health.services.PSObject.Properties[$name]
        if (-not $service -or $service.Value.ready -isnot [bool] -or $service.Value.ready -ne $true) { return $false }
    }
    return $true
}

function Wait-BundleHealth {
    param([int]$TimeoutSeconds = 600)
    Write-Host 'Waiting for the gateway and all three native applications...'
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    while ($timer.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        if (Test-BundleHealthReady (Get-BundleHealth)) { return }
        Start-Sleep -Seconds 2
    }
    throw 'The bundle did not become ready. Containers and data were retained for diagnosis; run start again after checking Docker Desktop.'
}

function Wait-BundleQwenPaw {
    param([int]$TimeoutSeconds = 600)
    Write-Host 'Waiting for the QwenPaw default agent before setting up the bridge...'
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    while ($timer.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        $health = Get-BundleHealth
        if ($health -and $health.services) {
            $service = $health.services.PSObject.Properties['qwenpaw']
            if ($service -and $service.Value.ready -is [bool] -and $service.Value.ready -eq $true) { return }
        }
        Start-Sleep -Seconds 2
    }
    throw 'QwenPaw did not become ready for bridge setup. Containers and data were retained for diagnosis.'
}

function Get-BundleBootstrapScript {
    param([string]$ProjectRoot)
    $path = Join-Path $ProjectRoot 'deploy\bootstrap_qwenpaw.py'
    Assert-BundleNoLinks $path
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw 'The QwenPaw bridge setup script is missing. Extract the complete release again.'
    }
    if ((Get-Item -LiteralPath $path).Length -gt 1048576) { throw 'The QwenPaw bridge setup script is unexpectedly large.' }
    $encoding = New-Object System.Text.UTF8Encoding($false, $true)
    return [System.IO.File]::ReadAllText($path, $encoding)
}

function Open-BundlePage {
    Start-Process -FilePath 'http://localhost:18080/' | Out-Null
}

function Invoke-BundleStart {
    param([string]$ProjectRoot, [switch]$NoBrowser, [switch]$BuildQwenPaw, [int]$ReadyTimeoutSeconds = 600)
    $paths = Get-BundlePaths $ProjectRoot
    Assert-BundleRuntime $paths
    $bootstrap = Get-BundleBootstrapScript $paths.Root
    $docker = Get-BundleDockerPath
    $context = Get-BundleLocalContext $docker
    $python = Find-BundlePython
    Wait-BundleEngine $docker $context
    $null = Invoke-BundleChecked $docker @('--context', $context, 'compose', 'version') 'Checking Docker Compose'
    Write-Host 'Preparing private configuration; existing credentials and data are preserved...'
    $null = Invoke-BundleChecked $python.File ($python.Prefix + @($paths.Prepare, '--mode', 'fresh', '--root', $paths.Runtime)) 'Preparing bundle configuration'
    Assert-BundleRuntime $paths -RequirePrepared
    $compose = Get-BundleComposeArguments $paths $context
    $buildServices = @('gateway')
    if ($BuildQwenPaw) {
        $sourceCompose = Join-Path $paths.Root 'deploy\compose.qwenpaw.source.yaml'
        Assert-BundleNoLinks $sourceCompose
        if (-not (Test-Path -LiteralPath $sourceCompose -PathType Leaf)) {
            throw 'The optional QwenPaw source-build configuration is missing.'
        }
        $compose += @('-f', $sourceCompose)
        $buildServices += 'qwenpaw'
        Write-Host 'Optional QwenPaw source build selected; this can take much longer than pulling the official image.'
    }
    $null = Invoke-BundleChecked $docker ($compose + @('config', '--quiet')) 'Validating bundle configuration'
    Write-Host 'Pulling missing application images; existing fixed images are reused...'
    $null = Invoke-BundleChecked $docker ($compose + @('pull', '--ignore-buildable', '--policy', 'missing')) 'Pulling bundle images' 3600
    Write-Host 'Building the local gateway (and QwenPaw only if explicitly selected)...'
    $null = Invoke-BundleChecked $docker ($compose + @('build') + $buildServices) 'Building bundle images' 3600
    $null = Invoke-BundleChecked $docker ($compose + @('up', '-d')) 'Starting bundle services' 3600
    Wait-BundleQwenPaw $ReadyTimeoutSeconds
    Write-Host 'Checking the native bridge channel; existing disabled settings are preserved...'
    $null = Invoke-BundleChecked $docker ($compose + @('exec', '-T', 'qwenpaw', '/app/venv/bin/python', '-')) 'Setting up QwenPaw bridge' 240 -InputText $bootstrap
    Wait-BundleHealth $ReadyTimeoutSeconds
    Write-Host 'Ready: http://localhost:18080/'
    Write-Host 'First use: configure your model, log in to WeChat/QQ, and allow your user ID. Ready does not mean those accounts are already configured.'
    if (-not $NoBrowser) { Open-BundlePage }
}

if ($MyInvocation.InvocationName -ne '.') {
    $ErrorActionPreference = 'Stop'
    try {
        Invoke-BundleStart -ProjectRoot $PSScriptRoot -NoBrowser:$NoBrowser -BuildQwenPaw:$BuildQwenPaw -ReadyTimeoutSeconds $ReadyTimeoutSeconds
        exit 0
    }
    catch { Write-Host ('STOP: ' + $_.Exception.Message) -ForegroundColor Red; exit 1 }
}
