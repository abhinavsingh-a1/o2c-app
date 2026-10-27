import { describe, expect, it } from "vitest";

// DO NOT MERGE: gate evidence (ui). Fails on purpose.
describe("gate evidence", () => {
  it("fails on purpose", () => {
    expect(1 + 1).toBe(3);
  });
});
