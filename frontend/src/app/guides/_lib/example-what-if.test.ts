import { expect, it } from "vitest";
import { whatIfExample } from "./example-what-if";
it("F8 committed SPY AGG example metrics", () => {
 const example=whatIfExample();
 expect([example.start,example.end]).toEqual(["2021-10-11","2026-10-09"]);
 const none = [0.5406702459318233, 0.09040604137022434, 0.11762551532269735, -0.21027990838605726];
 [example.none.cumulative,example.none.annualized,example.none.volatility,example.none.drawdown].forEach((v,i)=>expect(v).toBeCloseTo(none[i],10));
 const quarterly = [0.48171507982939277, 0.0819198020273264, 0.11061979154119962, -0.2076992605751623];
 [example.quarterly.cumulative,example.quarterly.annualized,example.quarterly.volatility,example.quarterly.drawdown].forEach((v,i)=>expect(v).toBeCloseTo(quarterly[i],10));
 const monthly = [0.48005073929661024, 0.08167633895069693, 0.11047117951366575, -0.20717188012760335];
 [example.monthly.cumulative,example.monthly.annualized,example.monthly.volatility,example.monthly.drawdown].forEach((v,i)=>expect(v).toBeCloseTo(monthly[i],10));
 expect(example.monthlyResets).toBe(60);
 expect(example.quarterlyResets).toBe(20);
 expect(example.endSpyWeight).toBeCloseTo(0.7457744024164441,10);
 expect(example.endAggWeight).toBeCloseTo(0.25422559758355595,10);
});
