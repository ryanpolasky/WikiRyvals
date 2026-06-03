"use strict";

// Single source of truth for the WikiRyvals backend origin.
//
// DEPLOY: change this ONE line to your production origin
// (e.g. "https://api.wikiryvals.com"), then update the matching entry in
// manifest.json "host_permissions" - Chrome requires that URL literally and
// can't read it from here. Local-dev default is the localhost API.
//
// Loaded by the background service worker (via importScripts) and by the
// lobby/watch pages (via a <script> tag before their own script), so it's set on
// globalThis to be readable from every extension context.
globalThis.WIKIRYVALS_BACKEND = "http://127.0.0.1:8011";
