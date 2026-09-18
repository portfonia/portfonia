// Vigil browser crypto (issue #455, Vigil R0 P2.2) never degrades: an
// unsupported browser, a WASM/memory failure, or a validation failure all
// surface as one of these and stop the pipeline. No caller may catch one
// of these and fall back to a weaker KDF or drop the password requirement
// (#450 Design section 5's "Browser v1" contract).

export class VigilCryptoInputError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "VigilCryptoInputError";
  }
}

export class VigilCryptoUnavailableError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options);
    this.name = "VigilCryptoUnavailableError";
  }
}

export class VigilCryptoIntegrityError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options);
    this.name = "VigilCryptoIntegrityError";
  }
}
