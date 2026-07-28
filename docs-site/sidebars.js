// @ts-check
// custos docs sidebar — 46 chapters across 10 Parts
// Content: Plan 20 T5 migrates from docs/**.md; T6 translates to zh-Hans.

/** @type {import('@docusaurus/plugin-content-docs').SidebarsConfig} */
const sidebars = {
  main: [
    {
      type: 'category',
      label: 'I · Overview',
      collapsed: false,
      collapsible: true,
      items: [
        'introduction/what-is-custos',
        'introduction/trust-model',
        'introduction/architecture-at-a-glance',
      ],
    },
    {
      type: 'category',
      label: 'II · Getting Started',
      items: [
        'getting-started/installation',
        'getting-started/enrollment',
        'getting-started/first-sandbox-run',
        'getting-started/first-deployment-spec',
      ],
    },
    {
      type: 'category',
      label: 'III · Core Concepts',
      items: [
        'concepts/deployment-spec-vs-instance',
        'concepts/trading-modes',
        'concepts/live-execution-gate',
        'concepts/reconcile-loop',
        'concepts/runner-fact',
      ],
    },
    {
      type: 'category',
      label: 'IV · Operator Guide',
      items: [
        'operator-guide/deployment',
        'operator-guide/credential-vault',
        'operator-guide/readiness-health',
        'operator-guide/runtime-log-observability',
        'operator-guide/emergency-playbook',
        'operator-guide/troubleshooting',
      ],
    },
    {
      type: 'category',
      label: 'V · Non-Custodial Trust Model',
      items: [
        'trust-model/red-lines',
        'trust-model/rl1-key-kek-never-leaves',
        'trust-model/rl2-live-execution-gate',
        'trust-model/rl3-reconcile-disconnect',
        'trust-model/rl4-decimal-money-math',
        'trust-model/signed-release-chain',
        'trust-model/audit-checklist',
      ],
    },
    {
      type: 'category',
      label: 'VI · Integration Guide',
      items: [
        'integration/gateway-contract-v1',
        'integration/signing-deployment-spec',
        'integration/consuming-runner-fact',
        'integration/contract-versioning',
        'integration/reference-implementations',
      ],
    },
    {
      type: 'category',
      label: 'VII · Engines',
      items: [
        'engines/nautilus-trader',
        'engines/noop',
        'engines/engine-roadmap',
      ],
    },
    {
      type: 'category',
      label: 'VIII · Strategy Toolkit',
      items: [
        'toolkit/overview',
        'toolkit/artifact-signing',
        'toolkit/registry-mode-loading',
      ],
    },
    {
      type: 'category',
      label: 'IX · Reference',
      items: [
        'reference/cli',
        'reference/configuration',
        'reference/json-schema',
        'reference/nats-subjects',
      ],
    },
    {
      type: 'category',
      label: 'X · Release & Governance',
      items: [
        'release-governance/semver-lts',
        'release-governance/upgrade-paths',
        'release-governance/security-policy',
        'release-governance/contributing',
        'release-governance/license',
        'release-governance/changelog',
      ],
    },
  ],
};

module.exports = sidebars;
