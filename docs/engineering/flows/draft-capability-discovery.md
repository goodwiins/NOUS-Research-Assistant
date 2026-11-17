# Draft capability discovery

The frontend and backend can deploy independently. This endpoint reports only
whether the deployed backend has the routes required by the draft panels.
Browser QA is planned; the backend route and frontend fallback have unit tests.

```mermaid
flowchart TD
    A[Frontend requests draft capabilities] --> B[GET backend-capabilities]
    B --> C{Backend responds with booleans?}
    C -->|yes| D[Show only supported draft controls]
    C -->|no| E[Disable draft controls]
```

`/openapi.json` remains available only in DEBUG mode. The capability response
contains no tenant data or full API schema, and both sides send `Cache-Control:
no-store` so deployment changes become visible on the next request.
