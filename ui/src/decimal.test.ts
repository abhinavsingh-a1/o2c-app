import { describe, expect, it } from "vitest";

import { isDecimal, lineTotal, normalize, sumMoney } from "./decimal";

describe("lineTotal", () => {
  it("rounds half up exactly like the agent", () => {
    // 170.5 x 2.15 = 366.575 -> 366.58 (floating point would give 366.57)
    expect(lineTotal("170.5", "2.15")).toBe("366.58");
  });

  it("handles whole numbers", () => {
    expect(lineTotal("13", "9.80")).toBe("127.40");
  });

  it("accepts a German decimal comma", () => {
    expect(lineTotal("10", "3,20")).toBe("32.00");
  });

  it("returns 0.00 for invalid input", () => {
    expect(lineTotal("abc", "3.20")).toBe("0.00");
    expect(lineTotal("", "3.20")).toBe("0.00");
  });
});

describe("sumMoney", () => {
  it("adds without floating-point drift", () => {
    expect(sumMoney(["0.10", "0.20"])).toBe("0.30");
    expect(sumMoney(["127.40", "738.00", "223.50", "972.00"])).toBe("2060.90");
  });
});

describe("isDecimal and normalize", () => {
  it("recognises valid numbers", () => {
    expect(isDecimal("3.20")).toBe(true);
    expect(isDecimal("3,20")).toBe(true);
    expect(isDecimal("-1")).toBe(false);
    expect(isDecimal("1e3")).toBe(false);
  });

  it("normalizes comma and spaces", () => {
    expect(normalize(" 3,20 ")).toBe("3.20");
  });
});
