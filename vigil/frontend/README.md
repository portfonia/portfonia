# Vigil frontend

Next.js management shell (P1.3). Package manager is `bun` exclusively.

Confirm and retrieve routes are not in this checkpoint (#461). Session
gating lives on the management shell, not the root layout, so those public
pages can load without a Vigil host session.
