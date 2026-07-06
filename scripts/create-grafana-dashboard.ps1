param(
    [string]$GrafanaUrl = "http://localhost:3000",
    [string]$Username = "admin",
    [string]$Password = "admin",
    [string]$DatasourceName = "Prometheus",
    [string]$DashboardTitle = "Custom Kubernetes HPA Experiment"
)

$ErrorActionPreference = "Stop"

$pair = "$Username`:$Password"
$encodedCredentials = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($pair))
$headers = @{
    Authorization = "Basic $encodedCredentials"
    "Content-Type" = "application/json"
}

Write-Host "Finding Prometheus datasource..." -ForegroundColor Cyan
try {
    $datasource = Invoke-RestMethod `
        -Uri "$GrafanaUrl/api/datasources/name/$DatasourceName" `
        -Headers $headers `
        -Method Get
} catch {
    $allDatasources = Invoke-RestMethod `
        -Uri "$GrafanaUrl/api/datasources" `
        -Headers $headers `
        -Method Get

    $datasource = $allDatasources | Where-Object { $_.type -eq "prometheus" } | Select-Object -First 1
    if (-not $datasource) {
        throw "No Prometheus datasource found in Grafana."
    }
}

$script:DatasourceUid = $datasource.uid
Write-Host "Using datasource '$($datasource.name)' (UID: $DatasourceUid)" -ForegroundColor Green

function New-PromTarget {
    param(
        [string]$RefId,
        [string]$Expr,
        [string]$Legend
    )
    return @{
        refId       = $RefId
        expr        = $Expr
        legendFormat = $Legend
        datasource  = @{ type = "prometheus"; uid = $script:DatasourceUid }
    }
}

# ── Panel builders ────────────────────────────────────────────────────────────

function New-LatencyPanel {
    param([int]$Id, [int]$X, [int]$Y, [int]$W = 24, [int]$H = 9)
    return @{
        id   = $Id
        type = "timeseries"
        title = "p99 Server-Side Inference Latency  [REPORT METRIC 1]"
        datasource = @{ type = "prometheus"; uid = $script:DatasourceUid }
        targets = @(
            (New-PromTarget `
                -RefId "A" `
                -Expr "histogram_quantile(0.99, sum(rate(dispatcher_request_duration_seconds_bucket[1m])) by (le))" `
                -Legend "p99 end-to-end latency (incl. queue wait)")
            (New-PromTarget `
                -RefId "B" `
                -Expr "vector(0.5)" `
                -Legend "SLO target (0.5 s)")
        )
        fieldConfig = @{
            defaults = @{
                unit     = "s"
                decimals = 3
                min      = 0
                color    = @{ mode = "palette-classic" }
                custom   = @{
                    drawStyle      = "line"
                    lineWidth      = 2
                    fillOpacity    = 8
                    showPoints     = "never"
                    spanNulls      = $true
                    thresholdsStyle = @{ mode = "line+area" }
                }
                thresholds = @{
                    mode  = "absolute"
                    steps = @(
                        @{ color = "green"; value = $null }
                        @{ color = "red";   value = 0.5 }
                    )
                }
            }
            overrides = @(
                @{
                    matcher    = @{ id = "byName"; options = "SLO target (0.5 s)" }
                    properties = @(
                        @{ id = "color"; value = @{ fixedColor = "red"; mode = "fixed" } }
                        @{ id = "custom.lineWidth"; value = 2 }
                        @{ id = "custom.lineStyle"; value = @{ fill = "dash"; dash = @(8, 4) } }
                        @{ id = "custom.fillOpacity"; value = 0 }
                    )
                }
            )
        }
        options = @{
            legend  = @{ displayMode = "list"; placement = "bottom"; showLegend = $true }
            tooltip = @{ mode = "multi"; sort = "none" }
        }
        gridPos = @{ h = $H; w = $W; x = $X; y = $Y }
    }
}

function New-CpuCoresPanel {
    param([int]$Id, [int]$X, [int]$Y, [int]$W = 24, [int]$H = 9)
    return @{
        id   = $Id
        type = "timeseries"
        title = "CPU Cores Allocated (= Replica Count)  [REPORT METRIC 2]"
        datasource = @{ type = "prometheus"; uid = $script:DatasourceUid }
        targets = @(
            (New-PromTarget `
                -RefId "A" `
                -Expr 'max(kube_deployment_status_replicas_available{deployment="ml-replica"})' `
                -Legend "available replicas (= CPU cores)")
            (New-PromTarget `
                -RefId "B" `
                -Expr 'max(kube_deployment_spec_replicas{deployment="ml-replica"})' `
                -Legend "requested replicas")
        )
        fieldConfig = @{
            defaults = @{
                unit     = "short"
                decimals = 0
                min      = 0
                color    = @{ mode = "palette-classic" }
                custom   = @{
                    drawStyle   = "line"
                    lineWidth   = 2
                    fillOpacity = 10
                    showPoints  = "never"
                    spanNulls   = $true
                    stacking    = @{ mode = "none" }
                }
            }
            overrides = @()
        }
        options = @{
            legend  = @{ displayMode = "list"; placement = "bottom"; showLegend = $true }
            tooltip = @{ mode = "multi"; sort = "none" }
        }
        gridPos = @{ h = $H; w = $W; x = $X; y = $Y }
    }
}

