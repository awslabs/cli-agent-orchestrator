# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 1.x.x   | :white_check_mark: |
| < 1.0   | :x:                |

## Reporting a Vulnerability

We take the security of CLI Agent Orchestrator seriously. If you believe you have found a security vulnerability, please report it to us as described below.

### How to Report

**Please do not report security vulnerabilities through public GitHub issues.**

Instead, please report them through one of the following methods:

1. **GitHub Security Advisories**: Use the [Security Advisories](https://github.com/awslabs/cli-agent-orchestrator/security/advisories) feature to privately report a vulnerability.

2. **Email**: Send an email to the AWS Security team. See [AWS Vulnerability Reporting](https://aws.amazon.com/security/vulnerability-reporting/) for details.

### What to Include

Please include the following information in your report:

- Type of issue (e.g., buffer overflow, SQL injection, cross-site scripting, etc.)
- Full paths of source file(s) related to the manifestation of the issue
- The location of the affected source code (tag/branch/commit or direct URL)
- Any special configuration required to reproduce the issue
- Step-by-step instructions to reproduce the issue
- Proof-of-concept or exploit code (if possible)
- Impact of the issue, including how an attacker might exploit it

### Response Timeline

- **Initial Response**: Within 48 hours, we will acknowledge receipt of your report.
- **Status Update**: Within 7 days, we will provide an initial assessment.
- **Resolution**: We aim to resolve critical vulnerabilities within 30 days.

## Security Scanning

This project uses automated security scanning to identify vulnerabilities:

### Trivy Vulnerability Scanner

We use [Trivy](https://github.com/aquasecurity/trivy) to scan for:

- **Filesystem vulnerabilities**: Scans Python dependencies and configuration files
- **Configuration issues**: Checks for misconfigurations in IaC files
- **Secret detection**: Identifies accidentally committed secrets

Security scans run:
- On every push to the `main` branch
- On every pull request targeting `main`

### Dependency Review

Pull requests are automatically checked for:
- Known vulnerabilities in dependencies
- License compliance issues
- Dependency version changes

### Running Security Scans Locally

You can run Trivy locally to check for vulnerabilities before committing:

```bash
# Install Trivy
brew install trivy  # macOS
# or
sudo apt-get install trivy  # Ubuntu/Debian

# Scan the repository
trivy fs --severity HIGH,CRITICAL .

# Scan Python dependencies
trivy fs --scanners vuln --severity HIGH,CRITICAL .
```

## Security Best Practices

When using CLI Agent Orchestrator:

1. **Keep Dependencies Updated**: Regularly update to the latest version to get security patches.

2. **Secure API Access**: The CAO server runs on localhost by default. If exposing externally, use proper authentication and TLS.

3. **Agent Profiles**: Review agent profiles before installation, especially those from external sources.

4. **Environment Variables**: Never commit sensitive environment variables. Use `.env` files (excluded from git) or secure secret management.

5. **Tmux Sessions**: CAO manages tmux sessions that may contain sensitive information. Ensure proper access controls on the host system.

## Dependency Management

We actively monitor and update dependencies to address security vulnerabilities:

- **Dependabot**: Automated dependency updates via GitHub Dependabot
- **uv.lock**: Locked dependency versions for reproducible builds
- **Regular Audits**: Periodic review of dependency tree for security issues

## Security Updates

Security updates are released as patch versions (e.g., 1.0.1) and are documented in:

- [CHANGELOG.md](CHANGELOG.md)
- [GitHub Releases](https://github.com/awslabs/cli-agent-orchestrator/releases)
- [GitHub Security Advisories](https://github.com/awslabs/cli-agent-orchestrator/security/advisories)

## License

This project is licensed under the Apache-2.0 License. See [LICENSE](LICENSE) for details.
