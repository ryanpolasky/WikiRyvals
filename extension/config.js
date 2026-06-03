"use strict";

// Single source of truth for the WikiRyvals backend origin.
//
// Chrome extensions are static files running in the browser - there is no
// runtime or .env, so the origin is hardcoded here and must ALSO be listed
// literally in manifest.json "host_permissions" (Chrome can't read it from here).
//
// For local dev, point this at "http://127.0.0.1:8011" and add the matching
// "http://127.0.0.1:8011/*" entry back to manifest.json "host_permissions".
//
// Loaded by the background service worker (via importScripts) and by the
// lobby/watch pages (via a <script> tag before their own script), so it's set on
// globalThis to be readable from every extension context.
globalThis.WIKIRYVALS_BACKEND = "https://api.wikiryvals.com";
