// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Praxis Contributors

//! Tests for the stream usage injection example config.
//!
//! The filter rewrites streaming chat-completion requests so the
//! upstream always sees `stream_options.include_usage = true`, and
//! leaves everything else byte-identical. The capturing backend proves
//! what actually reaches the upstream.

use std::collections::HashMap;

use praxis_test_utils::{StatefulCapturingBackend, StatefulCapturingGuard, free_port, http_post, start_proxy};

// -----------------------------------------------------------------------------
// Helpers
// -----------------------------------------------------------------------------

const RESPONSE_BODY: &str = r#"{"id":"chatcmpl-1","object":"chat.completion","choices":[{"index":0,"message":{"role":"assistant","content":"hi"},"finish_reason":"stop"}]}"#;

/// Return the raw body of the single forwarded chat-completion request,
/// ignoring the warmup probe the capturing backend also records.
fn forwarded_chat_body(backend: &StatefulCapturingGuard) -> String {
    let requests = backend.requests();
    let chats: Vec<_> = requests.iter().filter(|r| r.uri == "/v1/chat/completions").collect();
    assert_eq!(chats.len(), 1, "backend should see exactly one forwarded chat request");
    chats[0].body.clone()
}

fn start(config_name: &str) -> (praxis_test_utils::ProxyGuard, StatefulCapturingGuard) {
    let backend = StatefulCapturingBackend::new(vec![(200, RESPONSE_BODY.to_owned())]).start_with_shutdown();
    let proxy_port = free_port();
    let config = super::load_example_config(
        config_name,
        proxy_port,
        HashMap::from([("127.0.0.1:3000", backend.port())]),
    );
    (start_proxy(&config), backend)
}

// -----------------------------------------------------------------------------
// Tests
// -----------------------------------------------------------------------------

#[test]
fn streaming_request_receives_include_usage_upstream() {
    let (proxy, backend) = start("stream-usage-inject.yaml");

    let sent = r#"{"model":"gpt-4","stream":true,"messages":[{"role":"user","content":"hi"}]}"#;
    let (status, _) = http_post(proxy.addr(), "/v1/chat/completions", sent);
    assert_eq!(status, 200, "streaming request should be proxied upstream");

    let body = forwarded_chat_body(&backend);
    let forwarded: serde_json::Value =
        serde_json::from_str(&body).unwrap_or_else(|e| panic!("backend should receive valid JSON: {e}: {body}"));
    assert_eq!(
        forwarded["stream_options"]["include_usage"].as_bool(),
        Some(true),
        "include_usage must be injected before the upstream sees the body"
    );
    assert_eq!(
        forwarded["stream"].as_bool(),
        Some(true),
        "stream flag must be preserved"
    );
}

#[test]
fn non_streaming_request_passes_through_unchanged() {
    let (proxy, backend) = start("stream-usage-inject.yaml");

    let sent = r#"{"model":"gpt-4","messages":[{"role":"user","content":"hi"}]}"#;
    let (status, _) = http_post(proxy.addr(), "/v1/chat/completions", sent);
    assert_eq!(status, 200, "non-streaming request should be proxied upstream");

    let body = forwarded_chat_body(&backend);
    let forwarded: serde_json::Value =
        serde_json::from_str(&body).unwrap_or_else(|e| panic!("backend should receive valid JSON: {e}: {body}"));
    assert!(
        forwarded.get("stream_options").is_none(),
        "non-streaming bodies must not be rewritten, got: {body}"
    );
    let original: serde_json::Value = serde_json::from_str(sent).unwrap();
    assert_eq!(forwarded, original, "non-streaming body must reach upstream unchanged");
}

#[test]
fn client_provided_include_usage_is_not_duplicated() {
    let (proxy, backend) = start("stream-usage-inject.yaml");

    let sent = r#"{"model":"gpt-4","stream":true,"stream_options":{"include_usage":true},"messages":[{"role":"user","content":"hi"}]}"#;
    let (status, _) = http_post(proxy.addr(), "/v1/chat/completions", sent);
    assert_eq!(status, 200, "request already carrying the opt-in should be proxied");

    let body = forwarded_chat_body(&backend);
    assert_eq!(
        body.matches("\"stream_options\"").count(),
        1,
        "the opt-in must not be injected twice: {body}"
    );
    let forwarded: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(forwarded["stream_options"]["include_usage"].as_bool(), Some(true));
}
