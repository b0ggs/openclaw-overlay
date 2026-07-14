import fs from "node:fs";
import path from "node:path";

const DISPATCHABLE_STATES = new Set(["Todo", "Rework"]);
const REQUESTED_ID_PATTERN = /^[A-Za-z0-9._:-]+$/;

function expandWorkspace(value) {
  if (value === "~") {
    return process.env.HOME ?? value;
  }
  if (value.startsWith("~/")) {
    return path.join(process.env.HOME ?? "~", value.slice(2));
  }
  return value;
}

function normalizePath(filePath) {
  return filePath.split(path.sep).join("/");
}

function rel(filePath, root) {
  const relative = path.relative(root, filePath);
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) {
    return normalizePath(filePath);
  }
  return normalizePath(relative);
}

function issuePath(workspace, issueId) {
  return path.join(workspace, "state", "issues", `${issueId}.json`);
}

function safeArray(value) {
  return Array.isArray(value) ? value.filter((item) => typeof item === "string") : [];
}

function readJson(filePath, workspace, readFiles) {
  const entry = {
    path: rel(filePath, workspace),
    present: fs.existsSync(filePath),
    readable: false,
  };
  readFiles.push(entry);
  if (!entry.present) {
    return { ok: false, missing: true, path: entry.path };
  }

  try {
    const payload = JSON.parse(fs.readFileSync(filePath, "utf8"));
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      return { ok: false, invalid: true, path: entry.path, error: "top-level JSON is not an object" };
    }
    entry.readable = true;
    return { ok: true, payload, path: entry.path };
  } catch (error) {
    return { ok: false, invalid: true, path: entry.path, error: error.message };
  }
}

function baseDecision({
  result,
  reason,
  workspace,
  issueId,
  mode,
  readFiles,
  authoritativeListSource = null,
  authorizedIssueIds = [],
  authorizedEpicIds = [],
  details = {},
}) {
  return {
    result,
    reason,
    issueId,
    mode,
    workspace,
    authoritativeListSource,
    authorizedIssueIds,
    authorizedEpicIds,
    authorityFilesRead: readFiles.map((file) => file.path),
    authorityFileDetails: readFiles,
    sideEffectFree: true,
    humanAuthorizationRequired: result === "BLOCK",
    details,
  };
}

function block(params) {
  return baseDecision({ ...params, result: "BLOCK" });
}

function allow(params) {
  return baseDecision({
    ...params,
    result: "ALLOW",
    reason: "allowed",
    details: {
      ...params.details,
      dispatchableState: params.details?.dispatchableState ?? true,
    },
  });
}

function summarizeIssue(issue) {
  if (!issue || typeof issue !== "object") {
    return null;
  }
  return {
    id: issue.id,
    kind: issue.kind,
    state: issue.state,
    parent: issue.parent,
    project: issue.project,
    workerMode: issue.workerMode,
    executionPattern: issue.executionPattern,
    dispatchPolicy: issue.status?.dispatchPolicy,
  };
}

function resolveWorkspace(workspaceArg) {
  if (!workspaceArg) {
    throw new Error("--workspace is required");
  }
  return fs.realpathSync(path.resolve(expandWorkspace(workspaceArg)));
}

