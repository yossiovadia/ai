// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Praxis Contributors

//! JWKS (JSON Web Key Set) fetching and caching.
//!
//! Downloads public keys from the IdP's JWKS endpoint, caches them
//! by `kid` (Key ID), and provides lookup for JWT verification.
//! Keys are refreshed when an unknown `kid` is encountered (with
//! a cooldown to prevent abuse) or when the cache TTL expires.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};

use jsonwebtoken::jwk::JwkSet;
use jsonwebtoken::{DecodingKey, Algorithm};
use tokio::sync::RwLock;
use tracing::{debug, warn};

// -----------------------------------------------------------------------------
// Constants
// -----------------------------------------------------------------------------

/// Minimum interval between JWKS refresh attempts.
const REFRESH_COOLDOWN: Duration = Duration::from_secs(30);

/// Default TTL for cached keys.
const DEFAULT_TTL: Duration = Duration::from_secs(300); // 5 minutes

/// Maximum response size for JWKS endpoint.
const MAX_JWKS_BYTES: usize = 1_048_576; // 1 MiB

// -----------------------------------------------------------------------------
// JwksCache
// -----------------------------------------------------------------------------

/// Thread-safe cache of JWKS decoding keys.
pub(super) struct JwksCache {
    /// Cached keys indexed by `kid`.
    keys: Arc<RwLock<CachedKeys>>,

    /// HTTP client for fetching JWKS.
    client: reqwest::Client,

    /// JWKS endpoint URL.
    url: String,
}

struct CachedKeys {
    /// Decoding keys by `kid`.
    by_kid: HashMap<String, (DecodingKey, Algorithm)>,

    /// When the cache was last refreshed.
    last_refresh: Instant,
}

impl JwksCache {
    /// Create a new cache. Keys are fetched lazily on the first
    /// request rather than at construction, because the filter
    /// is built during config parsing before the async runtime
    /// is fully available.
    pub(super) fn new(url: String) -> Result<Self, String> {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(10))
            .build()
            .map_err(|e| format!("jwt_auth: failed to build HTTP client: {e}"))?;

        Ok(Self {
            keys: Arc::new(RwLock::new(CachedKeys {
                by_kid: HashMap::new(),
                last_refresh: Instant::now().checked_sub(DEFAULT_TTL).unwrap_or_else(Instant::now),
            })),
            client,
            url,
        })
    }

    /// Look up a decoding key by `kid`. Refreshes when:
    /// - the `kid` is unknown and the cooldown has elapsed, or
    /// - the cache TTL has expired (ensures revoked keys stop
    ///   working within one TTL window).
    pub(super) async fn get_key(&self, kid: &str) -> Option<(DecodingKey, Algorithm)> {
        // Check if TTL expired — refresh proactively so revoked
        // keys stop working within one TTL window.
        {
            let keys = self.keys.read().await;
            if keys.last_refresh.elapsed() >= DEFAULT_TTL {
                drop(keys);
                if let Err(e) = self.refresh().await {
                    warn!("JWKS TTL refresh failed: {e}");
                }
            }
        }

        // Fast path: key is cached
        {
            let keys = self.keys.read().await;
            if let Some(entry) = keys.by_kid.get(kid) {
                return Some(entry.clone());
            }
        }

        // Slow path: unknown kid, try refreshing
        {
            let keys = self.keys.read().await;
            if keys.last_refresh.elapsed() < REFRESH_COOLDOWN {
                debug!(kid, "unknown kid, cooldown active");
                return None;
            }
        }

        debug!(kid, "unknown kid, refreshing JWKS");
        if let Err(e) = self.refresh().await {
            warn!("JWKS refresh failed: {e}");
            return None;
        }

        let keys = self.keys.read().await;
        keys.by_kid.get(kid).cloned()
    }

    /// Fetch JWKS from the endpoint and update the cache.
    async fn refresh(&self) -> Result<(), String> {
        let resp = self
            .client
            .get(&self.url)
            .send()
            .await
            .map_err(|e| format!("JWKS fetch failed: {e}"))?;

        if !resp.status().is_success() {
            return Err(format!("JWKS endpoint returned {}", resp.status()));
        }

        let body = resp
            .bytes()
            .await
            .map_err(|e| format!("JWKS read failed: {e}"))?;

        if body.len() > MAX_JWKS_BYTES {
            return Err(format!(
                "JWKS response too large: {} bytes",
                body.len()
            ));
        }

        let jwk_set: JwkSet = serde_json::from_slice(&body)
            .map_err(|e| format!("JWKS parse failed: {e}"))?;

        let mut by_kid = HashMap::new();
        for jwk in &jwk_set.keys {
            let Some(kid) = jwk.common.key_id.as_ref() else {
                continue;
            };
            let algorithm = match jwk.common.key_algorithm.as_ref() {
                Some(jsonwebtoken::jwk::KeyAlgorithm::RS256) => Algorithm::RS256,
                Some(jsonwebtoken::jwk::KeyAlgorithm::RS384) => Algorithm::RS384,
                Some(jsonwebtoken::jwk::KeyAlgorithm::RS512) => Algorithm::RS512,
                Some(jsonwebtoken::jwk::KeyAlgorithm::ES256) => Algorithm::ES256,
                Some(jsonwebtoken::jwk::KeyAlgorithm::ES384) => Algorithm::ES384,
                // Azure AD omits `alg` — infer RS256 from key type.
                None => Algorithm::RS256,
                _ => continue,
            };

            match DecodingKey::from_jwk(jwk) {
                Ok(key) => {
                    by_kid.insert(kid.clone(), (key, algorithm));
                },
                Err(e) => {
                    warn!(kid, "failed to parse JWK: {e}");
                },
            }
        }

        debug!(count = by_kid.len(), "JWKS cache refreshed");

        let mut keys = self.keys.write().await;
        keys.by_kid = by_kid;
        keys.last_refresh = Instant::now();

        Ok(())
    }
}
