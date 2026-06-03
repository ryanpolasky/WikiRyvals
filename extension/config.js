"use strict";

// Single source of truth for the WikiRyvals backend origin.
//
// Chrome extensions ship as static files and run in the browser - there is no
// runtime or .env, so the origin is baked in here (and must ALSO be listed
// literally in manifest.json "host_permissions"; Chrome can't read it from here).
//
// It's environment-aware: an unpacked (dev) load has no `update_url` in its
// manifest, while a Chrome Web Store install does. So loading unpacked hits the
// localhost API and the published build hits production automatically - no manual
// edit when you package. Both URLs are in manifest.json "host_permissions".
//
// Loaded by the background service worker (via importScripts) and by the
// lobby/watch pages (via a <script> tag before their own script), so it's set on
// globalThis to be readable from every extension context.
const _wrIsDev = !("update_url" in chrome.runtime.getManifest());
globalThis.WIKIRYVALS_BACKEND = _wrIsDev
  ? "http://127.0.0.1:8011"
  : "https://api.wikiryvals.com";
