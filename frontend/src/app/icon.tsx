import { ImageResponse } from "next/og";

import { JADE_BI_DATA_URI } from "@/components/icons/jade-bi";

export const size = { width: 32, height: 32 };
export const contentType = "image/png";

export default function Icon() {
  return new ImageResponse(
    (
      <div style={{ display: "flex", width: "100%", height: "100%" }}>
        {/* ImageResponse renders a nested SVG image directly. */}
        {/* eslint-disable-next-line jsx-a11y/alt-text */}
        <img src={JADE_BI_DATA_URI} width={32} height={32} />
      </div>
    ),
    size,
  );
}
