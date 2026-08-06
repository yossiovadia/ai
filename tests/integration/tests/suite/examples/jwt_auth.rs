// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Praxis Contributors

//! Tests for the JWT authentication example configuration.
//!
//! Since `jwt_auth` requires a reachable JWKS endpoint at config
//! parse time, these tests validate the config structure only.
//! Full token validation is covered by unit tests with wiremock.

use std::collections::HashMap;

// -----------------------------------------------------------------------------
// Tests
// -----------------------------------------------------------------------------

#[test]
fn jwt_auth_config_parses() {
    let config = super::load_example_config("jwt-auth.yaml", 29930, HashMap::from([("127.0.0.1:3000", 29931_u16)]));

    assert_eq!(config.listeners.len(), 1, "should have 1 listener");
    assert_eq!(&*config.listeners[0].name, "gateway", "listener name should be gateway");
}
