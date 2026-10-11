import { readFileSync } from "node:fs";
import ts from "typescript";
import { expect, it } from "vitest";

it("737 I3 client guide body locks raw closes to type imports", () => {
  const path = "src/app/guides/_components/guide-body.tsx";
  const source = ts.createSourceFile(path, readFileSync(path, "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const imports = source.statements.filter(ts.isImportDeclaration);
  for (const declaration of imports) {
    if (!ts.isStringLiteral(declaration.moduleSpecifier)) continue;
    if (!/(^|\/)_data\/spy-agg-closes(?:\.ts)?$/.test(declaration.moduleSpecifier.text)) continue;
    const clause = declaration.importClause;
    const bindings = clause?.namedBindings;
    const namedTypesOnly = bindings && ts.isNamedImports(bindings) && bindings.elements.length > 0
      && bindings.elements.every(element => element.isTypeOnly);
    expect(Boolean(clause?.isTypeOnly || (!clause?.name && namedTypesOnly)), declaration.moduleSpecifier.text).toBe(true);
  }
  const exampleImport = imports.find(declaration => {
    return ts.isStringLiteral(declaration.moduleSpecifier) && declaration.moduleSpecifier.text === "../_lib/example";
  });
  expect(exampleImport?.importClause?.isTypeOnly).toBe(true);
});
