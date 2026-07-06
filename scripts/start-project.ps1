param(
    [switch]$SkipMinikubeStart,
    [switch]$SkipPrometheusForward,
    [int]$GrafanaPort = 3000,
    [int]$PrometheusPort = 9090
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

function Require-Command {
    param([string]$Name)

    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found on PATH."
    }
}

function Test-PortInUse {
    param([int]$Port)

    $connection = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
    return $null -ne $connection
}

function Wait-PodsReady {
    param(
        [string]$Name,
        [string]$Selector,
        [int]$TimeoutSeconds = 180
    )

    Write-Host "Waiting for $Name pods to be ready..." -ForegroundColor Cyan
    kubectl wait `
        --for=condition=Ready `
        "pod" `
        -l $Selector `
        --timeout="${TimeoutSeconds}s" | Out-Host
}

function Start-PortForward {
    param(
        [string]$Name,
        [string]$Service,
        [int]$LocalPort,
        [int]$RemotePort,
        [string]$Url
    )

    if (Test-PortInUse $LocalPort) {
        Write-Warning "$Name port $LocalPort is already in use. Leaving it alone."
        return
    }

    $command = @"
Write-Host '$Name port-forward running at $Url' -ForegroundColor Green
Write-Host 'Keep this window open. Press Ctrl+C to stop this port-forward.' -ForegroundColor Yellow
while (`$true) {
    kubectl port-forward svc/$Service ${LocalPort}:${RemotePort}
    `$exitCode = `$LASTEXITCODE
    Write-Warning ('$Name port-forward stopped (exit code {0}). Retrying in 3 seconds. Press Ctrl+C to stop.' -f `$exitCode)
    Start-Sleep -Seconds 3
}
"@

    Start-Process powershell.exe -ArgumentList @(
        "-NoExit",
        "-ExecutionPolicy", "Bypass",
        "-Command", $command
    )
}

Require-Command minikube
Require-Command kubectl

if (-not $SkipMinikubeStart) {
    Write-Host "Starting Minikube..." -ForegroundColor Cyan
    minikube start
}

Write-Host "Updating Minikube context..." -ForegroundColor Cyan
minikube update-context | Out-Host

Write-Host "Checking cluster context..." -ForegroundColor Cyan
kubectl cluster-info | Out-Host

Write-Host "Current pod status:" -ForegroundColor Cyan
kubectl get pods

Wait-PodsReady `
    -Name "Grafana" `
    -Selector "app.kubernetes.io/instance=prometheus,app.kubernetes.io/name=grafana"

Start-PortForward `
    -Name "Grafana" `
    -Service "prometheus-grafana" `
    -LocalPort $GrafanaPort `
    -RemotePort 80 `
    -Url "http://localhost:$GrafanaPort"

if (-not $SkipPrometheusForward) {
    Wait-PodsReady `
        -Name "Prometheus" `
        -Selector "prometheus=prometheus-kube-prometheus-prometheus"

    Start-PortForward `
        -Name "Prometheus" `
        -Service "prometheus-kube-prometheus-prometheus" `
        -LocalPort $PrometheusPort `
        -RemotePort 9090 `
        -Url "http://localhost:$PrometheusPort"
}

$DispatcherUrl = (minikube service dispatcher-service --url).Trim()
$env:DISPATCHER_URL = "$DispatcherUrl/infer"

Write-Host ""
Write-Host "Project startup complete." -ForegroundColor Green
Write-Host "Dispatcher: $DispatcherUrl"
Write-Host "Infer URL:  $env:DISPATCHER_URL"
Write-Host "Grafana:    http://localhost:$GrafanaPort"
if (-not $SkipPrometheusForward) {
    Write-Host "Prometheus: http://localhost:$PrometheusPort"
}
Write-Host ""
Write-Host "Smoke test:"
Write-Host "curl.exe -X POST `$env:DISPATCHER_URL -F `"file=@load_tester/test_image.jpg`""
