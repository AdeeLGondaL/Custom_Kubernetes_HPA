import io
import pytest
import torch
from PIL import Image
from fastapi.testclient import TestClient


def _make_jpeg() -> bytes:
    img = Image.new("RGB", (300, 300), color=(128, 64, 32))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture()
def client(monkeypatch):
    import ml_replica.app as m
    fake_out = torch.zeros(1, 1000)
    fake_out[0, 42] = 10.0
    from torchvision import transforms as T
    real_transform = T.Compose([
        T.Resize(256), T.CenterCrop(224), T.ToTensor(),
        T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])
    monkeypatch.setattr(m, "_load_model",
                        lambda: (lambda x: fake_out,
                                 [f"class_{i}" for i in range(1000)],
                                 real_transform))
    from ml_replica.app import app
    with TestClient(app) as c:
        yield c


def test_infer_returns_class_and_confidence(client):
    resp = client.post("/infer", files={"file": ("t.jpg", _make_jpeg(), "image/jpeg")})
    assert resp.status_code == 200
    body = resp.json()
    assert "class" in body
    assert "confidence" in body
    assert body["class"] == "class_42"
    assert 0.0 < body["confidence"] <= 1.0


def test_health_returns_ok(client):
    assert client.get("/health").status_code == 200


def test_metrics_endpoint_exposed(client):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert b"requests_total" in resp.content
    assert b"inference_duration_seconds" in resp.content