function activeFrozenAuthority(orchestrator, frozen) {
  const orchestratorFrozenAt = orchestrator.authorizationFrozenAt ?? null;
  const frozenFrozenAt = frozen?.authorizationFrozenAt ?? null;
  const active = Boolean(orchestratorFrozenAt || frozenFrozenAt);
  if (!active) {
    return { active: false };
  }

  if (orchestratorFrozenAt && frozenFrozenAt && orchestratorFrozenAt !== frozenFrozenAt) {
    return {
      active: true,
      contradiction: "authorizationFrozenAt differs between orchestrator and frozen authorization snapshot",
    };
  }

  const source = frozenFrozenAt ? "state/frozen-authorization.json" : "state/orchestrator.json";
  const approvedIssueIds = safeArray(frozenFrozenAt ? frozen.approvedIssueIds : orchestrator.approvedIssueIds);
  const approvedEpicIds = safeArray(frozenFrozenAt ? frozen.approvedEpicIds : orchestrator.approvedEpicIds);
  const authorizationFrozenAt = frozenFrozenAt ?? orchestratorFrozenAt;

  if (
    orchestratorFrozenAt &&
    frozenFrozenAt &&
    JSON.stringify(safeArray(orchestrator.approvedIssueIds).sort()) !== JSON.stringify([...approvedIssueIds].sort())
  ) {
    return {
      active: true,
      contradiction: "approvedIssueIds differs between orchestrator and frozen authorization snapshot",
    };
  }

  if (
    orchestratorFrozenAt &&
    frozenFrozenAt &&
    JSON.stringify(safeArray(orchestrator.approvedEpicIds).sort()) !== JSON.stringify([...approvedEpicIds].sort())
  ) {
    return {
      active: true,
      contradiction: "approvedEpicIds differs between orchestrator and frozen authorization snapshot",
    };
  }

  return {
    active: true,
    source,
    authorizationFrozenAt,
    approvedIssueIds,
    approvedEpicIds,
  };
}

function evaluateDispatchability(issue, issueId, workspace, mode, readFiles, context) {
  if (issue.id !== issueId) {
    return block({
      ...context,
      workspace,
      issueId,
      mode,
      readFiles,
      reason: "invalid_state",
      details: {
        ...context.details,
        issue: summarizeIssue(issue),
        error: "issue record id does not match requested issue id",
      },
    });
  }

  if (issue.kind === "epic" || issue.kind === "root" || Array.isArray(issue.children)) {
    return block({
      ...context,
      workspace,
      issueId,
      mode,
      readFiles,
      reason: "wrong_kind",
      details: {
        ...context.details,
        issue: summarizeIssue(issue),
        requiredKind: "dispatchable child issue",
      },
    });
  }

  if (mode === "dispatch" && !DISPATCHABLE_STATES.has(issue.state)) {
    return block({
      ...context,
      workspace,
      issueId,
      mode,
      readFiles,
      reason: "not_dispatchable",
      details: {
        ...context.details,
        issue: summarizeIssue(issue),
        dispatchableStates: [...DISPATCHABLE_STATES].sort(),
      },
    });
  }

  return null;
}

function readIssue(workspace, issueId, readFiles) {
  const issueResult = readJson(issuePath(workspace, issueId), workspace, readFiles);
  if (!issueResult.ok) {
    return issueResult.missing
      ? { ok: false, reason: "missing_issue", detail: `missing ${issueResult.path}` }
      : { ok: false, reason: "invalid_state", detail: `failed to read ${issueResult.path}: ${issueResult.error}` };
  }
  return { ok: true, issue: issueResult.payload };
}

function readEpic(workspace, epicId, readFiles) {
  const epicResult = readJson(issuePath(workspace, epicId), workspace, readFiles);
  if (!epicResult.ok) {
    return epicResult.missing
      ? { ok: false, reason: "invalid_state", detail: `missing authorized epic record ${epicResult.path}` }
      : { ok: false, reason: "invalid_state", detail: `failed to read ${epicResult.path}: ${epicResult.error}` };
  }
  if (epicResult.payload.kind !== "epic" || !Array.isArray(epicResult.payload.children)) {
    return { ok: false, reason: "invalid_state", detail: "authorized epic record is not an epic with a children list" };
  }
  return { ok: true, epic: epicResult.payload };
}

