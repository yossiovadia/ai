// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Praxis Contributors

//! Unit tests for the JWT authentication filter.

// -----------------------------------------------------------------------------
// Config Tests
// -----------------------------------------------------------------------------

#[test]
fn config_rejects_empty_jwks_url() {
    let yaml: serde_yaml::Value = serde_yaml::from_str(
        r#"
jwks_url: ""
claim_headers:
  preferred_username: "x-tenant-username"
"#,
    )
    .unwrap();
    match super::JwtAuthFilter::from_config(&yaml) {
        Err(err) => assert!(
            err.to_string().contains("jwks_url must not be empty"),
            "should reject empty jwks_url: {err}"
        ),
        Ok(_) => panic!("empty jwks_url should be rejected"),
    }
}

#[test]
fn config_rejects_empty_claim_headers() {
    let yaml: serde_yaml::Value = serde_yaml::from_str(
        r#"
jwks_url: "http://keycloak:8080/certs"
claim_headers: {}
"#,
    )
    .unwrap();
    match super::JwtAuthFilter::from_config(&yaml) {
        Err(err) => assert!(
            err.to_string().contains("claim_headers must have at least one"),
            "should reject empty claim_headers: {err}"
        ),
        Ok(_) => panic!("empty claim_headers should be rejected"),
    }
}

#[test]
fn config_rejects_unknown_fields() {
    let yaml: serde_yaml::Value = serde_yaml::from_str(
        r#"
jwks_url: "http://keycloak:8080/certs"
claim_headers:
  sub: "x-user"
bogus_field: true
"#,
    )
    .unwrap();
    assert!(
        super::JwtAuthFilter::from_config(&yaml).is_err(),
        "unknown fields should be rejected"
    );
}
