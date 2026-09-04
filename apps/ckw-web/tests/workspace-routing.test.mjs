import assert from "node:assert/strict";
import test from "node:test";

import { hasVzevSimulationInputs, workspaceFromHash, workspaceRoutes } from "../src/workspaceRouting.js";

test("each product workspace has a stable direct hash route", () => {
  assert.equal(workspaceFromHash(workspaceRoutes.leg), "leg");
  assert.equal(workspaceFromHash(workspaceRoutes.vzev), "vzev");
  assert.equal(workspaceFromHash(workspaceRoutes.billing), "billing");
});

test("empty and legacy hashes open the LEG workspace safely", () => {
  assert.equal(workspaceFromHash(""), "leg");
  assert.equal(workspaceFromHash("#/planning"), "leg");
  assert.equal(workspaceFromHash("#/unknown"), "leg");
});

test("vZEV simulation starts only when classic consumption and solar data are available", () => {
  assert.equal(hasVzevSimulationInputs([]), false);
  assert.equal(hasVzevSimulationInputs([{ flow_kind: "zev_aggregate" }]), false);
  assert.equal(hasVzevSimulationInputs([{ flow_kind: "consumption" }]), false);
  assert.equal(
    hasVzevSimulationInputs([{ flow_kind: "consumption" }, { flow_kind: "pv_production" }]),
    true
  );
  assert.equal(
    hasVzevSimulationInputs([{ flow_kind: "consumption" }, { flow_kind: "pv_excess" }]),
    true
  );
});
