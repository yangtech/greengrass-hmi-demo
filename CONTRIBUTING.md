# Contributing

Thank you for considering contributing to this project!

## How to Contribute

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Commit your changes (`git commit -m 'Add my feature'`)
4. Push to the branch (`git push origin feature/my-feature`)
5. Open a Pull Request

## Development Setup

1. Clone the repo
2. Copy `.env.example` to `.env` and fill in your AWS credentials
3. Set up an EC2 instance with IoT Greengrass v2 (see `infra/README.md`)
4. Install Ollama for offline AI mode (optional)
5. Deploy using `deploy.sh`

## Code Style

- Python: Follow PEP 8
- JavaScript: Standard style (no semicolons optional)
- HTML: Inline styles used for simplicity in this demo

## Reporting Issues

Please use GitHub Issues to report bugs or request features.

## Security

If you discover a security vulnerability, please see [SECURITY.md](SECURITY.md).
