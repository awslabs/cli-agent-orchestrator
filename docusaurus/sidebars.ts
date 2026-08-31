import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

const sidebars: SidebarsConfig = {
  docsSidebar: [
    'intro',
    {
      type: 'category',
      label: 'Getting Started',
      items: ['getting-started/installation', 'getting-started/quick-start'],
    },
    {
      type: 'category',
      label: 'Core Concepts',
      items: [
        'core-concepts/architecture',
        'core-concepts/sessions',
        'core-concepts/orchestration-patterns',
      ],
    },
    {
      type: 'category',
      label: 'Orchestration Patterns',
      items: [
        'patterns/handoff',
        'patterns/assign',
        'patterns/send-message',
      ],
    },
    {
      type: 'category',
      label: 'Features',
      items: [
        'features/multi-provider',
        'features/mcp-server',
        'features/profiles',
        'features/scheduled-flows',
        'features/web-ui',
      ],
    },
    {
      type: 'category',
      label: 'Guides',
      items: [
        'guides/building-with-claude-code',
        'guides/building-with-kiro',
      ],
    },
    {
      type: 'category',
      label: 'Reference',
      items: [
        'reference/cli-commands',
        'reference/configuration',
        'reference/environment-variables',
      ],
    },
  ],
};

export default sidebars;
