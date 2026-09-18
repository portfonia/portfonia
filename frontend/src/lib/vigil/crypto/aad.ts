// #450 Design section 5's "Browser v1" contract: "AAD UTF8 compact JSON arrays ['vigil-file',1,vault_id,
// object_id] and ['vigil-inner',1,vault_id,object_id]". JSON.stringify on
// an array with no `space` argument is already compact (no inserted
// whitespace), matching the backend's `json.dumps(..., separators=(",",
// ":"))` convention used elsewhere in this codebase.

const encoder = new TextEncoder();

function buildAad(purpose: "vigil-file" | "vigil-inner", vaultId: string, objectId: string): Uint8Array {
  return encoder.encode(JSON.stringify([purpose, 1, vaultId, objectId]));
}

export function buildFileAad(vaultId: string, objectId: string): Uint8Array {
  return buildAad("vigil-file", vaultId, objectId);
}

export function buildInnerAad(vaultId: string, objectId: string): Uint8Array {
  return buildAad("vigil-inner", vaultId, objectId);
}