function checkFrozenWindow({ workspace, issueId, mode, issue, readFiles, frozenAuthority }) {
  const authorizedIssueIds = frozenAuthority.approvedIssueIds;
  const authorizedEpicIds = frozenAuthority.approvedEpicIds;
  const context = {
    authoritativeListSource: "approvedIssueIds",
    authorizedIssueIds,
    authorizedEpicIds,
    details: {
      authorizationFrozenAt: frozenAuthority.authorizationFrozenAt,
      authorizationSourceFile: frozenAuthority.source,
      issue: summarizeIssue(issue),
    },
  };

  if (!authorizedIssueIds.length || !authorizedEpicIds.length) {
    return block({
      ...context,
      workspace,
      issueId,
      mode,
      readFiles,
      reason: "context_recovery_blocked",
      details: {
        ...context.details,
        error: "frozen authorization has no approved issue or epic list",
      },
    });
  }

  const wrongKindBlock = evaluateDispatchability(issue, issueId, workspace, mode, readFiles, {
    ...context,
    details: { ...context.details, dispatchabilityPhase: "kind-check" },
  });
  if (wrongKindBlock?.reason === "wrong_kind") {
    return wrongKindBlock;
  }

  if (!authorizedIssueIds.includes(issueId)) {
    return block({
      ...context,
      workspace,
      issueId,
      mode,
      readFiles,
      reason: "not_in_authorized_list",
      details: {
        ...context.details,
        error: "requested issue is absent from approvedIssueIds",
      },
    });
  }

  if (!issue.parent || !authorizedEpicIds.includes(issue.parent)) {
    return block({
      ...context,
      workspace,
      issueId,
      mode,
      readFiles,
      reason: "not_in_authorized_list",
      details: {
        ...context.details,
        error: "issue parent is absent from approvedEpicIds",
      },
    });
  }

  const epicResult = readEpic(workspace, issue.parent, readFiles);
  if (!epicResult.ok) {
    return block({
      ...context,
      workspace,
      issueId,
      mode,
      readFiles,
      reason: epicResult.reason,
      details: {
        ...context.details,
        error: epicResult.detail,
      },
    });
  }

  if (!safeArray(epicResult.epic.children).includes(issueId)) {
    return block({
      ...context,
      workspace,
      issueId,
      mode,
      readFiles,
      reason: "not_in_authorized_list",
      details: {
        ...context.details,
        epic: summarizeIssue(epicResult.epic),
        error: "requested issue is absent from authorized epic children",
      },
    });
  }

  const dispatchabilityBlock = evaluateDispatchability(issue, issueId, workspace, mode, readFiles, {
    ...context,
    details: {
      ...context.details,
      epic: summarizeIssue(epicResult.epic),
    },
  });
  if (dispatchabilityBlock) {
    return dispatchabilityBlock;
  }

  return allow({
    ...context,
    workspace,
    issueId,
    mode,
    readFiles,
    details: {
      ...context.details,
      epic: summarizeIssue(epicResult.epic),
    },
  });
}

