# GreatGatsby Budget development rules

- Develop and test every change locally before publishing it.
- Use versioned GHCR images under `ghcr.io/charliec94/greatgatsby-budget`.
- Do not commit databases, bank statements, imports, `.env` files, tokens, passwords, or personal financial data.
- Keep runtime processing local. Do not add external APIs, telemetry, remote fonts, or CDNs without explicit approval.
- Preserve backward compatibility for the persisted database or provide a tested migration.
- Use laptop host port 8081 and container port 8000 unless the user changes them.
- Require a passing automated test suite before a release image is pushed.
