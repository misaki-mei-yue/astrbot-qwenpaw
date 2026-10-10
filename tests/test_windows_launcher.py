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
        (self.package / "deploy/compose.qwenpaw.source.yaml").write_text("services: {}\n")
        (self.package / "deploy/prepare.py").write_text("# mocked process only\n")
        (self.package / "deploy/bootstrap_qwenpaw.py").write_text("# bootstrap fixture only\n", encoding="utf-8", newline="")

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
$script:QwenReady = $false; $script:FailQwenReady = $false
function Get-BundleDockerPath { return 'fake-docker' }
function Find-BundlePython { return [pscustomobject]@{File='fake-python';Prefix=@('-3')} }
function Get-BundleDesktopPath { return 'installed-desktop-fixture' }
function Start-BundleDesktop { param($Path); $script:DesktopStarted=$true }
function Start-Sleep { param($Seconds) }
function Invoke-BundleCommand {
    param($File, [string[]]$Arguments, $TimeoutSeconds=60, $Phase='', $InputText=$null)
    $script:calls += [pscustomobject]@{file=$File;args=@($Arguments);phase=$Phase;timeout=$TimeoutSeconds;inputLength=([string]$InputText).Length;qwenReady=$script:QwenReady;allReady=$script:Healthy}
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
function Wait-BundleQwenPaw {
    param($TimeoutSeconds)
    if ($script:FailQwenReady) { throw 'qwen not ready' }
    $script:QwenReady=$true
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

    def start_body(self, setup="", arguments=""):
        return setup + "\n$errorText=''; try { Invoke-BundleStart -ProjectRoot " + ps_string(self.package) + " " + arguments + r''' } catch { $errorText=$_.Exception.Message }
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

    def test_pull_failure_never_builds_or_starts_and_preserves_existing_data(self):
        runtime = self.prepared()
        before = {name: (runtime / name).read_bytes() for name in (".env", "saved-login.db")}
        result, output = self.run_ps(self.start_body("$script:FailPhase='Pulling bundle images'"))
        self.assertIn("Pulling bundle images failed", result["error"])
        self.assertFalse(any("build" in call["args"] or "up" in call["args"] for call in result["calls"]))
        self.assertFalse(result["opened"])
        self.assertNotIn("private-secret-from-native-output", output)
        for name, data in before.items():
            self.assertEqual((runtime / name).read_bytes(), data)

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
        self.assertEqual(len(commands), 5)
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
        self.assertEqual(commands[1]["args"][-4:], ["pull", "--ignore-buildable", "--policy", "missing"])
        self.assertEqual(commands[1]["timeout"], 3600)
        self.assertEqual(commands[2]["args"][-2:], ["build", "gateway"])
        self.assertEqual(commands[2]["timeout"], 3600)
        self.assertEqual(commands[3]["args"][-2:], ["up", "-d"])
        self.assertEqual(commands[3]["timeout"], 3600)
        self.assertEqual(commands[4]["args"][-5:], ["exec", "-T", "qwenpaw", "/app/venv/bin/python", "-"])
        self.assertEqual(commands[4]["timeout"], 240)
        self.assertEqual(commands[4]["inputLength"], len("# bootstrap fixture only\n"))
        self.assertTrue(commands[4]["qwenReady"])
        self.assertFalse(commands[4]["allReady"])

    def test_explicit_source_build_uses_overlay_and_builds_qwenpaw(self):
        result, _ = self.run_ps(self.start_body(arguments="-BuildQwenPaw"))
        self.assertEqual(result["error"], "")
        commands = [call for call in result["calls"] if "--project-name" in call["args"]]
        self.assertEqual(len(commands), 5)
        for call in commands:
            args = call["args"]
            self.assertEqual(args.count("-f"), 2)
            self.assertIn(str(self.package / "deploy/compose.qwenpaw.source.yaml"), args)
        self.assertEqual(commands[2]["args"][-3:], ["build", "gateway", "qwenpaw"])

    def test_missing_source_overlay_fails_before_pull_or_build(self):
        (self.package / "deploy/compose.qwenpaw.source.yaml").unlink()
        result, _ = self.run_ps(self.start_body(arguments="-BuildQwenPaw"))
        self.assertIn("source-build configuration is missing", result["error"])
        self.assertFalse(any("pull" in call["args"] or "build" in call["args"] or "up" in call["args"] for call in result["calls"]))
        self.assertFalse(result["opened"])

    def test_missing_bootstrap_script_stops_before_preparing_runtime(self):
        (self.package / "deploy/bootstrap_qwenpaw.py").unlink()
        result, _ = self.run_ps(self.start_body())
        self.assertIn("bridge setup script is missing", result["error"])
        self.assertEqual(result["calls"], [])
        self.assertFalse((self.package / "runtime").exists())

    def test_qwen_readiness_failure_never_runs_bootstrap_or_opens_browser(self):
        result, output = self.run_ps(self.start_body("$script:FailQwenReady=$true"))
        self.assertEqual(result["error"], "qwen not ready")
        self.assertTrue(any("up" in call["args"] for call in result["calls"]))
        self.assertFalse(any("exec" in call["args"] or "stop" in call["args"] for call in result["calls"]))
        self.assertFalse(result["opened"])
        self.assertNotIn("Ready: http", output)

    def test_bootstrap_failure_keeps_services_data_and_native_secrets_private(self):
        runtime = self.prepared()
        before = {name: (runtime / name).read_bytes() for name in (".env", "saved-login.db")}
        result, output = self.run_ps(self.start_body("$script:FailPhase='Setting up QwenPaw bridge'"))
        self.assertIn("Setting up QwenPaw bridge failed", result["error"])
        self.assertFalse(result["opened"])
        self.assertFalse(any("stop" in call["args"] or "down" in call["args"] for call in result["calls"]))
        self.assertNotIn("private-secret-from-native-output", output)
        self.assertNotIn("another-private-secret", output)
        for name, data in before.items():
            self.assertEqual((runtime / name).read_bytes(), data)

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

    def test_native_stdin_is_utf8_exact_and_not_in_arguments(self):
        payload = "# bridge setup\nprint('图片与文件')\n" + "x" * 100000
        payload_file = self.package / "stdin fixture.txt"
        payload_file.write_text(payload, encoding="utf-8", newline="")
        arguments = ["-c", "import sys,hashlib,json; raw=sys.stdin.buffer.read(); print(json.dumps({'sha256':hashlib.sha256(raw).hexdigest(), 'args':sys.argv[1:]}))"]
        literal = "@(" + ",".join(ps_string(value) for value in arguments) + ")"
        body = "$payload=[IO.File]::ReadAllText(" + ps_string(payload_file) + ", [Text.Encoding]::UTF8);\n"
        body += "$initialCodePage=[Console]::InputEncoding.CodePage; $initialPreamble=[BitConverter]::ToString([Console]::InputEncoding.GetPreamble());\n"
        body += "$result=Invoke-BundleCommand -File " + ps_string(sys.executable) + " -Arguments " + literal + r''' -InputText $payload
if ($result.ExitCode -ne 0) { throw 'stdin fixture process failed' }
if ([Console]::InputEncoding.CodePage -ne $initialCodePage -or [BitConverter]::ToString([Console]::InputEncoding.GetPreamble()) -ne $initialPreamble) { throw 'console input encoding was not restored' }
[Console]::WriteLine('RESULT:' + $result.Output.Trim())
'''
        import hashlib
        for encoding in ("default", "utf8-with-bom"):
            with self.subTest(console_input_encoding=encoding):
                invocation = body
                if encoding == "utf8-with-bom":
                    # Windows CI may use a UTF-8 console encoding whose StreamWriter
                    # emits its preamble before any direct BaseStream write.
                    invocation = r'''
$previousEncoding=[Console]::InputEncoding
[Console]::InputEncoding=(New-Object Text.UTF8Encoding($true))
try {
''' + body + r'''
} finally { [Console]::InputEncoding=$previousEncoding }
'''
                result, _ = self.run_ps(invocation, mocks=False)
                self.assertEqual(result["sha256"], hashlib.sha256(payload.encode("utf-8")).hexdigest())
                self.assertEqual(result["args"], [])

    def test_scripts_parse_on_installed_powershell(self):
        body = ""
        for name in ("start.ps1", "stop.ps1"):
            body += "$tokens=$null; $errors=$null; $null=[Management.Automation.Language.Parser]::ParseFile(" + ps_string(self.package / name) + ", [ref]$tokens, [ref]$errors); if ($errors.Count) { throw $errors[0].Message };\n"
        body += "[Console]::WriteLine('RESULT:true')"
        result, _ = self.run_ps(body, mocks=False)
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
