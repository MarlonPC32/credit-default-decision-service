use std::{collections::HashMap, env, error::Error, sync::Arc, time::Duration};

use axum::{
    Json, Router,
    body::Bytes,
    extract::{DefaultBodyLimit, State},
    http::{HeaderMap, StatusCode, header},
    middleware,
    response::{IntoResponse, Response},
    routing::{get, post},
};
use reqwest::{Client, RequestBuilder, Url};
use tokio::net::TcpListener;

mod telemetry;
use telemetry::Telemetry;

const MAX_PREDICT_BODY_BYTES: usize = 64 * 1024;

#[derive(Clone)]
struct Upstream {
    client: Client,
    health_url: Url,
    predict_url: Url,
    telemetry: Arc<Telemetry>,
}

impl Upstream {
    fn new(base_url: &str) -> Result<Self, Box<dyn Error>> {
        let mut base_url = Url::parse(base_url)?;
        if !matches!(base_url.scheme(), "http" | "https")
            || base_url.host_str().is_none()
            || !base_url.username().is_empty()
            || base_url.password().is_some()
            || base_url.query().is_some()
            || base_url.fragment().is_some()
        {
            return Err("FASTAPI_BASE_URL must be an HTTP(S) base URL without credentials, a query, or a fragment".into());
        }
        base_url
            .path_segments_mut()
            .map_err(|_| "FASTAPI_BASE_URL does not support path segments")?
            .pop_if_empty()
            .push("");

        let client = Client::builder()
            .timeout(Duration::from_secs(5))
            .redirect(reqwest::redirect::Policy::none())
            // A prediction may have written an audit row before its response fails.
            .retry(reqwest::retry::never())
            .no_proxy()
            .build()?;

        Ok(Self {
            client,
            health_url: base_url.join("health")?,
            predict_url: base_url.join("predict")?,
            telemetry: Arc::new(Telemetry::new()?),
        })
    }
}

fn gateway(upstream: Upstream) -> Router {
    let telemetry = Arc::clone(&upstream.telemetry);
    Router::new()
        .route("/health", get(health))
        .route("/metrics", get(metrics))
        .route(
            "/predict",
            post(predict).layer(DefaultBodyLimit::max(MAX_PREDICT_BODY_BYTES)),
        )
        .layer(middleware::from_fn_with_state(
            telemetry,
            telemetry::record_http,
        ))
        .with_state(upstream)
}

async fn metrics(State(upstream): State<Upstream>) -> Response {
    upstream.telemetry.metrics()
}

#[derive(Clone, Copy)]
enum Operation {
    Health,
    Prediction,
}

impl Operation {
    fn route(self) -> &'static str {
        match self {
            Self::Health => "/health",
            Self::Prediction => "/predict",
        }
    }

    fn description(self) -> &'static str {
        match self {
            Self::Health => "health check",
            Self::Prediction => "prediction",
        }
    }
}

async fn health(State(upstream): State<Upstream>) -> Response {
    relay(
        upstream.client.get(upstream.health_url),
        &upstream.telemetry,
        Operation::Health,
    )
    .await
}

async fn predict(State(upstream): State<Upstream>, headers: HeaderMap, body: Bytes) -> Response {
    let mut request = upstream.client.post(upstream.predict_url).body(body);
    if let Some(content_type) = headers.get(header::CONTENT_TYPE) {
        request = request.header(header::CONTENT_TYPE, content_type);
    }
    relay(request, &upstream.telemetry, Operation::Prediction).await
}

async fn relay(request: RequestBuilder, telemetry: &Telemetry, operation: Operation) -> Response {
    let response = match request.send().await {
        Ok(response)
            if response.status() == StatusCode::OK
                || (matches!(operation, Operation::Prediction)
                    && matches!(
                        response.status(),
                        StatusCode::BAD_REQUEST | StatusCode::UNPROCESSABLE_ENTITY
                    )) =>
        {
            response
        }
        Ok(_) => {
            telemetry.upstream_failure(operation.route(), "unexpected_status");
            return gateway_error(StatusCode::BAD_GATEWAY, operation.description(), "failed");
        }
        Err(error) => return upstream_error(error, telemetry, operation),
    };

    let status = response.status();
    let content_type = response.headers().get(header::CONTENT_TYPE).cloned();
    let body = match response.bytes().await {
        Ok(body) => body,
        Err(error) => return upstream_error(error, telemetry, operation),
    };

    let mut response = (status, body).into_response();
    if let Some(content_type) = content_type {
        response
            .headers_mut()
            .insert(header::CONTENT_TYPE, content_type);
    }
    response
}

fn upstream_error(error: reqwest::Error, telemetry: &Telemetry, operation: Operation) -> Response {
    let reason = if error.is_timeout() {
        "timeout"
    } else if error.is_connect() {
        "connect"
    } else {
        "transport"
    };
    telemetry.upstream_failure(operation.route(), reason);
    if error.is_timeout() && !error.is_connect() {
        return gateway_error(
            StatusCode::GATEWAY_TIMEOUT,
            operation.description(),
            "timed out",
        );
    }
    gateway_error(StatusCode::BAD_GATEWAY, operation.description(), "failed")
}

fn gateway_error(status: StatusCode, operation: &str, reason: &str) -> Response {
    (
        status,
        Json(HashMap::from([(
            "error",
            format!("FastAPI {operation} {reason}"),
        )])),
    )
        .into_response()
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    if env::args().nth(1).as_deref() == Some("--healthcheck") {
        let upstream = Upstream::new("http://127.0.0.1:8080")?;
        let response = upstream.client.get(upstream.health_url).send().await?;
        return if response.status() == StatusCode::OK {
            Ok(())
        } else {
            Err(format!("Gateway health check returned {}", response.status()).into())
        };
    }

    tracing_subscriber::fmt()
        .json()
        .with_max_level(tracing::Level::INFO)
        .init();
    let base_url =
        env::var("FASTAPI_BASE_URL").map_err(|_| "Set FASTAPI_BASE_URL to the FastAPI base URL")?;
    let app = gateway(Upstream::new(&base_url)?);
    let bind_address = env::var("GATEWAY_BIND_ADDR").unwrap_or_else(|_| "127.0.0.1:8080".into());
    let listener = TcpListener::bind(bind_address).await?;
    tracing::info!(event = "listening", service = "gateway", address = %listener.local_addr()?);
    axum::serve(listener, app).await?;
    Ok(())
}

#[cfg(test)]
mod tests;
