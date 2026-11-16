# Trigger backend credentials

The browser-facing agent execute route verifies the Supabase user, then signs a
90-minute `trigger:agent` ticket. The Trigger payload carries that ticket and
the verified user ID, never the Supabase access token. The backend accepts the
ticket only for `POST /api/v1/agent/execute`, `GET /api/v1/agent/jobs/{id}`,
and `POST /api/v1/agent/confirm/{id}`. Normal Supabase and CLI tokens keep
their existing verification path. The backend tenant middleware and HTTP auth
dependency enforce the same Trigger scope and route checks.

Configure `TRIGGER_AGENT_SIGNING_KEY` with the same independent random secret
(at least 32 bytes) on the **frontend server** and **backend**. Do not expose it
through a `NEXT_PUBLIC_` variable. Missing or short keys fail closed.

Other Trigger jobs use a two-minute `trigger:service` JWT signed at request
time. Configure a different `TRIGGER_SERVICE_SIGNING_KEY` on **Trigger.dev**
and **backend**, plus the same `TRIGGER_SERVICE_USER_ID` on both. The ID must
name an existing active backend user; its current organization and permissions
bound service requests. The backend will not provision this identity from a
service ticket. The JWT binds one HTTP method and path, and the backend also
limits it to the Trigger client route allowlist in
[`backend/src/core/security.py`](../../backend/src/core/security.py).
The Supabase service-role key is not a backend bearer for these jobs.

`BACKEND_API_URL` in Trigger.dev defaults to `https://localhost:8000`. Set it
to the deployed HTTPS backend URL. Local HTTP development needs an explicit
`BACKEND_API_URL=http://localhost:8000` override.

The route-scoping regression tests are
[`backend/tests/unit/core/test_trigger_backend_auth.py`](../../backend/tests/unit/core/test_trigger_backend_auth.py);
the payload and client tests are in
[`frontend/src/lib/__tests__/`](../../frontend/src/lib/__tests__/).
