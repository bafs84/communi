export type Workspace = "leg" | "vzev" | "billing";

export const workspaceRoutes: Readonly<Record<Workspace, string>>;

export function workspaceFromHash(hash: string): Workspace;

export function hasVzevSimulationInputs(files: Array<{ flow_kind: string }>): boolean;