function New-SimplePanel {
    param(
        [int]$Id,
        [string]$Title,
        [array]$Targets,
        [int]$X, [int]$Y,
        [int]$W = 12, [int]$H = 7,
        [string]$Unit = "short",
        [int]$Decimals = 2
    )
    return @{
        id   = $Id
        type = "timeseries"
        title = $Title
        datasource = @{ type = "prometheus"; uid = $script:DatasourceUid }
        targets = $Targets
        fieldConfig = @{
            defaults = @{
                unit     = $Unit
                decimals = $Decimals
                min      = 0
                custom   = @{
                    drawStyle   = "line"
                    lineWidth   = 2
                    fillOpacity = 8
                    showPoints  = "never"
                    spanNulls   = $true
                }
            }
            overrides = @()
        }
        options = @{
            legend  = @{ displayMode = "list"; placement = "bottom"; showLegend = $true }
            tooltip = @{ mode = "multi"; sort = "none" }
        }
        gridPos = @{ h = $H; w = $W; x = $X; y = $Y }
    }
}

# ── Dashboard ─────────────────────────────────────────────────────────────────

$dashboard = @{
    uid           = "custom-k8s-hpa-experiment"
    title         = $DashboardTitle
    tags          = @("kubernetes", "hpa", "experiment")
    timezone      = "browser"
    schemaVersion = 39
    version       = 0
    refresh       = "10s"
    time          = @{ from = "now-20m"; to = "now" }
    annotations   = @{
        list = @(
            @{
                builtIn    = 1
                datasource = @{ type = "grafana"; uid = "-- Grafana --" }
                enable     = $true
                hide       = $true
                iconColor  = "rgba(0, 211, 255, 1)"
                name       = "Annotations & Alerts"
                type       = "dashboard"
            }
        )
    }
    panels = @(
        # ── Row label ─────────────────────────────────────────────────────
        @{
            id      = 10
            type    = "text"
            title   = ""
            options = @{
                mode    = "markdown"
                content = "## Report Metrics -- export these panels via Inspect > Data > Download CSV after each experiment"
            }
            gridPos = @{ h = 2; w = 24; x = 0; y = 0 }
        }

        # ── REPORT METRIC 1: p99 latency (full width) ─────────────────────
        (New-LatencyPanel -Id 1 -X 0 -Y 2 -W 24 -H 10)

        # ── REPORT METRIC 2: CPU cores (full width) ───────────────────────
        (New-CpuCoresPanel -Id 2 -X 0 -Y 12 -W 24 -H 10)

        # ── Row label ─────────────────────────────────────────────────────
        @{
            id      = 11
            type    = "text"
            title   = ""
            options = @{
                mode    = "markdown"
                content = "## Context Panels -- for understanding autoscaler behaviour"
            }
            gridPos = @{ h = 2; w = 24; x = 0; y = 22 }
        }

        # ── Context: request rate ─────────────────────────────────────────
        (New-SimplePanel `
            -Id 3 -Title "Arrival Rate (λ)" `
            -Targets @(
                (New-PromTarget -RefId "A" `
                    -Expr "rate(dispatcher_requests_total[30s])" `
                    -Legend "req/s received")
            ) `
            -X 0 -Y 24 -W 12 -H 7 -Unit "reqps")

        # ── Context: queue depth ──────────────────────────────────────────
        (New-SimplePanel `
            -Id 4 -Title "Dispatcher Queue Depth" `
            -Targets @(
                (New-PromTarget -RefId "A" `
                    -Expr "dispatcher_queue_depth" `
                    -Legend "queued requests")
            ) `
            -X 12 -Y 24 -W 12 -H 7 -Unit "short" -Decimals 0)

        # ── Context: dropped requests ─────────────────────────────────────
        (New-SimplePanel `
            -Id 5 -Title "Dropped Requests (503s)" `
            -Targets @(
                (New-PromTarget -RefId "A" `
                    -Expr "rate(dispatcher_dropped_requests_total[30s])" `
                    -Legend "dropped/sec")
            ) `
            -X 0 -Y 31 -W 12 -H 7 -Unit "reqps")

        # ── Context: mean service time ────────────────────────────────────
        (New-SimplePanel `
            -Id 6 -Title "Mean Inference Time (W)" `
            -Targets @(
                (New-PromTarget -RefId "A" `
                    -Expr "rate(inference_duration_seconds_sum[1m]) / rate(inference_duration_seconds_count[1m])" `
                    -Legend "mean service time")
            ) `
            -X 12 -Y 31 -W 12 -H 7 -Unit "s" -Decimals 3)
    )
}

$payload = @{
    dashboard = $dashboard
    folderId  = 0
    overwrite = $true
    message   = "Update: report-focused layout with export instructions"
} | ConvertTo-Json -Depth 100

Write-Host "Creating/updating dashboard..." -ForegroundColor Cyan
$result = Invoke-RestMethod `
    -Uri "$GrafanaUrl/api/dashboards/db" `
    -Headers $headers `
    -Method Post `
    -Body $payload

Write-Host "Dashboard ready: $GrafanaUrl$($result.url)" -ForegroundColor Green
Write-Host ""
Write-Host "HOW TO EXPORT DATA AFTER EACH EXPERIMENT:" -ForegroundColor Yellow
Write-Host "  1. Open the dashboard in Grafana"
Write-Host "  2. Click the three-dot menu on 'p99 Latency' panel → Inspect → Data → Download CSV"
Write-Host "  3. Repeat for 'CPU Cores' panel"
Write-Host "  4. Rename files: custom-hpa_latency.csv, hpa70_latency.csv, hpa90_latency.csv"
Write-Host ""
Write-Host "OR run: python scripts/export_metrics.py --name custom-hpa" -ForegroundColor Cyan