function checkAuthorizedEpic({ workspace, issueId, mode, issue, orchestrator, readFiles }) {
  const authorizedEpic = typeof orchestrator.authorizedEpic === "string" ? orchestrator.authorizedEpic : null;
  const context = {
    authoritativeListSource: "authorizedEpic.children",
    authorizedIssueIds: [],
    authorizedEpicIds: authorizedEpic ? [authorizedEpic] : [],
    details: {
      authorizedEpic,
      issue: summarizeIssue(issue),
    },
  };

  if (!authorizedEpic) {
    return block({
      ...context,
      workspace,
      issueId,
      mode,
      readFiles,
      reason: "no_active_authorization",
      details: {
        ...context.details,
        error: "no frozen window and no authorizedEpic in orchestrator state",
      },
    });
  }

  const wrongKindBlock = evaluateDispatchability(issue, issueId, workspace, mode, readFiles, {
    ...context,
    details: { ...context.details, dispatchabilityPhase: "kind-check" },
  });
  if (wrongKindBlock?.reason === "wrong_kind") {
    return wrongKindBlock;
  }

  const epicResult = readEpic(workspace, authorizedEpic, readFiles);
  if (!epicResult.ok) {
    return block({
      ...context,
      workspace,
      issueId,
      mode,
      readFiles,
      reason: epicResult.reason,
      details: {
        ...context.details,
        error: epicResult.detail,
      },
    });
  }

  const children = safeArray(epicResult.epic.children);
  context.authorizedIssueIds = children;
  context.details.epic = summarizeIssue(epicResult.epic);

  if (!children.length) {
    return block({
      ...context,
      workspace,
      issueId,
      mode,
      readFiles,
      reason: "list_exhausted",
      details: {
        ...context.details,
        error: "authorized epic has no durable children",
      },
    });
  }

  if (!children.includes(issueId)) {
    return block({
      ...context,
      workspace,
      issueId,
      mode,
      readFiles,
      reason: "not_in_authorized_list",
      details: {
        ...context.details,
        error: "requested issue is absent from authorizedEpic children",
      },
    });
  }

  if (issue.parent !== authorizedEpic) {
    return block({
      ...context,
      workspace,
      issueId,
      mode,
      readFiles,
      reason: "not_in_authorized_list",
      details: {
        ...context.details,
        error: "issue parent does not match authorizedEpic",
      },
    });
  }

  const dispatchabilityBlock = evaluateDispatchability(issue, issueId, workspace, mode, readFiles, context);
  if (dispatchabilityBlock) {
    return dispatchabilityBlock;
  }

  return allow({
    ...context,
    workspace,
    issueId,
    mode,
    readFiles,
  });
}

export function checkListBoundExecution({ workspace: workspaceArg, issueId, mode = "dispatch" }) {
  const readFiles = [];
  const workspace = resolveWorkspace(workspaceArg);

  if (!issueId || !REQUESTED_ID_PATTERN.test(issueId)) {
    return block({
      workspace,
      issueId: issueId ?? null,
      mode,
      readFiles,
      reason: "invalid_issue_id",
      details: {
        error: "requested issue id is missing or malformed",
      },
    });
  }

  const orchestratorResult = readJson(path.join(workspace, "state", "orchestrator.json"), workspace, readFiles);
  if (!orchestratorResult.ok) {
    return block({
      workspace,
      issueId,
      mode,
      readFiles,
      reason: "invalid_state",
      details: {
        error: orchestratorResult.missing
          ? `missing ${orchestratorResult.path}`
          : `failed to read ${orchestratorResult.path}: ${orchestratorResult.error}`,
      },
    });
  }

  const frozenPath = path.join(workspace, "state", "frozen-authorization.json");
  const frozenResult = fs.existsSync(frozenPath) ? readJson(frozenPath, workspace, readFiles) : null;
  if (frozenResult && !frozenResult.ok) {
    return block({
      workspace,
      issueId,
      mode,
      readFiles,
      reason: "invalid_state",
      details: {
        error: `failed to read ${frozenResult.path}: ${frozenResult.error}`,
      },
    });
  }

  const issueResult = readIssue(workspace, issueId, readFiles);
  if (!issueResult.ok) {
    return block({
      workspace,
      issueId,
      mode,
      readFiles,
      reason: issueResult.reason,
      details: {
        error: issueResult.detail,
      },
    });
  }

  const frozenAuthority = activeFrozenAuthority(orchestratorResult.payload, frozenResult?.payload ?? null);
  if (frozenAuthority.active) {
    if (frozenAuthority.contradiction) {
      return block({
        workspace,
        issueId,
        mode,
        readFiles,
        reason: "invalid_state",
        details: {
          error: frozenAuthority.contradiction,
          issue: summarizeIssue(issueResult.issue),
        },
      });
    }
    return checkFrozenWindow({
      workspace,
      issueId,
      mode,
      issue: issueResult.issue,
      readFiles,
      frozenAuthority,
    });
  }

  return checkAuthorizedEpic({
    workspace,
    issueId,
    mode,
    issue: issueResult.issue,
    orchestrator: orchestratorResult.payload,
    readFiles,
  });
}
