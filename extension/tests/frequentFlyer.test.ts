import { describe, expect, it } from "vitest";
import { frequentFlyer } from "../src/shared";

const t1 = { id: "PT1", names: [{ title: "MR", firstName: "AHMED", lastName: "ABOUHARB" }] };
const t2 = { id: "PT2", names: [{ title: "MRS", firstName: "SARA", lastName: "ABOUHARB" }] };

describe("frequentFlyer", () => {
  it("finds booking-level cards by travelerId(s)", () => {
    const order = { data: { travelers: [t1, t2], frequentFlyerCards: [
      { companyCode: "SV", cardNumber: "123456789", travelerIds: ["PT1"], tierLevelName: "Silver" },
      { companyCode: "SV", cardNumber: "999", travelerId: "PT2" },
    ] } };
    expect(frequentFlyer(order, t1)).toEqual(["ALFURSAN SV 123456789 (Silver)"]);
    expect(frequentFlyer(order, t2)).toEqual(["ALFURSAN SV 999"]);
  });
  it("finds cards stored on the traveler", () => {
    const t = { ...t1, frequentFlyerCards: [{ companyCode: "EK", cardNumber: "EK555" }] };
    expect(frequentFlyer({ data: { travelers: [t] } }, t)).toEqual(["EK EK555"]);
  });
  it("returns nothing when there is no card", () => {
    expect(frequentFlyer({ data: { travelers: [t1] } }, t1)).toEqual([]);
  });
});
