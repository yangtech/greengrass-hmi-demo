# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please report it responsibly:

1. **Do NOT open a public GitHub issue**
2. Email: chenaws@amazon.com
3. Include a description of the vulnerability and steps to reproduce

## Security Considerations

This is a **demo project** intended for educational and demonstration purposes. It is NOT designed for production use without additional hardening:

- **Flask Secret Key**: Must be set via environment variable (`FLASK_SECRET_KEY`). The `.env.example` file provides a template.
- **Authentication**: The hybrid Cognito/SQLite auth is designed for demo scenarios. Production deployments should use Cognito exclusively with proper token management.
- **HTTPS**: The demo runs on HTTP. Production deployments should use HTTPS (e.g., via Nginx reverse proxy with TLS).
- **Ollama**: Runs without authentication on localhost. Do not expose port 11434 to external networks.
- **IAM Permissions**: The demo uses broad permissions for convenience. Production deployments should follow least-privilege principles.

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.4.x   | ✅ Current |
| < 1.4   | ❌ Not supported |
