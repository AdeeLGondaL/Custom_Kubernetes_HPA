import pytest
from autoscaler.autoscaler import compute_desired_replicas, query_prometheus, compute_mean_service_time
from autoscaler import autoscaler as autoscaler_module


# --- compute_desired_replicas ---

def test_little_law_basic():
    # λ=20 req/s, W=0.2s → load=4.0 → desired=ceil(4.0/0.7)=6
    assert compute_desired_replicas(20.0, 0.2, target_utilization=0.7) == 6


def test_zero_arrival_rate_returns_min():
    assert compute_desired_replicas(0.0, 0.2, target_utilization=0.7) == 1


def test_zero_service_time_returns_min():
    assert compute_desired_replicas(20.0, 0.0, target_utilization=0.7) == 1


def test_clamps_to_min_replicas():
    assert compute_desired_replicas(0.1, 0.01, target_utilization=0.7, min_replicas=2) == 2


def test_clamps_to_max_replicas():
    assert compute_desired_replicas(1000.0, 10.0, target_utilization=0.7, max_replicas=10) == 10


def test_custom_target_utilization():
    # λ=10, W=0.5 → load=5 → at 50% util: ceil(5/0.5)=10
    assert compute_desired_replicas(10.0, 0.5, target_utilization=0.5) == 10


# --- query_prometheus ---

def test_query_prometheus_extracts_value(requests_mock):
    requests_mock.get(
        "http://fake:9090/api/v1/query",
        json={"data": {"result": [{"value": ["1718000000", "12.5"]}]}},
    )
    assert query_prometheus("http://fake:9090", "some_metric") == pytest.approx(12.5)


def test_query_prometheus_returns_zero_for_empty_result(requests_mock):
    requests_mock.get(
        "http://fake:9090/api/v1/query",
        json={"data": {"result": []}},
    )
    assert query_prometheus("http://fake:9090", "some_metric") == 0.0


def test_query_prometheus_raises_on_http_error(requests_mock):
    requests_mock.get("http://fake:9090/api/v1/query", status_code=500)
    with pytest.raises(Exception):
        query_prometheus("http://fake:9090", "some_metric")


# --- compute_mean_service_time ---

def test_mean_service_time(monkeypatch):
    responses = iter([10.0, 50.0])  # sum=10, count=50 → mean=0.2
    monkeypatch.setattr(autoscaler_module, "query_prometheus", lambda url, q: next(responses))
    assert compute_mean_service_time("http://fake:9090") == pytest.approx(0.2)


def test_mean_service_time_zero_count(monkeypatch):
    monkeypatch.setattr(autoscaler_module, "query_prometheus", lambda url, q: 0.0)
    assert compute_mean_service_time("http://fake:9090") == 0.0
