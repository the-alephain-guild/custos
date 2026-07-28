// @ts-check
// custos docs site — Docusaurus 3.x config
// Authoritative plan: .forge/plans/2026-07/20-custos-docs-site-scaffold.md

const {themes: prismThemes} = require('prism-react-renderer');

/** @type {import('@docusaurus/types').Config} */
const config = {
  title: 'custos',
  tagline: 'the non-custodial execution runner',
  favicon: 'img/favicon.svg',

  url: 'https://custos.alephain.com',
  baseUrl: '/',

  organizationName: 'the-alephain-guild',
  projectName: 'custos',
  trailingSlash: false,

  onBrokenLinks: 'throw',
  markdown: {hooks: {onBrokenMarkdownLinks: 'throw'}},

  i18n: {
    defaultLocale: 'en',
    locales: ['en', 'zh-Hans'],
    localeConfigs: {
      en: {label: 'English', htmlLang: 'en'},
      'zh-Hans': {label: '简体中文', htmlLang: 'zh-Hans'},
    },
  },

  presets: [
    [
      'classic',
      /** @type {import('@docusaurus/preset-classic').Options} */
      ({
        docs: {
          routeBasePath: '/',
          sidebarPath: require.resolve('./sidebars.js'),
          editUrl:
            'https://github.com/the-alephain-guild/custos/tree/main/docs-site/',
          showLastUpdateAuthor: true,
          showLastUpdateTime: true,
        },
        blog: false,
        theme: {
          customCss: require.resolve('./src/css/custom.css'),
        },
      }),
    ],
  ],

  themeConfig:
    /** @type {import('@docusaurus/preset-classic').ThemeConfig} */
    ({
      image: 'img/og-card.png',
      colorMode: {
        defaultMode: 'light',
        disableSwitch: false,
        respectPrefersColorScheme: true,
      },
      navbar: {
        title: 'custos',
        logo: {
          alt: 'custos',
          src: 'img/favicon.svg',
        },
        items: [
          {
            type: 'docSidebar',
            sidebarId: 'main',
            position: 'left',
            label: 'Docs',
          },
          {
            href: 'https://github.com/the-alephain-guild/custos',
            label: 'GitHub',
            position: 'right',
          },
          {
            type: 'localeDropdown',
            position: 'right',
          },
          // {type: 'docsVersionDropdown', position: 'right'},  // T11: enable after first `docs:version 0.3.0`
        ],
      },
      footer: {
        style: 'dark',
        links: [
          {
            title: 'Documentation',
            items: [
              {label: 'What is custos', to: '/introduction/what-is-custos'},
              {label: 'Getting started', to: '/getting-started/installation'},
              {label: 'Trust model', to: '/trust-model/red-lines'},
              {label: 'Integration guide', to: '/integration/gateway-contract-v1'},
            ],
          },
          {
            title: 'Ecosystem',
            items: [
              {label: 'The Alephain Guild', href: 'https://alephain.com'},
              {
                label: 'ARX · private beta',
                href: 'mailto:contact@alephain.com?subject=ARX%20private%20beta%20%C2%B7%20invite%20request',
              },
              {label: 'Alchymia Labs', href: 'https://alchymia.alephain.com'},
              {label: 'Tesseract Trading', href: 'https://tesseract.alephain.com'},
            ],
          },
          {
            title: 'Code',
            items: [
              {label: 'GitHub', href: 'https://github.com/the-alephain-guild/custos'},
              {
                label: 'CHANGELOG',
                href: 'https://github.com/the-alephain-guild/custos/blob/main/CHANGELOG.md',
              },
              {
                label: 'License · Apache-2.0',
                href: 'https://github.com/the-alephain-guild/custos/blob/main/LICENSE',
              },
              {
                label: 'Security policy',
                href: 'https://github.com/the-alephain-guild/custos/blob/main/SECURITY.md',
              },
            ],
          },
        ],
        copyright: `© ${new Date().getFullYear()} custos contributors · Apache-2.0 · Part of The Alephain Guild ecosystem.`,
      },
      prism: {
        theme: prismThemes.github,
        darkTheme: prismThemes.dracula,
        additionalLanguages: ['bash', 'json', 'yaml', 'python', 'rust', 'toml', 'diff'],
      },
    }),
};

module.exports = config;
