import { createLucideIcon, type LucideIcon } from "lucide-react";

export const JadeDisc: LucideIcon = createLucideIcon("JadeDisc", [
  ["circle", { cx: "12", cy: "12", r: "10", key: "outer" }],
  ["circle", { cx: "12", cy: "12", r: "5", key: "inner" }],
]);
