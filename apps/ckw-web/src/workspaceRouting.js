export const workspaceRoutes = Object.freeze({
  leg: "#/leg",
  vzev: "#/vzev",
  billing: "#/billing"
});

export function workspaceFromHash(hash) {
  if (hash === workspaceRoutes.vzev) return "vzev";
  if (hash === workspaceRoutes.billing) return "billing";
  return "leg";
}

export function hasVzevSimulationInputs(files) {
  const flowKinds = new Set(files.map((file) => file.flow_kind));
  return flowKinds.has("consumption") && (flowKinds.has("pv_production") || flowKinds.has("pv_excess"));
}
