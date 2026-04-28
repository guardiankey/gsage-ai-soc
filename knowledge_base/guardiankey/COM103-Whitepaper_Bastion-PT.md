---
title: "Whitepaper – GuardianKey Auth Bastion"
author: "GuardianKey"
date: "Novembro/2025"
subject: "Whitepaper GuardianKey Auth Bastion"
keywords: [GuardianKey, Auth Bastion, MFA, GovBR, OIDC, SAML, Proxy, Segurança, Legado]
subtitle: "Bastião de autenticação para sistemas legados e modernos"
...

# Whitepaper – GuardianKey Auth Bastion

## Resumo

O GuardianKey Auth Bastion é uma solução inovadora que funciona como um proxy de autenticação inteligente, permitindo adicionar autenticação multifator (MFA), integração com GovBR (OAuth2), OIDC/SAML e políticas adaptativas de acesso em sistemas legados sem a necessidade de alterar código-fonte. Com isso, organizações conseguem elevar drasticamente o nível de segurança de seus sistemas críticos de forma rápida, econômica e sem riscos de compatibilidade.


## Desafios

Muitas organizações ainda operam aplicações legadas que são críticas para o negócio — ERPs, sistemas de gestão, plataformas educacionais, softwares hospitalares ou aplicações web proprietárias — mas que não foram projetadas para suportar MFA, OAuth2/OIDC ou integração com provedores de identidade modernos.

Atualizar esses sistemas pode ser caro, demorado e arriscado, além de muitas vezes inviável, seja por falta de suporte do fornecedor, seja por riscos de impacto na operação. Resultado: aplicações estratégicas continuam expostas a acessos indevidos, fraudes e exigências regulatórias não atendidas.

## Nossa Solução

O GuardianKey Auth Bastion se posiciona entre o usuário e o sistema protegido, atuando como um bastião de autenticação:

- Intercepta as requisições de login antes de chegar à aplicação original.
- Aplica políticas de autenticação e autorização adicionais, como MFA, SSO, georrestrição ou integração com GovBR.
- Após validar a identidade, injeta a sessão no sistema legado, permitindo que o usuário acesse sem perceber mudanças no fluxo original.

Assim, aplicações que nunca foram projetadas para suportar MFA ou OIDC passam a oferecer esses recursos de forma transparente e centralizada.


## Benefícios

- Proteção imediata de sistemas legados, sem alteração de código.
- Adoção rápida de MFA, OIDC, OAuth2 e GovBR em aplicações críticas.
- Centralização das políticas de autenticação e autorização.
- Flexibilidade de implantação em ambientes on-premise, cloud ou híbridos.
- Atendimento a normas e regulações (LGPD, ISO, PCI-DSS, autenticação forte).
- Menor custo e tempo de implementação em comparação a projetos de reescrita de software.
- Autenticação em dois fatores (2FA) integrada, com suporte a TOTP (RFC6238), tokens por e-mail/SMS e outras opções configuráveis.
- Painel de administração intuitivo, com dashboards, gerenciamento de usuários, políticas de acesso e auditoria completa.

## Casos de Uso

- **Órgãos públicos**: integração transparente com GovBR sem modificar os sistemas existentes. Adoção de segundo fator de autenticação (MFA) sem alteração de código.
- **Hospitais e universidades**: aplicação de MFA em sistemas antigos sem suporte nativo.
- **Empresas privadas**: unificação da autenticação via OIDC/SAML em aplicações diversas.
- **Cloud e SaaS**: camada adicional de segurança para portais de clientes e parceiros.
- **Ideal para sistemas legados, portais públicos e aplicações modernas.**

## Conclusão

O GuardianKey Auth Bastion é a ponte entre o legado e o futuro da autenticação. Com ele, qualquer aplicação pode se beneficiar de MFA, integração com GovBR, OIDC e autenticação adaptativa — sem mudanças no código, sem riscos de compatibilidade e com implementação rápida.

---

**Saiba mais:**  
[guardiankey.io/pt-br/](https://guardiankey.io/pt-br/)  
[guardiankey.io/docs/pt-br/](https://guardiankey.io/docs/pt-br/)
