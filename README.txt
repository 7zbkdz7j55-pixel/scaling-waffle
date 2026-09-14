================================================================
 FENDRISK SECURITY PREFLIGHT
 Guided security self-assessment for AI-built apps
 by PromptForce AI · A B2 Stealthy Solutions product
================================================================

WHAT IT IS
----------
A single HTML file that walks you through 29 security checks
across 7 categories — the decisions AI coding tools tend to skip
when they generate a working demo:

  Secrets & Keys ................... 4 checks
  Authorization & Access Control ... 5 checks
  Input & Data Handling ............ 4 checks
  Deployment & Headers ............. 4 checks
  API & Payment Security ........... 5 checks
  AI & LLM Security ................ 3 checks
  Data & Compliance ................ 4 checks
                                    ----------
  Total                             29 checks

Each check has a severity (10 critical, 13 high, 6 medium), a
plain-language description, a "How do I check this?" panel with
the exact DevTools steps or shell command to run, and a fix to
apply if it fails.

You mark each item Confirmed, Uncertain, or Failed. A dashboard
tracks your progress and a summary bar tells you where you stand.
Export Report produces a plain-text report you can copy to your
clipboard or print.


HOW TO RUN IT
-------------
Double-click index.html, or drag it into any modern browser.
That is the whole installation. It works offline, from a USB
stick, or from any folder. It can also be uploaded to any static
host as-is.


REQUIREMENTS
------------
- A modern browser (Chrome, Edge, Firefox, or Safari, current or
  one version behind).
- JavaScript enabled. If it is off, the page says so plainly
  rather than showing a broken screen.
- No internet connection required. No account. No install.
- "Copy to Clipboard" uses the browser clipboard API, which most
  browsers only permit on https:// pages. Opened directly from
  disk (file://) the copy button may be blocked by the browser —
  the tool tells you when that happens so you can select the
  report text and copy it manually. Printing always works.


WHAT IT DOES NOT DO
-------------------
Read this part. It is the honest boundary of the product, and it
is the same boundary stated inside the tool itself.

- It does NOT scan your code. It never reads your repository,
  your files, or your running application.
- It does NOT test your live app. No requests are made to your
  site, your API, or anything else.
- It is NOT a security audit, penetration test, or compliance
  certification, and it is not evidence of one.
- "Confirmed" means YOU checked and believe the item passes. The
  tool cannot verify that, and does not try to. A report full of
  green marks proves only what you told it.
- It does NOT cover every security risk. 29 checks is a
  high-value starting set, not a complete threat model.
- It does NOT store anything off your device. Your marks are
  saved in your browser's localStorage so your progress survives
  a reload. Nothing is transmitted — the tool makes no network
  requests at all.

Applications handling payments, health data, or personal data at
scale should get a professional security review. This tool helps
you arrive at that review better prepared; it does not replace it.


PRIVACY
-------
Fendrisk Security Preflight makes no network requests. You can
verify this: open DevTools, go to the Network tab, and use the
tool. You will see no outbound requests.

Your progress is stored only in your own browser (localStorage,
key "fendrisk-security-preflight-v1"). Clearing your browser data
clears it. Reset All clears it immediately.


SUPPORT
-------
support@b2stealthysolutions.com

Include your browser and version. Never send API keys, secrets,
passwords, or card numbers by email — including examples pasted
from your app.


EDITION
-------
This is the free edition of Fendrisk Security Preflight. It is
free to use and free to keep, but not free to redistribute — see
LICENSE.txt. Share the download link rather than the file.

Other B2 Stealthy Solutions tools are listed at
https://www.b2stealthysolutions.com


LICENCE
-------
See LICENSE.txt. Free to use, including commercially. Resale,
redistribution, and re-hosting are not permitted.

© 2026 B2 Stealthy Solutions.
