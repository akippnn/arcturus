# Security policy

## Supported versions

Until v1.0, security fixes are applied to the latest published `0.99.x` release candidate only. After v1.0, this policy will be updated with a supported-release window.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability or exposed credential. Use the repository host's private security-advisory feature or contact the maintainer through a private channel listed on the maintainer profile.

Include:

- affected version or commit
- component and deployment mode
- reproduction steps or proof of concept
- expected impact
- whether credentials or production data may be involved

Do not access data that is not yours, disrupt a service, or retain secret material beyond what is necessary to report the issue.

## Response expectations

The maintainer will acknowledge a valid private report, assess severity, coordinate remediation, and publish a disclosure after affected users have a reasonable opportunity to update. Exact timelines depend on impact and maintainer availability.

## Operational incidents

Immediately revoke a credential believed to be exposed. Repository history rewriting is not credential revocation. Replace the credential through its provider, update dependents, verify the replacement, and only then sanitize public history or artifacts.
