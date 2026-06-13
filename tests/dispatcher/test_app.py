import asyncio
import io
import pytest
from PIL import Image
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock, patch


def _make_jpeg() -> bytes:
    img = Image.new("RGB", (10, 10), color=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("DISABLE_K8S_WATCH", "1")
    from dispatcher.app import app
    with TestClient(app) as c:
        yield c


# --- pod event logic ---

async def test_added_ready_pod_joins_free_pool():
    import dispatcher.app as m
    m._init_state()
    await m.on_pod_event("ADDED", "10.0.0.1", True)
    assert "10.0.0.1" in m.valid_ips
    ip = await asyncio.wait_for(m.free_queue.get(), timeout=1.0)
    assert ip == "10.0.0.1"


async def test_deleted_pod_leaves_valid_set():
    import dispatcher.app as m
    m._init_state()
    await m.on_pod_event("ADDED", "10.0.0.2", True)
    await m.on_pod_event("DELETED", "10.0.0.2", False)
    assert "10.0.0.2" not in m.valid_ips


async def test_stale_ip_skipped_in_get_free_replica():
    import dispatcher.app as m
    m._init_state()
    # Put a stale IP in the queue (simulates pod deleted while queued)
    await m.free_queue.put("10.0.0.9")
    # Don't add to valid_ips — it's stale
    # Add a valid one
    await m.on_pod_event("ADDED", "10.0.0.3", True)
    ip = await asyncio.wait_for(m._get_free_replica(), timeout=1.0)
    assert ip == "10.0.0.3"  # stale IP was skipped


# --- backpressure ---

def test_503_when_pending_count_at_max(client, monkeypatch):
    import dispatcher.app as m
    monkeypatch.setattr(m, "pending_count", m.MAX_QUEUE_DEPTH)
    resp = client.post("/infer", files={"file": ("t.jpg", _make_jpeg(), "image/jpeg")})
    assert resp.status_code == 503


# --- routing ---

async def test_infer_routes_to_free_replica_and_returns_response(monkeypatch):
    monkeypatch.setenv("DISABLE_K8S_WATCH", "1")
    import dispatcher.app as m
    m._init_state()
    m.valid_ips.add("10.0.0.5")
    await m.free_queue.put("10.0.0.5")

    fake_resp = MagicMock()
    fake_resp.content = b'{"class":"tabby","confidence":0.95}'
    fake_resp.status_code = 200

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=fake_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("dispatcher.app.httpx.AsyncClient", return_value=mock_client):
        from dispatcher.app import app
        with TestClient(app) as c:
            resp = c.post("/infer", files={"file": ("t.jpg", _make_jpeg(), "image/jpeg")})

    assert resp.status_code == 200
    # Replica returned to free pool after request
    assert "10.0.0.5" in m.valid_ips
    assert not m.free_queue.empty()
