import React from 'react';
import Link from '@docusaurus/Link';
import useBaseUrl from '@docusaurus/useBaseUrl';
import Layout from '@theme/Layout';
import ThemedImage from '@theme/ThemedImage';
import styles from './index.module.css';

const providers = [
  {name: 'Claude Code', color: '#8b5cf6'},
  {name: 'Kiro CLI', color: '#f59e0b'},
  {name: 'Codex', color: '#10b981'},
  {name: 'Kimi CLI', color: '#ec4899'},
  {name: 'Copilot CLI', color: '#3b82f6'},
  {name: 'Cursor CLI', color: '#6366f1'},
  {name: 'OpenCode CLI', color: '#f97316'},
  {name: 'Hermes', color: '#14b8a6'},
  {name: 'Antigravity CLI', color: '#06b6d4'},
];

const features = [
  {
    title: 'Multi-Agent Orchestration',
    emoji: '🎯',
    description:
      'Coordinate multiple AI agents through Handoff, Assign, and Send Message patterns. Each agent runs in its own isolated tmux window.',
    gradient: 'linear-gradient(135deg, #e0f2fe, #bae6fd)',
  },
  {
    title: 'Provider Agnostic',
    emoji: '🔌',
    description:
      'Works with 9 providers including Claude Code, Kiro CLI, Codex, Copilot CLI, and Cursor CLI. Mix providers freely in one session.',
    gradient: 'linear-gradient(135deg, #fae8ff, #e9d5ff)',
  },
  {
    title: 'MCP Native',
    emoji: '🔗',
    description:
      'Built-in MCP server exposes orchestration to any MCP-compatible client. Use CAO as your agentic backbone.',
    gradient: 'linear-gradient(135deg, #dcfce7, #bbf7d0)',
  },
  {
    title: 'Scheduled Flows',
    emoji: '⏱️',
    description:
      'Define cron-like automated workflows that run multi-agent pipelines on a schedule. Perfect for CI/CD and monitoring.',
    gradient: 'linear-gradient(135deg, #fef3c7, #fde68a)',
  },
  {
    title: 'Web Dashboard',
    emoji: '📊',
    description:
      'Monitor all agent sessions from a real-time browser dashboard. View output, assign tasks, and track progress.',
    gradient: 'linear-gradient(135deg, #ede9fe, #ddd6fe)',
  },
  {
    title: 'Zero Infrastructure',
    emoji: '💻',
    description:
      'Runs entirely on your local machine with no cloud dependencies. Install with one command and start orchestrating.',
    gradient: 'linear-gradient(135deg, #ccfbf1, #99f6e4)',
  },
];

function HeroSection() {
  return (
    <header className={styles.hero}>
      <div className={styles.heroDecoration}>
        <div className={styles.blob1} />
        <div className={styles.blob2} />
        <div className={styles.blob3} />
      </div>
      <div className={`container ${styles.heroContainer}`}>
        <div className={styles.badge}>Open Source Multi-Agent Framework</div>
        {/* The lockup carries the project name, so the alt text is the
            heading's accessible name rather than a description of the image.
            Two files because the hero background flips to black in dark mode
            and the lockup's navy would disappear against it. */}
        <h1 className={styles.heroLogo}>
          <ThemedImage
            className={styles.heroLockup}
            alt="CLI Agent Orchestrator"
            sources={{
              light: useBaseUrl('/img/cao-lockup.png'),
              dark: useBaseUrl('/img/cao-lockup-dark.png'),
            }}
            width={1800}
            height={480}
          />
        </h1>
        <p className={styles.heroTagline}>
          Lightweight orchestration for multi-agent AI workflows
        </p>
        <div className={styles.heroButtons}>
          <Link className={styles.btnPrimary} to="/docs/intro">
            Get Started →
          </Link>
          <a
            className={styles.btnSecondary}
            href={useBaseUrl('/course/index.html')}>
            Interactive Course
          </a>
          <Link
            className={styles.btnSecondary}
            to="https://github.com/awslabs/cli-agent-orchestrator">
            GitHub
          </Link>
        </div>
        <div className={styles.providers}>
          {providers.map((p, i) => (
            <span
              key={i}
              className={styles.providerPill}
              style={{'--pill-color': p.color} as React.CSSProperties}>
              <span className={styles.providerDot} />
              {p.name}
            </span>
          ))}
        </div>
      </div>
    </header>
  );
}

function TerminalSection() {
  return (
    <section className={styles.terminalSection}>
      <div className="container">
        <div className={styles.terminalWrapper}>
          <div className={styles.terminal}>
            <div className={styles.terminalBar}>
              <span className={styles.dot} data-c="red" />
              <span className={styles.dot} data-c="yellow" />
              <span className={styles.dot} data-c="green" />
              <span className={styles.terminalLabel}>cao — terminal</span>
            </div>
            <pre className={styles.terminalCode}>
<code><span className={styles.prompt}>$</span> <span className={styles.cmd}>cao launch</span> <span className={styles.flag}>--agents</span> <span className={styles.val}>code_supervisor</span> <span className={styles.flag}>--session-name</span> <span className={styles.val}>main</span>{'\n'}<span className={styles.prompt}>$</span> <span className={styles.cmd}>cao session send</span> <span className={styles.val}>main</span> <span className={styles.str}>"Implement auth module"</span>{'\n'}<span className={styles.prompt}>$</span> <span className={styles.cmd}>cao session status</span> <span className={styles.val}>main</span> <span className={styles.flag}>--workers</span>{'\n'}<span className={styles.success}>✓ 3 terminals active</span></code>
            </pre>
          </div>
        </div>
      </div>
    </section>
  );
}

function FeaturesSection() {
  return (
    <section className={styles.features}>
      <div className="container">
        <h2 className={styles.sectionHeading}>
          Everything you need to orchestrate AI agents
        </h2>
        <p className={styles.sectionSub}>
          A batteries-included framework for multi-agent coordination.
        </p>
        <div className={styles.featuresGrid}>
          {features.map((f, i) => (
            <div
              key={i}
              className={styles.featureCard}
              style={{'--card-bg': f.gradient} as React.CSSProperties}>
              <div className={styles.featureEmoji}>{f.emoji}</div>
              <h3 className={styles.featureTitle}>{f.title}</h3>
              <p className={styles.featureDesc}>{f.description}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

export default function Home(): React.JSX.Element {
  return (
    <Layout description="Lightweight orchestration for multi-agent AI workflows">
      <HeroSection />
      <TerminalSection />
      <main>
        <FeaturesSection />
      </main>
    </Layout>
  );
}
