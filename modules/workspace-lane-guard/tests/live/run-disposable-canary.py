#!/usr/bin/env python3
"""Fresh disposable Gateway canary. Emits bounded JSON and always removes its root."""

import argparse
import json
import os
import pathlib
import secrets
import shutil
import socket
import subprocess
import tempfile
import time


MODULE = pathlib.Path(__file__).resolve().parents[2]
MOCK = pathlib.Path(__file__).with_name("mock-provider.mjs")


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def wait_port(port, process, deadline=20):
    until = time.time() + deadline
    while time.time() < until:
        if process.poll() is not None:
            raise RuntimeError(f"process exited before port {port} opened")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"port {port} did not open")


def run_json(args, env, timeout=120):
    result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"command failed ({result.returncode}): {args[:3]}: {result.stderr[-800:]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"non-JSON result from {args[:3]}: {result.stdout[-800:]}") from error


def subagent_tasks(value):
    records = []

    def visit(item):
        if isinstance(item, dict):
            if item.get("runtime") == "subagent" and isinstance(item.get("status"), str):
                records.append(item)
            for nested in item.values():
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)
    return records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--openclaw", default="openclaw")
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    root = pathlib.Path(tempfile.mkdtemp(prefix="openclaw-v64-canary-", dir="/root/disposable"))
    provider = gateway = None
    try:
        state = root / "state"
        main_workspace = root / "workspaces/main"
        target_workspace = root / "workspaces/target"
        main_workspace.mkdir(parents=True)
        target_workspace.mkdir(parents=True)
        state.mkdir()
        gateway_port, provider_port = free_port(), free_port()
        token = secrets.token_urlsafe(32)
        sentinel = f"WLG_CHILD_ONLY_{secrets.token_hex(8)}"
        model = "synthetic/deterministic"
        config = {
            "gateway": {
                "mode": "local", "bind": "loopback", "port": gateway_port,
                "auth": {"mode": "token", "token": token},
                "remote": {"url": f"ws://127.0.0.1:{gateway_port}", "token": token},
            },
            "models": {"mode": "merge", "providers": {"synthetic": {
                "baseUrl": f"http://127.0.0.1:{provider_port}/v1",
                "apiKey": "synthetic-local", "api": "openai-completions",
                "models": [{
                    "id": "deterministic", "name": "Deterministic", "reasoning": False,
                    "input": ["text"], "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                    "contextWindow": 32000, "maxTokens": 4096,
                }],
            }}},
            "tools": {"exec": {"mode": "full"}},
            "agents": {
                "defaults": {
                    "model": {"primary": model}, "workspace": str(main_workspace),
                    "thinkingDefault": "off", "timeoutSeconds": 180, "maxConcurrent": 4,
                    "subagents": {
                        "requireAgentId": True, "maxSpawnDepth": 1, "maxConcurrent": 4,
                        "maxChildrenPerAgent": 2, "runTimeoutSeconds": 14400,
                        "archiveAfterMinutes": 60,
                    },
                },
                "entries": {
                    "main": {
                        "workspace": str(main_workspace), "model": model, "thinkingDefault": "off",
                        "subagents": {"allowAgents": ["target"]},
                    },
                    "target": {
                        "workspace": str(target_workspace), "model": model, "thinkingDefault": "off",
                        "sandbox": {"mode": "all", "workspaceAccess": "rw", "scope": "session"},
                        "tools": {"allow": ["exec"], "sandbox": {"tools": {"allow": ["exec"]}}},
                    },
                },
            },
            "plugins": {
                "allow": ["workspace-lane-guard"],
                "entries": {"workspace-lane-guard": {
                    "enabled": True,
                    "config": {
                        "stateDir": str(state), "openclawVersion": args.expected_version,
                        "acquisitionDeadlineMs": 500,
                        "targets": [{
                            "agentId": "target", "workspaceRoot": str(target_workspace),
                            "access": "rw", "tools": ["exec"], "model": model, "thinking": "off",
                        }],
                    },
                }},
            },
        }
        (state / "openclaw.json").write_text(json.dumps(config), encoding="utf-8")
        subprocess.run(["bash", str(MODULE / "install.sh"), str(state)], check=True, capture_output=True, text=True)
        env = {
            **os.environ,
            "OPENCLAW_STATE_DIR": str(state),
            "OPENCLAW_GATEWAY_TOKEN": token,
            "OPENCLAW_GATEWAY_PORT": str(gateway_port),
            "OPENCLAW_GATEWAY_URL": f"ws://127.0.0.1:{gateway_port}",
        }
        version = subprocess.run([args.openclaw, "--version"], text=True, capture_output=True, check=True).stdout
        if args.expected_version not in version:
            raise RuntimeError(f"version mismatch: expected {args.expected_version}, got {version.strip()}")
        provider = subprocess.Popen(
            ["node", str(MOCK)],
            env={**env, "WLG_MOCK_PORT": str(provider_port), "WLG_SENTINEL": sentinel},
            stdout=None if args.verbose else subprocess.DEVNULL,
            stderr=None if args.verbose else subprocess.PIPE,
            text=True,
        )
        wait_port(provider_port, provider)
        gateway = subprocess.Popen(
            [args.openclaw, "gateway", "run", "--port", str(gateway_port), "--bind", "loopback", "--auth", "token", "--token", token],
            env=env,
            stdout=None if args.verbose else subprocess.DEVNULL,
            stderr=None if args.verbose else subprocess.PIPE,
            text=True,
        )
        wait_port(gateway_port, gateway, 30)
        inspect = run_json([args.openclaw, "plugins", "inspect", "workspace-lane-guard", "--json"], env)
        if inspect["plugin"]["imported"]:
            raise RuntimeError("cold inspect imported runtime")
        session_key = "agent:main:wlg-canary"
        noop = run_json([args.openclaw, "agent", "--session-key", session_key, "--message", "CANARY_NOOP", "--json", "--timeout", "120"], env)
        if "CANARY_NOOP_OK" not in json.dumps(noop):
            raise RuntimeError("initial requester turn failed")
        main_tool = run_json([args.openclaw, "agent", "--session-key", session_key, "--message", "CANARY_MAIN_TOOL", "--json", "--timeout", "120"], env)
        if "CANARY_MAIN_TOOL_OK" not in json.dumps(main_tool):
            raise RuntimeError("unrelated headless main tool was intercepted")
        prepare = run_json([
            args.openclaw, "gateway", "call", "plugins.sessionAction", "--json", "--timeout", "5000",
            "--params", json.dumps({
                "pluginId": "workspace-lane-guard", "actionId": "prepareLane", "sessionKey": session_key,
                "payload": {"parentAgentId": "main", "targetAgentId": "target", "taskRoot": str(target_workspace)},
            }),
        ], env)
        lane = prepare["result"]["lane"] if "lane" in prepare.get("result", {}) else prepare["result"]["result"]["lane"]
        run_json([
            args.openclaw, "gateway", "call", "sessions.pluginPatch", "--json", "--timeout", "5000",
            "--params", json.dumps({
                "key": session_key, "pluginId": "workspace-lane-guard", "namespace": "lane", "value": lane,
            }),
        ], env)
        readiness_deadline = time.time() + 10
        while True:
            readiness_challenge = secrets.token_urlsafe(24)
            readiness = run_json([
                args.openclaw, "gateway", "call", "plugins.sessionAction", "--json", "--timeout", "5000",
                "--params", json.dumps({
                    "pluginId": "workspace-lane-guard", "actionId": "readiness", "sessionKey": session_key,
                    "payload": {"challenge": readiness_challenge},
                }),
            ], env)
            ready_text = json.dumps(readiness)
            if '"ready": true' in ready_text and readiness_challenge in ready_text:
                break
            if time.time() >= readiness_deadline:
                raise RuntimeError(f"readiness action not healthy: {ready_text[:1200]}")
            time.sleep(0.1)
        spawn = run_json([args.openclaw, "agent", "--session-key", session_key, "--message", "CANARY_SPAWN", "--json", "--timeout", "180"], env, 210)
        spawn_text = json.dumps(spawn)
        completion_observed = "CANARY_PARENT_RECEIVED" in spawn_text
        if not completion_observed:
            run_meta = spawn.get("result", {}).get("meta", {})
            if run_meta.get("yielded") is not True or run_meta.get("livenessState") != "paused":
                raise RuntimeError(f"native completion/yield result missing: {spawn_text[:1500]}")
        task_deadline = time.time() + 90
        tasks = None
        while True:
            tasks = run_json([args.openclaw, "tasks", "list", "--runtime", "subagent", "--json"], env)
            records = subagent_tasks(tasks)
            failed = [
                record for record in records
                if record.get("status") in ("failed", "timed_out", "cancelled", "lost")
            ]
            if failed:
                raise RuntimeError(f"native child did not succeed: {json.dumps(failed)[:1500]}")
            succeeded_and_delivered = any(
                record.get("status") == "succeeded" and record.get("deliveryStatus") == "delivered"
                for record in records
            )
            if succeeded_and_delivered and not completion_observed:
                history = run_json([
                    args.openclaw, "gateway", "call", "chat.history", "--json", "--timeout", "5000",
                    "--params", json.dumps({"sessionKey": session_key, "limit": 100}),
                ], env)
                completion_observed = "CANARY_PARENT_RECEIVED" in json.dumps(history)
            if succeeded_and_delivered and completion_observed:
                break
            if time.time() >= task_deadline:
                raise RuntimeError(
                    "native completion evidence missing after yield: "
                    f"tasks={json.dumps(records)[:1000]} completionObserved={completion_observed}"
                )
            time.sleep(0.5)
        context = run_json([args.openclaw, "agent", "--session-key", session_key, "--message", "CANARY_CONTEXT", "--json", "--timeout", "120"], env)
        context_text = json.dumps(context)
        if "sentinel=false execCalls=0" not in context_text:
            raise RuntimeError(f"requester context isolation failed: {context_text[:1500]}")
        tasks = run_json([args.openclaw, "tasks", "list", "--runtime", "subagent", "--json"], env)
        records = subagent_tasks(tasks)
        if not any(
            record.get("status") == "succeeded" and record.get("deliveryStatus") == "delivered"
            for record in records
        ):
            raise RuntimeError(f"durable succeeded and delivered native task missing: {json.dumps(records)[:1500]}")
        result = {
            "schemaVersion": 1,
            "status": "PASS",
            "openclawVersion": args.expected_version,
            "coldInspectDidNotImport": True,
            "readiness": True,
            "headlessMainTool": True,
            "nativeCompletionAndYield": True,
            "requesterContextIsolation": True,
            "durableNativeTask": True,
            "cleanup": "delete",
            "rootRetained": False,
        }
        print(json.dumps(result, sort_keys=True))
    finally:
        for process in (gateway, provider):
            if process and process.poll() is None:
                process.terminate()
        for process in (gateway, provider):
            if process:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        shutil.rmtree(root, ignore_errors=False)


if __name__ == "__main__":
    main()
