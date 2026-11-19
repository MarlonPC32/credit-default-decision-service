use super::*;
use std::{
    future::pending,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
};

async fn serve(app: Router) -> String {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    format!("http://{address}")
}

async fn get_health(upstream_url: &str) -> reqwest::Response {
    let gateway_url = serve(gateway(Upstream::new(upstream_url).unwrap())).await;
    test_client()
        .get(format!("{gateway_url}/health"))
        .send()
        .await
        .unwrap()
}

fn test_client() -> Client {
    Client::builder()
        .no_proxy()
        .timeout(Duration::from_secs(10))
        .build()
        .unwrap()
}

async fn send_prediction(gateway_url: &str, body: impl Into<reqwest::Body>) -> reqwest::Response {
    test_client()
        .post(format!("{gateway_url}/predict"))
        .header(header::CONTENT_TYPE, "application/json; charset=utf-8")
        .body(body)
        .send()
        .await
        .unwrap()
}

#[tokio::test]
async fn relays_the_upstream_health_response() {
    const UPSTREAM_BODY: &str = r#"{"status":"ok","model":"test model","threshold":0.15}"#;
    let upstream = Router::new().route(
        "/health",
        get(|| async { ([(header::CONTENT_TYPE, "application/json")], UPSTREAM_BODY) }),
    );
    let response = get_health(&serve(upstream).await).await;

    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(response.headers()[header::CONTENT_TYPE], "application/json");
    assert_eq!(response.text().await.unwrap(), UPSTREAM_BODY);
}

#[tokio::test]
async fn returns_bad_gateway_when_connection_is_refused() {
    let socket = tokio::net::TcpSocket::new_v4().unwrap();
    socket.bind("127.0.0.1:0".parse().unwrap()).unwrap();
    let address = socket.local_addr().unwrap();

    let response = get_health(&format!("http://{address}")).await;

    assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
    assert_eq!(response.headers()[header::CONTENT_TYPE], "application/json");
    assert_eq!(
        response.text().await.unwrap(),
        r#"{"error":"FastAPI health check failed"}"#
    );
}

#[tokio::test]
async fn returns_bad_gateway_when_upstream_returns_an_error() {
    let upstream = Router::new().route(
        "/health",
        get(|| async { StatusCode::INTERNAL_SERVER_ERROR }),
    );
    let response = get_health(&serve(upstream).await).await;

    assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
}

#[tokio::test]
async fn forwards_prediction_bytes_and_content_type_and_relays_success() {
    const INPUT: &[u8] = b"{ \"AGE\" : 35 }\n";
    const OUTPUT: &[u8] =
        b"{ \"default_probability\": 0.3174, \"threshold\": 0.15, \"decision\": 1 }\n";
    let upstream = Router::new().route(
        "/predict",
        post(|headers: HeaderMap, body: Bytes| async move {
            assert_eq!(
                headers[header::CONTENT_TYPE],
                "application/json; charset=utf-8"
            );
            assert_eq!(body.as_ref(), INPUT);
            ([(header::CONTENT_TYPE, "application/json")], OUTPUT)
        }),
    );
    let gateway_url = serve(gateway(Upstream::new(&serve(upstream).await).unwrap())).await;

    let response = send_prediction(&gateway_url, INPUT).await;

    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(response.headers()[header::CONTENT_TYPE], "application/json");
    assert_eq!(response.bytes().await.unwrap().as_ref(), OUTPUT);
}

#[tokio::test]
async fn relays_fastapi_validation_status_and_body() {
    const DETAIL: &[u8] =
        b"{ \"detail\": [{\"loc\":[\"body\",\"AGE\"],\"msg\":\"Field required\"}] }\n";
    let upstream = Router::new().route(
        "/predict",
        post(|body: Bytes| async move {
            assert_eq!(body.as_ref(), b"{}");
            (
                StatusCode::UNPROCESSABLE_ENTITY,
                [(header::CONTENT_TYPE, "application/json")],
                DETAIL,
            )
        }),
    );
    let gateway_url = serve(gateway(Upstream::new(&serve(upstream).await).unwrap())).await;

    let response = send_prediction(&gateway_url, "{}").await;

    assert_eq!(response.status(), StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(response.headers()[header::CONTENT_TYPE], "application/json");
    assert_eq!(response.bytes().await.unwrap().as_ref(), DETAIL);
}

#[tokio::test]
async fn forwards_at_the_limit_and_rejects_larger_bodies_before_contacting_upstream() {
    let upstream_calls = Arc::new(AtomicUsize::new(0));
    let calls = Arc::clone(&upstream_calls);
    let upstream = Router::new().route(
        "/predict",
        post(move |body: Bytes| async move {
            calls.fetch_add(1, Ordering::SeqCst);
            assert_eq!(body.len(), MAX_PREDICT_BODY_BYTES);
            StatusCode::OK
        }),
    );
    let gateway_url = serve(gateway(Upstream::new(&serve(upstream).await).unwrap())).await;

    let oversized = send_prediction(&gateway_url, vec![b' '; MAX_PREDICT_BODY_BYTES + 1]).await;
    assert_eq!(oversized.status(), StatusCode::PAYLOAD_TOO_LARGE);
    assert_eq!(upstream_calls.load(Ordering::SeqCst), 0);

    let at_limit = send_prediction(&gateway_url, vec![b' '; MAX_PREDICT_BODY_BYTES]).await;
    assert_eq!(at_limit.status(), StatusCode::OK);
    assert_eq!(upstream_calls.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn prediction_returns_bad_gateway_on_connection_failure() {
    let socket = tokio::net::TcpSocket::new_v4().unwrap();
    socket.bind("127.0.0.1:0".parse().unwrap()).unwrap();
    let upstream_url = format!("http://{}", socket.local_addr().unwrap());
    let gateway_url = serve(gateway(Upstream::new(&upstream_url).unwrap())).await;

    let response = send_prediction(&gateway_url, "{}").await;

    assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
}

#[tokio::test]
async fn prediction_returns_bad_gateway_on_unexpected_status() {
    let upstream = Router::new().route(
        "/predict",
        post(|| async { (StatusCode::INTERNAL_SERVER_ERROR, "internal error details") }),
    );
    let gateway_url = serve(gateway(Upstream::new(&serve(upstream).await).unwrap())).await;

    let response = send_prediction(&gateway_url, "{}").await;

    assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
    assert_eq!(
        response.text().await.unwrap(),
        r#"{"error":"FastAPI prediction failed"}"#
    );
}

#[tokio::test]
async fn prediction_returns_gateway_timeout_after_one_upstream_request() {
    let upstream_calls = Arc::new(AtomicUsize::new(0));
    let calls = Arc::clone(&upstream_calls);
    let upstream = Router::new().route(
        "/predict",
        post(move || async move {
            calls.fetch_add(1, Ordering::SeqCst);
            pending::<StatusCode>().await
        }),
    );
    let gateway_url = serve(gateway(Upstream::new(&serve(upstream).await).unwrap())).await;

    let response = send_prediction(&gateway_url, "{}").await;

    assert_eq!(response.status(), StatusCode::GATEWAY_TIMEOUT);
    assert_eq!(upstream_calls.load(Ordering::SeqCst), 1);
}
