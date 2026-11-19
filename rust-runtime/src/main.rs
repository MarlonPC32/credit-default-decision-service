use std::{collections::HashMap, env, error::Error, time::Duration};

use axum::{
    Json, Router,
    body::Bytes,
    extract::{DefaultBodyLimit, State},
    http::{HeaderMap, StatusCode, header},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use reqwest::{Client, RequestBuilder, Url};
use tokio::net::TcpListener;

const MAX_PREDICT_BODY_BYTES: usize = 64 * 1024;

#[derive(Clone)]
struct Upstream {
    client: Client,
    health_url: Url,
    predict_url: Url,
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
        })
    }
}

fn gateway(upstream: Upstream) -> Router {
    Router::new()
        .route("/health", get(health))
        .route(
            "/predict",
            post(predict).layer(DefaultBodyLimit::max(MAX_PREDICT_BODY_BYTES)),
        )
        .with_state(upstream)
}

async fn health(State(upstream): State<Upstream>) -> Response {
    relay(
        upstream.client.get(upstream.health_url),
        &[StatusCode::OK],
        "health check",
    )
    .await
}

async fn predict(State(upstream): State<Upstream>, headers: HeaderMap, body: Bytes) -> Response {
    let mut request = upstream.client.post(upstream.predict_url).body(body);
    if let Some(content_type) = headers.get(header::CONTENT_TYPE) {
        request = request.header(header::CONTENT_TYPE, content_type);
    }
    relay(
        request,
        &[StatusCode::OK, StatusCode::UNPROCESSABLE_ENTITY],
        "prediction",
    )
    .await
}

async fn relay(
    request: RequestBuilder,
    accepted_statuses: &[StatusCode],
    operation: &str,
) -> Response {
    let response = match request.send().await {
        Ok(response) if accepted_statuses.contains(&response.status()) => response,
        Ok(_) => return gateway_error(StatusCode::BAD_GATEWAY, operation, "failed"),
        Err(error) => return upstream_error(error, operation),
    };

    let status = response.status();
    let content_type = response.headers().get(header::CONTENT_TYPE).cloned();
    let body = match response.bytes().await {
        Ok(body) => body,
        Err(error) => return upstream_error(error, operation),
    };

    let mut response = (status, body).into_response();
    if let Some(content_type) = content_type {
        response
            .headers_mut()
            .insert(header::CONTENT_TYPE, content_type);
    }
    response
}

fn upstream_error(error: reqwest::Error, operation: &str) -> Response {
    if error.is_timeout() && !error.is_connect() {
        return gateway_error(StatusCode::GATEWAY_TIMEOUT, operation, "timed out");
    }
    gateway_error(StatusCode::BAD_GATEWAY, operation, "failed")
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
    let base_url =
        env::var("FASTAPI_BASE_URL").map_err(|_| "Set FASTAPI_BASE_URL to the FastAPI base URL")?;
    let app = gateway(Upstream::new(&base_url)?);
    let listener = TcpListener::bind("127.0.0.1:8080").await?;
    println!("Rust gateway listening on {}", listener.local_addr()?);
    axum::serve(listener, app).await?;
    Ok(())
}

#[cfg(test)]
mod tests;
