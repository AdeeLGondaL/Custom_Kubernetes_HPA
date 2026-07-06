import io
import os
import time
import torch
from fastapi import FastAPI, File, UploadFile
from prometheus_client import Counter, Gauge, Histogram, make_asgi_app
from PIL import Image

torch.set_num_threads(1)
torch.set_num_interop_threads(1)

app = FastAPI()
app.mount("/metrics", make_asgi_app())

requests_total       = Counter("requests_total", "Total inference requests")
requests_in_progress = Gauge("requests_in_progress", "Requests currently being processed")
inference_duration   = Histogram(
    "inference_duration_seconds", "Time to run one inference",
    buckets=[0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0],
)

model     = None
labels    = None
transform = None
WARMUP_RUNS = int(os.environ.get("WARMUP_RUNS", "3"))


def _load_model():
    from torchvision.models import resnet18, ResNet18_Weights
    from torchvision import transforms as T
    weights = ResNet18_Weights.IMAGENET1K_V1
    m = resnet18(weights=weights)
    m.eval()
    t = T.Compose([
        T.Resize(256), T.CenterCrop(224), T.ToTensor(),
        T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])
    return m, weights.meta["categories"], t


def _warmup_model(m, runs: int = WARMUP_RUNS):
    if runs <= 0:
        return
    dummy = torch.zeros(1, 3, 224, 224)
    with torch.inference_mode():
        for _ in range(runs):
            m(dummy)


@app.on_event("startup")
async def startup():
    global model, labels, transform
    model, labels, transform = _load_model()
    _warmup_model(model)


@app.post("/infer")
async def infer(file: UploadFile = File(...)):
    requests_total.inc()
    with requests_in_progress.track_inprogress():
        t_start     = time.perf_counter()
        image_bytes = await file.read()
        image       = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        tensor      = transform(image).unsqueeze(0)
        with torch.inference_mode():
            output     = model(tensor)
            class_idx  = int(output.argmax(dim=1))
            confidence = float(torch.softmax(output, dim=1)[0, class_idx])
        inference_duration.observe(time.perf_counter() - t_start)
    return {"class": labels[class_idx], "confidence": round(confidence, 4)}


@app.get("/health")
async def health():
    return {"status": "ok"}
