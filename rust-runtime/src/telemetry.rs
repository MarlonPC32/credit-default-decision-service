use std::{sync::Arc, time::Instant};

use axum::{
    extract::{Request, State},
    http::{Method, StatusCode, header},
    middleware::Next,
    response::{IntoResponse, Response},
};
use prometheus::{
    Encoder, HistogramOpts, HistogramVec, IntCounterVec, Opts, Registry, TextEncoder,
};

pub struct Telemetry {
    registry: Registry,
    requests: IntCounterVec,
    duration: HistogramVec,
    failures: IntCounterVec,
}

impl Telemetry {
    pub fn new() -> Result<Self, prometheus::Error> {
        let registry = Registry::new();
        let requests = IntCounterVec::new(
            Opts::new("credit_http_requests_total", "Completed HTTP requests."),
            &["method", "route", "status"],
        )?;
        let duration = HistogramVec::new(
            HistogramOpts::new(
                "credit_http_request_duration_seconds",
                "HTTP request duration.",
            ),
            &["method", "route"],
        )?;
        let failures = IntCounterVec::new(
            Opts::new(
                "credit_gateway_upstream_failures_total",
                "Failed calls to FastAPI.",
            ),
            &["route", "reason"],
        )?;
        registry.register(Box::new(requests.clone()))?;
        registry.register(Box::new(duration.clone()))?;
        registry.register(Box::new(failures.clone()))?;
        Ok(Self {
            registry,
            requests,
            duration,
            failures,
        })
    }

    pub fn upstream_failure(&self, route: &str, reason: &str) {
        self.failures.with_label_values(&[route, reason]).inc();
        tracing::warn!(
            event = "upstream_failure",
            service = "gateway",
            route,
            reason
        );
    }

    pub fn metrics(&self) -> Response {
        let encoder = TextEncoder::new();
        let mut body = Vec::new();
        if encoder.encode(&self.registry.gather(), &mut body).is_err() {
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        }
        ([(header::CONTENT_TYPE, encoder.format_type())], body).into_response()
    }
}

pub async fn record_http(
    State(telemetry): State<Arc<Telemetry>>,
    request: Request,
    next: Next,
) -> Response {
    let route = match request.uri().path() {
        "/metrics" => return next.run(request).await,
        "/health" => "/health",
        "/predict" => "/predict",
        _ => "other",
    };
    let method = match *request.method() {
        Method::GET => "GET",
        Method::HEAD => "HEAD",
        Method::POST => "POST",
        _ => "OTHER",
    };
    let started = Instant::now();
    let response = next.run(request).await;
    let status = response.status().as_u16();
    let duration_seconds = started.elapsed().as_secs_f64();
    telemetry
        .requests
        .with_label_values(&[method, route, &status.to_string()])
        .inc();
    telemetry
        .duration
        .with_label_values(&[method, route])
        .observe(duration_seconds);

    if status >= 500 {
        tracing::warn!(
            event = "http_request",
            service = "gateway",
            method,
            route,
            status,
            duration_seconds
        );
    } else if route != "/health" || status >= 400 {
        tracing::info!(
            event = "http_request",
            service = "gateway",
            method,
            route,
            status,
            duration_seconds
        );
    }
    response
}
