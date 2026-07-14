#!/usr/bin/env node
import { checkListBoundExecution } from "./list-bound-execution-gate.js";

function usage() {
  return `Usage: openclaw overlay-v2 list-bound-execution-gate check --workspace <path> --issue-id <id> [--mode dispatch]

Runs a side-effect-free list-bound execution decision against authoritative JSON state.`;
}

function parseArgs(argv) {
  const args = [...argv];
  const command = args.shift();
  const options = { command, mode: "dispatch" };

  while (args.length) {
    const arg = args.shift();
    if (arg === "--workspace") {
      options.workspace = args.shift();
    } else if (arg === "--issue-id") {
      options.issueId = args.shift();
    } else if (arg === "--mode") {
      options.mode = args.shift();
    } else if (arg === "-h" || arg === "--help") {
      options.help = true;
    } else {
      throw new Error(`unknown argument: ${arg}`);
    }
  }

  return options;
}

export function runCli(argv = process.argv.slice(2), io = { stdout: process.stdout, stderr: process.stderr }) {
  let options;
  try {
    options = parseArgs(argv);
  } catch (error) {
    io.stderr.write(`${error.message}\n${usage()}\n`);
    return 64;
  }

  if (options.help) {
    io.stdout.write(`${usage()}\n`);
    return 0;
  }

  if (options.command !== "check") {
    io.stderr.write(`${usage()}\n`);
    return 64;
  }

  if (!options.workspace || !options.issueId) {
    io.stderr.write(`--workspace and --issue-id are required\n${usage()}\n`);
    return 64;
  }

  try {
    const decision = checkListBoundExecution({
      workspace: options.workspace,
      issueId: options.issueId,
      mode: options.mode,
    });
    io.stdout.write(`${JSON.stringify(decision, null, 2)}\n`);
    return decision.result === "ALLOW" ? 0 : 3;
  } catch (error) {
    io.stderr.write(`${error.message}\n`);
    return 2;
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  process.exitCode = runCli();
}
