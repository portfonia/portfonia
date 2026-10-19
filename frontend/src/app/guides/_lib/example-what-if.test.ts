import { expect, it } from "vitest";
import { whatIfExample } from "./example-what-if";
it("F8 committed SPY AGG example metrics", () => {
 const example=whatIfExample();
 expect([example.start,example.end]).toEqual(["2021-10-11","2026-10-09"]);
 const none = [0.5406702459318233, 0.09040604137022434, 0.11762551532269735, -0.21027990838605726];
 [example.none.cumulative,example.none.annualized,example.none.volatility,example.none.drawdown].forEach((v,i)=>expect(v).toBeCloseTo(none[i],10));
 const quarterly = [0.48171507982939277, 0.0819198020273264, 0.11061979154119962, -0.2076992605751623];
 [example.quarterly.cumulative,example.quarterly.annualized,example.quarterly.volatility,example.quarterly.drawdown].forEach((v,i)=>expect(v).toBeCloseTo(quarterly[i],10));
});
