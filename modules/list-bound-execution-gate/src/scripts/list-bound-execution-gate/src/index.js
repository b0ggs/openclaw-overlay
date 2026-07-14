import { runCli } from "./cli.js";

function registerCli(program) {
  const overlay = program
    .command("overlay-v2")
    .description("Overlay V2 module commands");

  const gate = overlay
    .command("list-bound-execution-gate")
    .description("List-bound execution gate commands");

  gate
    .command("check")
    .description("Return ALLOW or BLOCK for a requested issue against authoritative JSON state")
    .requiredOption("--workspace <path>", "OpenClaw workspace root containing state/orchestrator.json and state/issues/*.json")
    .requiredOption("--issue-id <id>", "Requested durable issue id")
    .option("--mode <mode>", "Decision mode", "dispatch")
    .action((options) => {
      process.exitCode = runCli([
        "check",
        "--workspace",
        options.workspace,
        "--issue-id",
        options.issueId,
        "--mode",
        options.mode,
      ]);
    });
}

export default {
  id: "overlay-v2-list-bound-execution-gate",
  name: "Overlay V2 List-Bound Execution Gate",
  description: "Native OpenClaw CLI command for side-effect-free list-bound execution checks.",
  configSchema: {
    type: "object",
    additionalProperties: false,
    properties: {},
  },
  register(api) {
    api.registerCli(({ program }) => registerCli(program), {
      descriptors: [
        {
          name: "overlay-v2",
          description: "Overlay V2 module commands",
          hasSubcommands: true,
        },
      ],
    });
  },
};
