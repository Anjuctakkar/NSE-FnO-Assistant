---
name: breeze-api-connect-test
description: Configure, test, and troubleshoot the ICICI Breeze API integration in the NSE F&O app without exposing credentials or placing trades.
---

# ICICI Breeze connection and testing

Use this skill when configuring or diagnosing the app's read-only ICICI Breeze connection, live quote requests, or credential test.

## App behavior

- The **Settings** tab accepts the Breeze API key, API secret, and session token, and posts them to `POST /api/fno/breeze/test`.
- The connection test creates a short-lived client and makes a read-only NSE cash quote request for TCS. The signed request validates the API secret/checksum path used by live quotes; it does not place orders or persist credentials.
- Credentials typed into Settings stay in page memory and are submitted to the configured F&O server for connection tests and live quotes. They are not written to browser storage. A server may still have normal access logs or process diagnostics; never add credentials to logs or error messages.
- With all three Settings fields blank, the app uses `BREEZE_API_KEY`, `BREEZE_API_SECRET`, and `BREEZE_SESSION_TOKEN` from the server environment.
- A live quote is fetched on demand from the one-stock view. The optional **ICICI Breeze live (5-minute)** source uses live cash/option-chain snapshots and Breeze 5-minute cash/ATM-option history for one-stock strategies; it does not fall back to bhavcopy.
- The Breeze SDK is optional and is installed with `python -m pip install -r requirements-live.txt`.

## Safe workflow

1. Never ask users to paste credentials into chat, source control, test fixtures, screenshots, or logs. Never inspect `.env` values.
2. Verify the app flow and credential validation with fake or mocked Breeze clients; do not make real broker requests during automated tests.
3. Keep Breeze integration read-only. Strategy data requests may read live option chains and historical candles, but must not place orders, change accounts, or persist credentials.
4. Test backend changes with the Breeze and server tests, plus the hosted API test if the FastAPI route changes. Check the UI syntax and smoke tests when modifying `fno/static/fno.js`.
5. Report only whether authentication succeeded and actionable sanitized failure hints. Do not return customer details or raw upstream exceptions.

## Relevant implementation

- `nse-fno/fno/breeze_market.py`: SDK initialization, quote/chain/history normalization, and authentication checks.
- `nse-fno/fno/service.py`: credential validation, environment fallback, live-data strategy orchestration, and test result.
- `nse-fno/fno/server.py`: standalone HTTP route.
- `nse-fno/fno/api.py`: authenticated dashboard route.
- `nse-fno/fno/static/fno.js`: Settings tab and on-demand quote UI.
- `nse-fno/tests/test_breeze_market.py`, `nse-fno/tests/test_server.py`, `nse-fno/tests/test_api.py`, and `nse-fno/tests/test_ui.py`: focused coverage.

Preserve the current invariant that Breeze credentials are used only for read-only market-data requests and authentication checks. Do not claim a connection is verified merely because the SDK client object was constructed.
