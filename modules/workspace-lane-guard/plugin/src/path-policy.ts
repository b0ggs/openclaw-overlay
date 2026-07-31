import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import type { TargetConfig } from "./lane-schema.ts";

export type CanonicalWorkspace = {
  root: string;
  components: string[];
  device: number | bigint;
  inode: number | bigint;
};

export function componentsFor(candidate: string): string[] {
  return path.resolve(candidate).split(path.sep).filter(Boolean);
}

export function relationship(
  a: string[],
  b: string[],
): "equal" | "ancestor" | "descendant" | "disjoint" {
  const common = Math.min(a.length, b.length);
  for (let i = 0; i < common; i += 1) if (a[i] !== b[i]) return "disjoint";
  if (a.length === b.length) return "equal";
  return a.length < b.length ? "ancestor" : "descendant";
}

export function isInside(candidate: string, root: string): boolean {
  const relative = path.relative(root, candidate);
  return (
    relative === "" ||
    (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative))
  );
}

function assertNoSymlinkChain(candidate: string, stopAt: string): void {
  let cursor = path.resolve(candidate);
  const stop = path.resolve(stopAt);
  while (isInside(cursor, stop)) {
    if (fs.existsSync(cursor)) {
      const stat = fs.lstatSync(cursor);
      if (stat.isSymbolicLink()) throw new Error("SYMLINK_ANCESTOR_REJECTED");
    }
    if (cursor === stop) return;
    const next = path.dirname(cursor);
    if (next === cursor) break;
    cursor = next;
  }
  throw new Error("PATH_OUTSIDE_WORKSPACE");
}

export function canonicalWorkspace(candidate: string): CanonicalWorkspace {
  if (!path.isAbsolute(candidate)) throw new Error("WORKSPACE_NOT_ABSOLUTE");
  const lexical = path.resolve(candidate);
  const real = fs.realpathSync(lexical);
  const stat = fs.statSync(real, { bigint: true });
  if (!stat.isDirectory()) throw new Error("WORKSPACE_NOT_DIRECTORY");
  assertNoSymlinkChain(lexical, real);
  return { root: real, components: componentsFor(real), device: stat.dev, inode: stat.ino };
}

export function validateTaskRoot(taskRoot: string, workspaceRoot: string): string {
  if (!path.isAbsolute(taskRoot)) throw new Error("TASK_ROOT_NOT_ABSOLUTE");
  const lexical = path.resolve(taskRoot);
  if (!isInside(lexical, workspaceRoot)) throw new Error("TASK_ROOT_ESCAPE");
  assertNoSymlinkChain(lexical, workspaceRoot);
  const existing = deepestExistingAncestor(lexical);
  const realExisting = fs.realpathSync(existing);
  if (!isInside(realExisting, workspaceRoot)) throw new Error("TASK_ROOT_SYMLINK_ESCAPE");
  for (const component of path.relative(existing, lexical).split(path.sep).filter(Boolean)) {
    if (component === "." || component === ".." || component.includes("\0"))
      throw new Error("TASK_ROOT_COMPONENT_INVALID");
  }
  return lexical;
}

export function deepestExistingAncestor(candidate: string): string {
  let cursor = path.resolve(candidate);
  while (!fs.existsSync(cursor)) {
    const next = path.dirname(cursor);
    if (next === cursor) throw new Error("NO_EXISTING_PATH_ANCESTOR");
    cursor = next;
  }
  return cursor;
}

export function validateConfiguredRoots(targets: TargetConfig[]): Map<string, CanonicalWorkspace> {
  const roots = new Map<string, CanonicalWorkspace>();
  for (const target of targets) roots.set(target.agentId, canonicalWorkspace(target.workspaceRoot));
  for (let i = 0; i < targets.length; i += 1) {
    for (let j = i + 1; j < targets.length; j += 1) {
      const left = targets[i];
      const right = targets[j];
      const rel = relationship(
        roots.get(left.agentId)!.components,
        roots.get(right.agentId)!.components,
      );
      if (rel === "ancestor" || rel === "descendant")
        throw new Error("CONFIGURED_WORKSPACE_ANCESTRY_REJECTED");
      if (rel === "equal") {
        if (
          !left.sameRootPair ||
          left.sameRootPair !== right.sameRootPair ||
          left.access === right.access
        ) {
          throw new Error("CONFIGURED_WORKSPACE_DUPLICATE_REJECTED");
        }
      }
    }
  }
  return roots;
}

export function rejectBroadWritableRoot(
  root: string,
  access: "ro" | "rw",
  liveHarness?: string,
): void {
  if (access !== "rw") return;
  const disallowed = new Set([path.parse(root).root, os.homedir(), "/root/projects"]);
  if (liveHarness) disallowed.add(path.resolve(liveHarness));
  if (disallowed.has(path.resolve(root))) throw new Error("BROAD_WRITABLE_ROOT_REJECTED");
}
