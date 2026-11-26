CoinAgentX Simple Reversal v7.0.1 — Bybit HTTP 403 Fix

What changed:
- Indonesia endpoint remains https://api.bybit.id (the Bybit-documented endpoint for Indonesia).
- Global fallbacks are https://api.bytick.com and https://api.bybit.com.
- Removed stale/incorrect fallback domains api.bybit.nl and api.bybit-tr.com.
- Generic HTTP 403 is not automatically labeled as a geo-restriction. Explicit regional text / HTTP 451 is classified separately.
- If all official hosts fail, the exception lists each host and returned HTTP status.
- Streamlit Secrets may optionally define COINAGENT_BYBIT_BASE using a Bybit-documented hostname appropriate to the account/site.

Deploy:
1. Replace the GitHub repository with the complete contents of this ZIP, keeping app.py and simple_engine.py in the repository root.
2. Commit/push to the branch used by Streamlit Community Cloud.
3. Reboot the Streamlit app.
4. Run check_market_access.py in the same hosting environment (or use its output from the app diagnostics) and share the complete JSON result if the 403 persists.

Important:
This patch improves official endpoint selection and diagnostics; it cannot override Bybit's IP, regional, WAF, or rate-limit restrictions. Bybit documents api.bybit.id for Indonesia and api.bybit.com / api.bytick.com as global mainnet endpoints. A 403 from the hosting environment may remain until that host's egress IP is accepted by Bybit. Do not use undocumented regional hosts to bypass restrictions.

Validation:
- 13 local automated tests passed.
- Python compilation passed.
- A live request from Streamlit Cloud was not performed in this build environment.

Optional diagnostic:
GitHub repository > Actions > CoinAgentX Market Access Diagnostic > Run workflow.
This diagnoses the GitHub Actions runner's egress, which may differ from Streamlit Cloud.
