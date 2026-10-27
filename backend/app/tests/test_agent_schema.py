"""Public Agent schema contracts for issue #755."""

from collections.abc import Iterator
from typing import cast
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.agent_audit import audit_agent_request
from app.main import _api_doc_urls, app
from app.routers import agent
from app.tests.conftest import TEST_USER_ID, seed_user
from app.tests.test_agent_api import bearer, create

URL = "/agent/openapi.json"


def refs(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "$ref":
                assert isinstance(child, str)
                yield child
            else:
                yield from refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from refs(child)


def resolve(document: object, ref: str) -> object:
    value = document
    assert ref.startswith("#/components/schemas/")
    for part in ref[2:].split("/"):
        value = cast(dict[str, object], value)[part.replace("~1", "/").replace("~0", "~")]
    return value


def test_schema_operation_inventory_security_and_references(app_client: TestClient) -> None:
    response = app_client.get(URL)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    schema = response.json()
    operations = {
        (method.upper(), path) for path, methods in schema["paths"].items() for method in methods
    }
    inventory = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/agent/v1/")
        for method in route.methods
    }
    assert operations == inventory
    assert len(operations) == 19
    assert schema["servers"] == [{"url": "https://api.portfonia.com"}]
    assert schema["components"]["securitySchemes"] == {
        "bearerAuth": {"type": "http", "scheme": "bearer"}
    }
    for methods in schema["paths"].values():
        for operation in methods.values():
            assert operation["security"] == [{"bearerAuth": []}]
    reachable = set()
    pending = list(refs(schema["paths"]))
    while pending:
        ref = pending.pop()
        target = resolve(schema, ref)
        if ref not in reachable:
            reachable.add(ref)
            pending.extend(refs(target))
    assert set(schema["components"]["schemas"]) == {
        ref.removeprefix("#/components/schemas/")
        for ref in reachable
        if ref.startswith("#/components/schemas/")
    }


def test_schema_documents_existing_holdings_rules(app_client: TestClient) -> None:
    response = app_client.get(URL)
    assert response.status_code == 200
    schema = response.json()
    paths = schema["paths"]
    post = paths["/agent/v1/holdings"]["post"]
    put = paths["/agent/v1/holdings"]["put"]
    assert "Idempotency-Key" in str(post)
    assert "Idempotency-Key" not in str(put)
    body = put["requestBody"]["content"]["application/json"]["schema"]
    assert body["properties"]["holdings"]["minItems"] == 1
    assert "canonical" in body["properties"]["version"]["description"]
    assert "completeness" in body["properties"]["delete_ids"]["description"]
    assert "decimal string" in str(schema)
    assert "last" in paths["/agent/v1/holdings/{holding_id}"]["delete"]["description"]
    for methods in paths.values():
        for operation in methods.values():
            assert "Retry-After" in operation["description"]
            assert "token" in operation["description"]
            assert "subscription" in operation["description"]


def test_production_exposes_only_agent_schema() -> None:
    from app.routers import agent_schema

    production = FastAPI(**_api_doc_urls("production"))
    production.include_router(agent.router, prefix="/agent/v1")
    production.include_router(agent_schema.router)
    production.openapi = agent_schema.with_body_components(production.openapi)  # type: ignore[method-assign]
    production.middleware("http")(audit_agent_request)
    with TestClient(production) as client:
        for url in ("/openapi.json", "/docs", "/redoc"):
            assert client.get(url).status_code == 404
        assert client.get(URL).status_code == 200


def test_schema_read_has_no_data_access_side_effects(
    app_client: TestClient, db_session: Session
) -> None:
    seed_user(db_session, TEST_USER_ID)
    token = create(app_client)
    before = db_session.execute(text("SELECT last_used_at FROM api_tokens")).all()
    audit_before = db_session.execute(text("SELECT count(*) FROM api_audit_log")).scalar_one()
    with (
        patch("app.tasks.notification_tasks.send_api_access_notice_task.delay") as access,
        patch(
            "app.tasks.notification_tasks.send_api_modification_notice_task.delay"
        ) as modification,
        patch("app.routers.agent.enforce_agent_limits") as limits,
    ):
        assert app_client.get(URL).status_code == 200
        assert app_client.get(URL, headers=bearer(token)).status_code == 200
        access.assert_not_called()
        modification.assert_not_called()
        limits.assert_not_called()
    assert db_session.execute(text("SELECT last_used_at FROM api_tokens")).all() == before
    assert (
        db_session.execute(text("SELECT count(*) FROM api_audit_log")).scalar_one() == audit_before
    )


def test_schema_documents_existing_holdings_success_responses(app_client: TestClient) -> None:
    document = app_client.get(URL).json()
    expected = [
        ("/agent/v1/holdings", "post", "201", {"version", "holding"}),
        ("/agent/v1/holdings", "get", "200", {"version", "holdings", "metadata"}),
        ("/agent/v1/holdings", "put", "200", {"version", "holdings", "metadata"}),
        ("/agent/v1/holdings/{holding_id}", "put", "200", {"version", "holding"}),
        ("/agent/v1/holdings/{holding_id}", "patch", "200", {"version", "holding"}),
        ("/agent/v1/holdings/{holding_id}", "delete", "200", {"version", "deleted_id"}),
    ]
    for path, method, status, fields in expected:
        responses = document["paths"][path][method]["responses"]
        assert {code for code in responses if code.startswith("2")} == {status}
        body = responses[status]["content"]["application/json"]["schema"]
        model = resolve(document, body["$ref"])
        assert isinstance(model, dict)
        assert set(model["properties"]) == fields
        assert set(model["required"]) == fields
    validation = document["paths"]["/agent/v1/holdings/validate"]["post"]["responses"]
    assert {code for code in validation if code.startswith("2")} == {"200"}
    body = validation["200"]["content"]["application/json"]["schema"]
    variants = [resolve(document, entry["$ref"]) for entry in body["oneOf"]]
    assert [
        set(cast(dict[str, object], cast(dict[str, object], variant)["properties"]))
        for variant in variants
    ] == [{"valid", "version", "normalized"}, {"valid", "version", "errors"}]
