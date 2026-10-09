// A single thick circle forms a solid jade ring from radius 5 to radius 10.
import { createLucideIcon, type LucideIcon } from "lucide-react";

export const JadeDisc: LucideIcon = createLucideIcon("JadeDisc", [
  ["circle", { cx: "12", cy: "12", r: "7.5", "stroke-width": "5", key: "disc" }],
]);
