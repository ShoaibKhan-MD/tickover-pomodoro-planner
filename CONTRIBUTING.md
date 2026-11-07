# Contributing to Tickover

Thanks for considering a contribution.

## Before opening a pull request

1. Open an issue for a significant feature or behavior change so the design can be discussed first.
2. Keep the app local-first and single-user unless a proposal explicitly designs and tests safe multi-user isolation.
3. Do not add telemetry, advertising, cloud accounts, or external services without clear user consent and documentation.
4. Preserve existing user data and add an additive database migration for schema changes.
5. Treat AI-agent writes as untrusted input: validate payloads, use stable `external_id` values, and use revision checks for whole-plan changes.
6. Keep API changes documented in the README and FastAPI/OpenAPI schema.

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\\Scripts\\activate
pip install -r requirements-dev.txt
pytest -q
```

For frontend changes, extract the inline script from `static/index.html` and run `node --check` on it, or rely on the CI workflow.

## Pull requests

Include a concise problem statement, screenshots for visible UI changes, test coverage for behavior changes, and notes about database migrations or API compatibility. Never include real schedules, database files, API tokens, `.env` files, or personal data in a pull request.
