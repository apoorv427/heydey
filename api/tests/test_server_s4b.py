"""S4b: /today, /brief/run (simulate-overnight), /approvals/decide over HTTP."""

import pytest

from heydey import approvals, morning_brief, vector_store, workspaces

from conftest import auth_headers

DIM = 384


@pytest.fixture()
def today_ws(heydey_home, client, monkeypatch):
    workspaces.create_workspace("todayws")
    conn = workspaces.connect("todayws")
    vector_store.upsert_points(conn, [
        ("p1", [1.0] + [0.0] * (DIM - 1), {
            "text": "LOCKS: done=proven GATE pending",
            "source_path": "/x/_System/docs/LOCKS.md", "chunk_index": 0,
            "mtime_iso": "2099-01-01T00:00:00+00:00",
        }),
    ])
    approval_id = approvals.seed_demo_sku_approval(conn)
    conn.commit()
    conn.close()
    monkeypatch.setattr(morning_brief, "notify_macos", lambda *a: True)
    yield "todayws", approval_id


def test_brief_run_requires_token(client, today_ws):
    assert client.post("/brief/run", json={"workspace": today_ws[0]}).status_code == 401


def test_simulate_overnight_then_today(client, today_ws):
    ws_id, approval_id = today_ws
    run = client.post("/brief/run", json={"workspace": ws_id}, headers=auth_headers())
    assert run.status_code == 200
    assert len(run.json()["items"]) >= 5

    today = client.get(f"/today?workspace={ws_id}")
    assert today.status_code == 200
    body = today.json()
    assert body["brief"]["id"] == run.json()["id"]
    assert [a["id"] for a in body["approvals"]] == [approval_id]
    assert body["sentinel"]["summary"].endswith("green")


def test_decide_over_http_executes_prepared_action(client, today_ws):
    ws_id, approval_id = today_ws
    response = client.post(
        "/approvals/decide",
        json={"id": approval_id, "decision": "approved", "workspace": ws_id},
        headers=auth_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["deep_link"].startswith("https://admin.shopify.com/store/demo-store/products/")
    assert body["note"] == "no write was fired — prepared action only"

    again = client.post(
        "/approvals/decide",
        json={"id": approval_id, "decision": "approved", "workspace": ws_id},
        headers=auth_headers(),
    )
    assert again.status_code == 422  # single-shot enforced over HTTP too
