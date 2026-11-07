# Security policy

Tickover is a single-user, self-hosted application. It is not designed to be exposed as one shared public service for unrelated users.

## Safe deployment

- Keep the server bound to `127.0.0.1` unless you deliberately configure a trusted private network.
- When remote access is necessary, use HTTPS, restrict network access, set `APP_PASSWORD`, and protect the agent bearer token.
- Never place `AGENT_API_TOKEN` in frontend code, a public repository, screenshots, prompts shared with strangers, or logs.
- Back up the SQLite database before restoring or changing deployment configuration.
- Do not run the app with a shared database for multiple users; the current data model does not provide per-user isolation.

## Reporting a vulnerability

Do not publish working exploit details, API tokens, database files, or personal data in a public issue. Contact the maintainer privately through the email or contact method listed on the maintainer's GitHub profile. Include the affected version, impact, reproduction steps, and any safe proof-of-concept details.
