import hashlib
import json
import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import unittest


MODULE = pathlib.Path(__file__).resolve().parents[1]
ROOT = MODULE.parents[1]
SUPPORTED_OPENCLAW_VERSION = "2026.7.2-beta.4"
SUPPORTED_OPENCLAW_BUILD = "5e63b365d4d3e62ef600b783fad7c5043b6f4738"
SUPPORTED_OPENCLAW_SHASUM = "95ed4f87ce8e8500e0474e07d0fa1e79616a2055"
SUPPORTED_OPENCLAW_TARBALL_SHA256 = "822b3e5cec8bd41a7d2f4ff1709f1e9e789c6e5e4e23e058443b22f8f6e07ead"


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wlg-install-", dir="/root/disposable")
        self.bin_temp = tempfile.TemporaryDirectory(prefix="wlg-openclaw-bin-", dir="/root/disposable")
        self.target = pathlib.Path(self.temp.name)
        (self.target / "openclaw.json").write_text('{}\n', encoding="utf-8")
        self.openclaw_bin = pathlib.Path(self.bin_temp.name) / "openclaw-exact"
        self.openclaw_bin.write_text(
            "#!/usr/bin/env bash\n"
            f"printf 'OpenClaw {SUPPORTED_OPENCLAW_VERSION} ({SUPPORTED_OPENCLAW_BUILD[:7]})\\n'\n",
            encoding="utf-8",
        )
        self.openclaw_bin.chmod(0o755)
        self.install_env = {**os.environ, "OPENCLAW_BIN": str(self.openclaw_bin)}
        self.baseline = hashlib.sha256((self.target / "openclaw.json").read_bytes()).hexdigest()

    def tearDown(self):
        self.temp.cleanup()
        self.bin_temp.cleanup()

    def run_script(self, name, check=True, env=None):
        return subprocess.run(
            ["bash", str(MODULE / name), str(self.target)],
            env=env or self.install_env,
            text=True, capture_output=True, check=check,
        )

    @staticmethod
    def snapshot(root):
        result = {}
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            mode = stat.S_IMODE(path.lstat().st_mode)
            if path.is_symlink():
                result[relative] = ("symlink", mode, os.readlink(path))
            elif path.is_dir():
                result[relative] = ("dir", mode)
            else:
                result[relative] = ("file", mode, hashlib.sha256(path.read_bytes()).hexdigest())
        return result

    def test_install_is_deterministic_and_idempotent(self):
        installed = self.run_script("install.sh")
        self.assertIn(f"openclawVersion={SUPPORTED_OPENCLAW_VERSION}", installed.stdout)
        manifest = self.target / ".openclaw-overlay/modules/workspace-lane-guard/manifest.tsv"
        first = self.snapshot(self.target)
        self.run_script("install.sh")
        self.assertEqual(first, self.snapshot(self.target))
        self.assertTrue((self.target / "extensions/workspace-lane-guard/openclaw.plugin.json").is_file())
        self.assertTrue((self.target / "scripts/lane-guard-healthmon.mjs").stat().st_mode & stat.S_IXUSR)
        self.assertEqual(self.baseline, hashlib.sha256((self.target / "openclaw.json").read_bytes()).hexdigest())

        with tempfile.TemporaryDirectory(prefix="wlg-install-peer-", dir="/root/disposable") as peer_name:
            peer = pathlib.Path(peer_name)
            (peer / "openclaw.json").write_text('{}\n', encoding="utf-8")
            subprocess.run(
                ["bash", str(MODULE / "install.sh"), str(peer)],
                env=self.install_env, check=True, capture_output=True, text=True,
            )
            self.assertEqual(first, self.snapshot(peer))

    def test_release_manifest_and_plugin_schema_declare_only_exact_version(self):
        module_manifest = (MODULE / "module.yaml").read_text(encoding="utf-8")
        self.assertIn(f"version: {SUPPORTED_OPENCLAW_VERSION}", module_manifest)
        self.assertIn(f"build: {SUPPORTED_OPENCLAW_BUILD}", module_manifest)
        self.assertIn(f"npm_shasum: {SUPPORTED_OPENCLAW_SHASUM}", module_manifest)
        self.assertIn(f"tarball_sha256: {SUPPORTED_OPENCLAW_TARBALL_SHA256}", module_manifest)
        self.assertIn("mode: exact-version-only", module_manifest)
        self.assertNotIn("dist_tag:", module_manifest)
        self.assertNotIn("2026.7.1-2", module_manifest)

        plugin_manifest = json.loads((MODULE / "plugin/openclaw.plugin.json").read_text(encoding="utf-8"))
        declared = plugin_manifest["configSchema"]["properties"]["openclawVersion"]["enum"]
        self.assertEqual(declared, [SUPPORTED_OPENCLAW_VERSION])

    def test_unsupported_openclaw_version_fails_before_target_mutation(self):
        unsupported_bin = self.target / "openclaw-unsupported"
        unsupported_bin.write_text(
            "#!/usr/bin/env bash\nprintf 'OpenClaw 2026.7.1-2 (0000000)\\n'\n",
            encoding="utf-8",
        )
        unsupported_bin.chmod(0o755)
        before = self.snapshot(self.target)
        result = self.run_script(
            "install.sh",
            check=False,
            env={**os.environ, "OPENCLAW_BIN": str(unsupported_bin)},
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn(
            f"UNSUPPORTED_OPENCLAW_VERSION expected={SUPPORTED_OPENCLAW_VERSION} actual=2026.7.1-2",
            result.stderr,
        )
        self.assertEqual(before, self.snapshot(self.target))
        self.assertFalse((self.target / ".openclaw-overlay").exists())

    def test_uninstall_restores_preexisting_files_and_config(self):
        prior = self.target / "scripts/workspace-lane-control.mjs"
        prior.parent.mkdir(parents=True)
        prior.write_text("prior\n", encoding="utf-8")
        prior.chmod(0o600)
        before = self.snapshot(self.target)
        self.run_script("install.sh")
        (self.target / "openclaw.json").write_text('{"changedAfterInstall":true}\n', encoding="utf-8")
        self.run_script("uninstall.sh")
        self.assertEqual(before, self.snapshot(self.target))

    def test_invalid_configuration_fails_closed(self):
        self.run_script("install.sh")
        invalid = {
            "plugins": {
                "allow": ["workspace-lane-guard"],
                "entries": {
                    "workspace-lane-guard": {
                        "enabled": True,
                        "config": {
                            "stateDir": str(self.target),
                            "openclawVersion": "not-a-pinned-version",
                            "targets": [{
                                "agentId": "worker",
                                "workspaceRoot": str(self.target),
                                "access": "ro",
                                "tools": ["read"],
                                "model": "synthetic/model",
                                "thinking": "off",
                            }],
                        },
                    },
                },
            },
        }
        (self.target / "openclaw.json").write_text(json.dumps(invalid), encoding="utf-8")
        config_before_validation = (self.target / "openclaw.json").read_bytes()
        manifest = self.target / ".openclaw-overlay/modules/workspace-lane-guard/manifest.tsv"
        manifest_before_validation = manifest.read_bytes()
        result = subprocess.run(
            ["openclaw", "config", "validate"],
            env={**os.environ, "OPENCLAW_STATE_DIR": str(self.target)},
            text=True, capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("openclawVersion", result.stdout + result.stderr)
        self.assertEqual(config_before_validation, (self.target / "openclaw.json").read_bytes())
        self.assertEqual(manifest_before_validation, manifest.read_bytes())
        self.run_script("uninstall.sh")
        self.assertEqual(self.baseline, hashlib.sha256((self.target / "openclaw.json").read_bytes()).hexdigest())

    def test_uninstall_refuses_modified_installed_file(self):
        self.run_script("install.sh")
        changed = self.target / "scripts/workspace-lane-control.mjs"
        changed.write_text("changed\n", encoding="utf-8")
        result = self.run_script("uninstall.sh", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing uninstall", result.stderr)
        self.assertTrue(changed.exists())

    def test_root_dispatch_supports_explicit_module_only(self):
        subprocess.run(
            ["bash", str(ROOT / "install.sh"), str(self.target), "workspace-lane-guard"],
            env=self.install_env, check=True,
        )
        subprocess.run(
            ["bash", str(ROOT / "uninstall.sh"), str(self.target), "workspace-lane-guard"],
            env=self.install_env, check=True,
        )
        self.assertFalse((self.target / "extensions/workspace-lane-guard").exists())

    def test_pinned_cold_and_runtime_inspection_use_public_plugin_sdk(self):
        config = {
            "agents": {
                "defaults": {"subagents": {
                    "requireAgentId": True,
                    "maxSpawnDepth": 1,
                    "maxConcurrent": 4,
                    "maxChildrenPerAgent": 2,
                    "runTimeoutSeconds": 14400,
                    "archiveAfterMinutes": 60,
                }},
                "entries": {
                    "main": {"subagents": {"allowAgents": ["worker"]}},
                    "worker": {
                        "workspace": str(self.target),
                        "model": "gpt-5",
                        "thinkingDefault": "xhigh",
                        "sandbox": {"mode": "all", "workspaceAccess": "ro", "scope": "session"},
                        "tools": {
                            "allow": ["read"],
                            "sandbox": {"tools": {"allow": ["read"]}},
                        },
                    },
                },
            },
            "plugins": {"entries": {"workspace-lane-guard": {
                "enabled": True,
                "config": {
                    "stateDir": str(self.target),
                    "openclawVersion": "2026.7.2-beta.4",
                    "targets": [{
                        "agentId": "worker",
                        "workspaceRoot": str(self.target),
                        "access": "ro",
                        "tools": ["read"],
                        "model": "gpt-5",
                        "thinking": "extra-high",
                    }],
                },
            }}},
        }
        (self.target / "openclaw.json").write_text(json.dumps(config), encoding="utf-8")
        self.run_script("install.sh")
        env = {**os.environ, "OPENCLAW_STATE_DIR": str(self.target)}
        cold = subprocess.run(
            ["openclaw", "plugins", "inspect", "workspace-lane-guard", "--json"],
            env=env, text=True, capture_output=True, check=True,
        )
        cold_json = json.loads(cold.stdout)
        self.assertFalse(cold_json["plugin"]["imported"])
        self.assertEqual(
            cold_json["plugin"]["contracts"]["trustedToolPolicies"],
            ["workspace-lane-admission"],
        )
        runtime = subprocess.run(
            ["openclaw", "plugins", "inspect", "workspace-lane-guard", "--runtime", "--json"],
            env=env, text=True, capture_output=True, check=True,
        )
        runtime_json = json.loads(runtime.stdout)
        self.assertTrue(runtime_json["plugin"]["imported"])
        self.assertEqual(runtime_json["plugin"]["hookCount"], 4)
        for source in (MODULE / "plugin/src").glob("*.ts"):
            text = source.read_text(encoding="utf-8")
            self.assertNotIn("openclaw/dist", text)
            self.assertNotIn("node_modules/openclaw/dist", text)


if __name__ == "__main__":
    unittest.main()
