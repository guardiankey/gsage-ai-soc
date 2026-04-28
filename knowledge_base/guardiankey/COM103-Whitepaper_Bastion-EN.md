---
title: "Whitepaper – GuardianKey Auth Bastion"
author: "GuardianKey"
date: "November/2025"
subject: "Whitepaper GuardianKey Auth Bastion"
keywords: [GuardianKey, Auth Bastion, MFA, GovBR, OIDC, SAML, Proxy, Security, Legacy]
subtitle: "Authentication bastion for legacy and modern systems"
...

# Whitepaper – GuardianKey Auth Bastion

## Summary

GuardianKey Auth Bastion is an innovative solution that acts as an intelligent authentication proxy, enabling the addition of multi-factor authentication (MFA), integration with OAuth2/OIDC, and adaptive access policies to legacy systems without the need to change source code. This allows organizations to drastically increase the security level of their critical systems quickly, cost-effectively, and without compatibility risks.

## Challenges

Many organizations still operate legacy applications that are critical to their business—ERPs, management systems, educational platforms, hospital software, or proprietary web applications—but were not designed to support MFA, OAuth2/OIDC, or integration with modern identity providers.

Updating these systems can be expensive, time-consuming, and risky, and is often unfeasible due to lack of vendor support or the risk of operational impact. As a result, strategic applications remain exposed to unauthorized access, fraud, and unmet regulatory requirements.

## Our Solution

GuardianKey Auth Bastion sits between the user and the protected system, acting as an authentication bastion:

- Intercepts login requests before they reach the original application.
- Applies additional authentication and authorization policies, such as MFA, SSO, georestriction, or integration with GovBR.
- After validating the identity, injects the session into the legacy system, allowing the user to access it without noticing changes in the original flow.

Thus, applications that were never designed to support MFA or OIDC can now offer these features transparently and centrally.

## Benefits

- Immediate protection of legacy systems, without code changes.
- Rapid adoption of MFA, OIDC, and OAuth2 in critical applications.
- Centralization of authentication and authorization policies.
- Flexible deployment in on-premise, cloud, or hybrid environments.
- Compliance with standards and regulations (GDPR, ISO, PCI-DSS, strong authentication).
- Lower cost and implementation time compared to software rewrite projects.
- Integrated two-factor authentication (2FA), supporting TOTP (RFC6238), email/SMS tokens, and other configurable options.
- Intuitive administration panel with dashboards, user management, access policies, and complete auditing.

## Use Cases

- **Public agencies**: seamless integration with OAuth2/OIDC without modifying existing systems. Adoption of multi-factor authentication (MFA) without code changes.
- **Hospitals and universities**: applying MFA to old systems without native support.
- **Private companies**: unified authentication via OIDC/SAML across various applications.
- **Cloud and SaaS**: additional security layer for customer and partner portals.
- **Ideal for legacy systems, public portals, and modern applications.**

## Conclusion

GuardianKey Auth Bastion is the bridge between legacy and the future of authentication. With it, any application can benefit from MFA, integration with OAuth2/OIDC, and adaptive authentication—without code changes, without compatibility risks, and with rapid implementation.

---

**Learn more:**  
[guardiankey.io/](https://guardiankey.io/)  
[guardiankey.io/docs/](https://guardiankey.io/docs/)

