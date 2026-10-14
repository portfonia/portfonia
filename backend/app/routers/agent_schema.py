"""Public description of the implemented Agent data API."""

from collections.abc import Callable, Iterator
from copy import deepcopy
from typing import cast

from fastapi import APIRouter, Request

router = APIRouter()

OPERATIONS = {
    "/agent/v1/reports": {"get"},
    "/agent/v1/snapshots": {"get"},
    "/agent/v1/intel": {"get"},
    "/agent/v1/holdings": {"get", "put", "post"},
    "/agent/v1/holdings/{holding_id}": {"put", "patch", "delete"},
    "/agent/v1/holdings/validate": {"post"},
}


def with_body_components(
    generate: Callable[[], dict[str, object]],
) -> Callable[[], dict[str, object]]:
    """Register definitions from manually documented bodies in the app schema."""

    def openapi() -> dict[str, object]:
        document = generate()
        paths = cast(dict[str, dict[str, dict[str, object]]], document["paths"])
        components = cast(dict[str, dict[str, object]], document["components"])["schemas"]
        for path, methods in OPERATIONS.items():
            for method in methods:
                body = paths[path][method].get("requestBody")
                if not isinstance(body, dict):
                    continue
                schema = body["content"]["application/json"]["schema"]
                for name, definition in schema.pop("$defs", {}).items():
                    components.setdefault(name, definition)
        return document

    return openapi


def _refs(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "$ref" and isinstance(child, str):
                yield child
            else:
                yield from _refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _refs(child)


@router.get("/agent/openapi.json", include_in_schema=False)
def agent_schema(request: Request) -> dict[str, object]:
    source = request.app.openapi()
    paths = {
        path: {method: deepcopy(source["paths"][path][method]) for method in methods}
        for path, methods in OPERATIONS.items()
    }
    for operations in paths.values():
        for operation in operations.values():
            operation["security"] = [{"bearerAuth": []}]
    schemas: dict[str, object] = {}
    pending = list(_refs(paths))
    while pending:
        ref = pending.pop()
        if not ref.startswith("#/components/schemas/"):
            continue
        name = ref.removeprefix("#/components/schemas/")
        if name not in schemas:
            schema = deepcopy(source["components"]["schemas"][name])
            schemas[name] = schema
            pending.extend(_refs(schema))
    return {
        "openapi": source["openapi"],
        "info": cast(dict[str, object], deepcopy(source["info"])),
        "servers": [{"url": "https://api.portfonia.com"}],
        "paths": paths,
        "components": {
            "schemas": schemas,
            "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}},
        },
    }
