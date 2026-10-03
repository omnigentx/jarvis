# CI auth test-order regression

Failing run: https://github.com/omnigentx/jarvis/actions/runs/37107832014
Backend result: 1 failed, 2528 passed. Frontend build and E2E passed.

Failure: test_manual_http_install_is_immediately_readable_by_real_runtime received HTTP 401 on installation.

Root cause: test_routes/test_auth.py reloads core.auth. routes.plugins retains the dependency callable from module initialization, while the integration test imported the newly created callable and used it as a dependency_overrides key. Those identities differ. The test passed alone but did not override authentication after the reload.

Local reproduction:

```sh
cd backend
UV_FROZEN=1 uv run pytest tests/test_routes/test_auth.py tests/test_routes/test_plugins.py tests/test_services/test_plugin_manual_review.py -q --tb=short
```

Before: 1 failed, 16 passed (HTTP 401 instead of 200).
After: 17 passed.

Fix: the integration test pins a deterministic test API key and authenticates through the real Bearer path. It also checks that an unauthenticated install returns 401 and creates no package. No production auth changes or removed assertions.
