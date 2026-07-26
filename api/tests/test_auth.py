"""S0 gate: authenticated localhost — mutations need the per-launch token."""

from conftest import TEST_TOKEN, auth_headers


def test_tokenless_post_401(client):
    response = client.post("/workspaces", json={"id": "w1"})
    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"
    assert client.get("/workspaces").json() == {"workspaces": []}  # nothing was created


def test_wrong_token_401(client):
    response = client.post(
        "/workspaces", json={"id": "w1"}, headers={"Authorization": "Bearer wrong-token"}
    )
    assert response.status_code == 401


def test_foreign_origin_401_even_with_valid_token(client):
    response = client.post(
        "/workspaces",
        json={"id": "w1"},
        headers={**auth_headers(), "Origin": "https://evil.example"},
    )
    assert response.status_code == 401


def test_webapp_origin_with_token_allowed(client):
    response = client.post(
        "/workspaces",
        json={"id": "w-ui"},
        headers={**auth_headers(), "Origin": "http://localhost:3000"},
    )
    assert response.status_code == 201


def test_valid_token_creates_workspace(client):
    response = client.post("/workspaces", json={"id": "w1"}, headers=auth_headers())
    assert response.status_code == 201
    assert client.get("/workspaces").json() == {"workspaces": ["w1"]}
    # duplicate -> 409
    assert (
        client.post("/workspaces", json={"id": "w1"}, headers=auth_headers()).status_code
        == 409
    )


def test_reads_are_free(client):
    assert client.get("/health").status_code == 200
    assert client.get("/workspaces").status_code == 200


def test_all_mutating_verbs_gated_before_routing(client):
    """Middleware runs before routing: even unknown paths 401 on mutating verbs,
    so a future route cannot ship unguarded by accident."""
    for method in ("post", "put", "delete", "patch"):
        response = getattr(client, method)("/any/future/route")
        assert response.status_code == 401, method
