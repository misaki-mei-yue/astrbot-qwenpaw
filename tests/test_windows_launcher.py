"""Exercise the Windows entrypoints with mocked processes, never Docker builds."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")


def ps_string(value):
    return "'" + str(value).replace("'", "''") + "'"


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows PowerShell launcher test")
class WindowsLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bundle-launcher-")
        self.addCleanup(self.temp.cleanup)
        self.package = Path(self.temp.name).resolve() / "package with spaces"
        self.package.mkdir()
        (self.package / "deploy").mkdir()
        for name in ("start.ps1", "stop.ps1"):
            shutil.copyfile(ROOT / name, self.package / name)
        (self.package / "deploy/compose.bundle.yaml").write_text("services: {}\n")
        (self.package / "deploy/prepare.py").write_text("# mocked process only\n")

    def prepared(self):
        runtime = self.package / "runtime"
        runtime.mkdir(exist_ok=True)
        (runtime / ".combined-setup.json").write_text('{"format":1,"mode":"fresh"}')
        (runtime / ".env").write_text("BRIDGE_TOKEN=private-existing-sentinel\n")
        (runtime / "saved-login.db").write_bytes(b"existing-login-data")
        return runtime

    def run_ps(self, body, mocks=True):
        code = "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
        code += ". " + ps_string(self.package / "stop.ps1") + "\n"
        if mocks:
            code += r'''
$script:calls = @(); $script:FailPhase = ''; $script:Endpoint = 'npipe:////./pipe/dockerDesktopLinuxEngine'
$script:EngineState = 'linux'; $script:engineChecks = 0; $script:FailHealth = $false
$script:Opened = $false; $script:Healthy = $false; $script:DesktopStarted = $false
function Get-BundleDockerPath { return 'fake-docker' }
function Find-BundlePython { return [pscustomobject]@{File='fake-python';Prefix=@('-3')} }
function Get-BundleDesktopPath { return 'installed-desktop-fixture' }
function Start-BundleDesktop { param($Path); $script:DesktopStarted=$true }
function Start-Sleep { param($Seconds) }
function Invoke-BundleCommand {
    param($File, [string[]]$Arguments, $TimeoutSeconds=60, $Phase='')
    $script:calls += [pscustomobject]@{file=$File;args=@($Arguments);phase=$Phase;timeout=$TimeoutSeconds}
    if ($Phase -and $Phase -eq $script:FailPhase) {
        return [pscustomobject]@{ExitCode=2;Output='private-secret-from-native-output';Error='another-private-secret'}
    }
    $output=''
    if ($Arguments[0] -eq 'context' -and $Arguments[1] -eq 'show') { $output='desktop-linux' }
    elseif ($Arguments[0] -eq 'context' -and $Arguments[1] -eq 'inspect') { $output=ConvertTo-Json $script:Endpoint -Compress }
    elseif ($Arguments -contains 'info') {
        $script:engineChecks++
        if ($script:EngineState -eq 'delayed') {
            if ($script:engineChecks -eq 1) { return [pscustomobject]@{ExitCode=1;Output='';Error='not ready'} }
            $output='linux'
        } else { $output=$script:EngineState }
    }
    elseif ($File -eq 'fake-python') {
        $at=[Array]::IndexOf($Arguments, '--root'); $destination=$Arguments[$at+1]
        $null=[IO.Directory]::CreateDirectory($destination)
        $marker=Join-Path $destination '.combined-setup.json'
        $envFile=Join-Path $destination '.env'
        if (-not (Test-Path -LiteralPath $marker)) { [IO.File]::WriteAllText($marker, '{"format":1,"mode":"fresh"}') }
        if (-not (Test-Path -LiteralPath $envFile)) { [IO.File]::WriteAllText($envFile, 'BRIDGE_TOKEN=private-existing-sentinel') }
    }
    return [pscustomobject]@{ExitCode=0;Output=$output;Error=''}
}
function Wait-BundleHealth {
    param($TimeoutSeconds)
    if ($script:FailHealth) { throw 'health not ready' }
    $script:Healthy=$true
}
function Open-BundlePage {
    if (-not $script:Healthy) { throw 'browser opened before health' }
    $script:Opened=$true
}
'''
        code += body
        encoded = base64.b64encode(code.encode("utf-16le")).decode("ascii")
        result = subprocess.run(
            [POWERSHELL, "-NoLogo", "-NoProfile", "-EncodedCommand", encoded],
            capture_output=True, encoding="utf-8", errors="replace", timeout=35,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = [line[7:] for line in result.stdout.splitlines() if line.startswith("RESULT:")]
        self.assertEqual(len(lines), 1, result.stdout + result.stderr)
        return json.loads(lines[0]), result.stdout + result.stderr

    def start_body(self, setup=""):
        return setup + "\n$errorText=''; try { Invoke-BundleStart -ProjectRoot " + ps_string(self.package) + r''' } catch { $errorText=$_.Exception.Message }
[Console]::WriteLine('RESULT:' + (ConvertTo-Json @{error=$errorText;calls=@($script:calls);opened=$script:Opened;healthy=$script:Healthy;desktop=$script:DesktopStarted} -Depth 8 -Compress))
'''

    def test_unknown_runtime_is_rejected_before_any_process_and_preserves_bytes(self):
        runtime = self.package / "runtime"
        runtime.mkdir()
        sensitive = runtime / "old-login.db"
        sensitive.write_bytes(b"unknown-existing-data")
        result, _ = self.run_ps(self.start_body())
        self.assertIn("unrecognized data", result["error"])
        self.assertEqual(result["calls"], [])
        self.assertEqual(sensitive.read_bytes(), b"unknown-existing-data")
        self.assertFalse((runtime / ".env").exists())

    def test_wrong_mode_is_rejected_without_resetting_credentials(self):
        runtime = self.prepared()
        marker = runtime / ".combined-setup.json"
        marker.write_text('{"format":1,"mode":"existing"}')
        before = (runtime / ".env").read_bytes()
        result, _ = self.run_ps(self.start_body())
        self.assertIn("different deployment", result["error"])
        self.assertEqual(result["calls"], [])
        self.assertEqual((runtime / ".env").read_bytes(), before)

    def test_remote_context_never_prepares_or_starts_services(self):
        result, _ = self.run_ps(self.start_body("$script:Endpoint='ssh://remote-fixture'"))
        self.assertIn("remote Docker", result["error"])
        self.assertFalse(any(call["file"] == "fake-python" for call in result["calls"]))
        self.assertFalse((self.package / "runtime").exists())
        self.assertFalse(result["opened"])

    def test_windows_engine_is_rejected_without_starting_desktop(self):
        result, _ = self.run_ps(self.start_body("$script:EngineState='windows'"))
        self.assertIn("Linux containers", result["error"])
        self.assertFalse(result["desktop"])
        self.assertFalse(any(call["file"] == "fake-python" for call in result["calls"]))

    def test_installed_desktop_can_be_started_when_engine_is_unavailable(self):
        result, _ = self.run_ps(self.start_body("$script:EngineState='delayed'"))
        self.assertEqual(result["error"], "")
        self.assertTrue(result["desktop"])
        self.assertTrue(result["opened"])

    def test_prepare_failure_stops_before_compose_build_and_hides_native_secrets(self):
        result, output = self.run_ps(self.start_body("$script:FailPhase='Preparing bundle configuration'"))
        self.assertIn("Preparing bundle configuration failed", result["error"])
        self.assertFalse(any("build" in call["args"] or "up" in call["args"] for call in result["calls"]))
        self.assertNotIn("private-secret-from-native-output", output)
        self.assertNotIn("another-private-secret", output)

    def test_config_failure_stops_before_build_and_keeps_existing_data(self):
        runtime = self.prepared()
        originals = {name: (runtime / name).read_bytes() for name in (".env", "saved-login.db")}
        result, _ = self.run_ps(self.start_body("$script:FailPhase='Validating bundle configuration'"))
        self.assertIn("Validating bundle configuration failed", result["error"])
        self.assertFalse(any("build" in call["args"] or "up" in call["args"] for call in result["calls"]))
        for name, value in originals.items():
            self.assertEqual((runtime / name).read_bytes(), value)

    def test_build_failure_does_not_start_or_open_browser(self):
        result, _ = self.run_ps(self.start_body("$script:FailPhase='Building bundle images'"))
        self.assertIn("Building bundle images failed", result["error"])
        self.assertFalse(any("up" in call["args"] for call in result["calls"]))
        self.assertFalse(result["opened"])

    def test_failed_health_preserves_running_services_and_never_claims_ready(self):
        result, output = self.run_ps(self.start_body("$script:FailHealth=$true"))
        self.assertEqual(result["error"], "health not ready")
        self.assertTrue(any("up" in call["args"] for call in result["calls"]))
        self.assertFalse(any("stop" in call["args"] or "down" in call["args"] for call in result["calls"]))
        self.assertFalse(result["opened"])
        self.assertNotIn("Ready: http", output)
        self.assertTrue((self.package / "runtime/.env").is_file())

    def test_success_uses_only_bundle_compose_with_explicit_project_and_health(self):
        result, _ = self.run_ps(self.start_body())
        self.assertEqual(result["error"], "")
        self.assertTrue(result["opened"])
        self.assertTrue(result["healthy"])
        calls = result["calls"]
        prepare_call = next(call for call in calls if call["file"] == "fake-python")
        self.assertIn(str(self.package / "deploy/prepare.py"), prepare_call["args"])
        self.assertEqual(prepare_call["args"][-4:], ["--mode", "fresh", "--root", str(self.package / "runtime")])
        commands = [call for call in calls if "--project-name" in call["args"]]
        self.assertEqual(len(commands), 3)
        names = set()
        for call in commands:
            args = call["args"]
            names.add(args[args.index("--project-name") + 1])
            self.assertEqual(args[args.index("-f") + 1], str(self.package / "deploy/compose.bundle.yaml"))
            self.assertEqual(args[args.index("--env-file") + 1], str(self.package / "runtime/.env"))
            self.assertEqual(args[:2], ["--context", "desktop-linux"])
        self.assertEqual(len(names), 1)
        self.assertRegex(names.pop(), r"^astrbot-qwenpaw-bundle-[0-9a-f]{10}$")
        self.assertEqual(commands[0]["args"][-2:], ["config", "--quiet"])
        self.assertEqual(commands[1]["timeout"], 3600)
        self.assertEqual(commands[2]["timeout"], 3600)

    def test_stop_only_targets_current_package_and_preserves_files(self):
        runtime = self.prepared()
        before = {name: (runtime / name).read_bytes() for name in (".env", "saved-login.db")}
        body = "Invoke-BundleStop -ProjectRoot " + ps_string(self.package) + r'''
[Console]::WriteLine('RESULT:' + (ConvertTo-Json @{calls=@($script:calls);desktop=$script:DesktopStarted} -Depth 8 -Compress))
'''
        result, _ = self.run_ps(body)
        commands = [call for call in result["calls"] if "--project-name" in call["args"]]
        self.assertEqual([call["args"][-1] for call in commands], ["--quiet", "stop"])
        self.assertFalse(any(call["file"] == "fake-python" for call in result["calls"]))
        self.assertFalse(result["desktop"])
        for name, data in before.items():
            self.assertEqual((runtime / name).read_bytes(), data)

    def test_stop_unprepared_package_runs_no_process(self):
        body = "$errorText=''; try { Invoke-BundleStop -ProjectRoot " + ps_string(self.package) + r''' } catch { $errorText=$_.Exception.Message }
[Console]::WriteLine('RESULT:' + (ConvertTo-Json @{error=$errorText;calls=@($script:calls)} -Depth 8 -Compress))
'''
        result, _ = self.run_ps(body)
        self.assertIn("not been started", result["error"])
        self.assertEqual(result["calls"], [])

    def test_health_requires_explicit_booleans_for_all_three_services(self):
        body = r'''
$valid='{"status":"ready","services":{"astrbot":{"ready":true},"qwenpaw":{"ready":true},"napcat":{"ready":true}}}' | ConvertFrom-Json
$stringBool='{"status":"ready","services":{"astrbot":{"ready":"true"},"qwenpaw":{"ready":true},"napcat":{"ready":true}}}' | ConvertFrom-Json
$missing='{"status":"ready","services":{"astrbot":{"ready":true},"qwenpaw":{"ready":true}}}' | ConvertFrom-Json
$starting='{"status":"starting","services":{"astrbot":{"ready":true},"qwenpaw":{"ready":true},"napcat":{"ready":true}}}' | ConvertFrom-Json
[Console]::WriteLine('RESULT:' + (ConvertTo-Json @((Test-BundleHealthReady $valid),(Test-BundleHealthReady $stringBool),(Test-BundleHealthReady $missing),(Test-BundleHealthReady $starting),(Test-BundleHealthReady $null)) -Compress))
'''
        result, _ = self.run_ps(body, mocks=False)
        self.assertEqual(result, [True, False, False, False, False])

    def test_native_argument_roundtrip_preserves_spaces_quotes_and_backslashes(self):
        values = ["with spaces", 'a"quoted"value', "trailing slash \\", "$(not-a-command)", ""]
        arguments = ["-c", "import json,sys; print(json.dumps(sys.argv[1:]))", *values]
        literal = "@(" + ",".join(ps_string(value) for value in arguments) + ")"
        body = "$result=Invoke-BundleCommand -File " + ps_string(sys.executable) + " -Arguments " + literal + r'''
if ($result.ExitCode -ne 0) { throw 'argument fixture process failed' }
[Console]::WriteLine('RESULT:' + $result.Output.Trim())
'''
        result, _ = self.run_ps(body, mocks=False)
        self.assertEqual(result, values)

    def test_scripts_parse_on_installed_powershell(self):
        body = ""
        for name in ("start.ps1", "stop.ps1"):
            body += "$tokens=$null; $errors=$null; $null=[Management.Automation.Language.Parser]::ParseFile(" + ps_string(self.package / name) + ", [ref]$tokens, [ref]$errors); if ($errors.Count) { throw $errors[0].Message };\n"
        body += "[Console]::WriteLine('RESULT:true')"
        result, _ = self.run_ps(body, mocks=False)
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
