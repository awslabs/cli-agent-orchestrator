import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

const config: Config = {
  title: 'CLI Agent Orchestrator',
  tagline: 'Lightweight orchestration for multi-agent AI workflows',
  favicon: 'img/favicon.ico',

  future: {
    v4: true,
  },

  url: 'https://awslabs.github.io',
  baseUrl: '/cli-agent-orchestrator/',

  organizationName: 'awslabs',
  projectName: 'cli-agent-orchestrator',

  onBrokenLinks: 'throw',

  markdown: {
    hooks: {
      onBrokenMarkdownLinks: 'warn',
    },
  },

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  presets: [
    [
      'classic',
      {
        docs: {
          sidebarPath: './sidebars.ts',
          editUrl:
            'https://github.com/awslabs/cli-agent-orchestrator/tree/main/docusaurus/',
        },
        blog: {
          blogTitle: 'CAO Blog',
          blogDescription:
            'Tutorials, deep dives, and community stories about orchestrating multi-agent CLI workflows.',
          blogSidebarTitle: 'Recent posts',
          blogSidebarCount: 'ALL',
          postsPerPage: 10,
          showReadingTime: true,
          editUrl:
            'https://github.com/awslabs/cli-agent-orchestrator/tree/main/docusaurus/',
          feedOptions: {
            type: ['rss', 'atom'],
            title: 'CAO Blog',
            description:
              'Tutorials, deep dives, and community stories about CLI Agent Orchestrator.',
            copyright: `Copyright © ${new Date().getFullYear()} Amazon.com, Inc. or its affiliates.`,
            xslt: true,
          },
          // Contributors must register in blog/authors.yml and pick a tag from
          // blog/tags.yml, so both go through PR review instead of accumulating
          // one-off values. A missing truncate marker dumps a whole post onto
          // the list page, so that fails the build too. See CONTRIBUTING.md.
          onInlineAuthors: 'throw',
          onInlineTags: 'throw',
          onUntruncatedBlogPosts: 'throw',
        },
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    // Default preview image for link unfurls (og:image / twitter:image).
    // Individual pages can override it with `image:` in their front matter.
    image: 'img/cao-social-card.png',
    colorMode: {
      respectPrefersColorScheme: true,
    },
    navbar: {
      title: 'CLI Agent Orchestrator',
      // srcDark is not optional here: dark mode paints the navbar pure black
      // (see the navbar background override in src/css/custom.css), and the
      // mark's navy contrasts against that at only ~2:1. The dark file lifts
      // navy to a light tint; everything else about the two is identical.
      logo: {
        alt: '',
        src: 'img/cao-mark.svg',
        srcDark: 'img/cao-mark-dark.svg',
        width: 32,
        height: 32,
      },
      items: [
        {
          type: 'docSidebar',
          sidebarId: 'docsSidebar',
          position: 'left',
          label: 'Documentation',
        },
        {
          href: 'pathname:///course/index.html',
          label: 'Interactive Course',
          position: 'left',
        },
        {
          to: '/blog',
          label: 'Blog',
          position: 'left',
        },
        {
          href: 'https://github.com/awslabs/cli-agent-orchestrator',
          label: 'GitHub',
          position: 'right',
        },
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Docs',
          items: [
            {
              label: 'Getting Started',
              to: '/docs/intro',
            },
            {
              label: 'Interactive Course',
              href: 'pathname:///course/index.html',
            },
          ],
        },
        {
          title: 'Community',
          items: [
            {
              label: 'Blog',
              to: '/blog',
            },
            {
              label: 'GitHub Discussions',
              href: 'https://github.com/awslabs/cli-agent-orchestrator/discussions',
            },
            {
              label: 'GitHub Issues',
              href: 'https://github.com/awslabs/cli-agent-orchestrator/issues',
            },
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} Amazon.com, Inc. or its affiliates. All Rights Reserved. Licensed under Apache-2.0`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
      additionalLanguages: ['bash', 'python', 'yaml', 'json', 'toml'],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
