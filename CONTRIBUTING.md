# Contributing Guidelines

Thank you for your interest in contributing to our project. Whether it's a bug report, new feature, correction, or additional
documentation, we greatly value feedback and contributions from our community.

Please read through this document before submitting any issues or pull requests to ensure we have all the necessary
information to effectively respond to your bug report or contribution.


## Reporting Bugs/Feature Requests

We welcome you to use the GitHub issue tracker to report bugs or suggest features.

When filing an issue, please check existing open, or recently closed, issues to make sure somebody else hasn't already
reported the issue. Please try to include as much information as you can. Details like these are incredibly useful:

* A reproducible test case or series of steps
* The version of our code being used
* Any modifications you've made relevant to the bug
* Anything unusual about your environment or deployment


## Contributing via Pull Requests
Contributions via pull requests are much appreciated. Before sending us a pull request, please ensure that:

1. You are working against the latest source on the *main* branch.
2. You check existing open, and recently merged, pull requests to make sure someone else hasn't addressed the problem already.
3. You open an issue to discuss any significant work - we would hate for your time to be wasted.

To send us a pull request, please:

1. Fork the repository.
2. Modify the source; please focus on the specific change you are contributing. If you also reformat all the code, it will be hard for us to focus on your change.
3. Ensure local tests pass.
4. Commit to your fork using clear commit messages.
5. Send us a pull request, answering any default questions in the pull request interface.
6. Pay attention to any automated CI failures reported in the pull request, and stay involved in the conversation.

GitHub provides additional document on [forking a repository](https://help.github.com/articles/fork-a-repo/) and
[creating a pull request](https://help.github.com/articles/creating-a-pull-request/).


## Developing in GitHub Codespaces

The project runs end-to-end inside a Codespace. See [docs/codespaces.md](docs/codespaces.md)
for how to start `cao-server`, forward port `9889`, and troubleshoot 404s on the
forwarded URL.


## Recording test fixtures safely

Many provider tests use fixtures captured from **live CLI output** (see
`test/providers/fixtures/`). That output can embed personal or sensitive data —
notably login/banner lines that print your account email, and tokens that hide
inside ANSI escape sequences where they are easy to miss in review. A real
incident (#436) merged personal emails this way.

When recording or updating a live-output fixture:

- **Capture on a synthetic/throwaway account** where possible, not your personal
  or corporate account.
- **Scrub any login/banner/identity line before committing** — replace a real
  email with `user@example.com`, an account name with a placeholder, etc.
- **Skim the raw bytes, not just the rendered view.** A secret can sit next to
  ANSI escape codes and not be visible in a normal terminal render or diff.
- Prefer the reserved placeholders scanners already treat as safe:
  `user@example.com`, and for AWS the documented `AKIAIOSFODNN7EXAMPLE`.

A gitleaks secret scan runs on every PR and weekly over full history (see
[SECURITY.md](SECURITY.md#secret-scanning)); you can run it locally with
`scripts/security-scan.sh gitleaks`, or enable the optional pre-commit hook
([`.pre-commit-config.yaml`](.pre-commit-config.yaml)) with `pre-commit install`
to catch secrets before they enter a commit. If a secret ever lands, follow the
[Leak Response & Git-History Scrub Runbook](docs/security.md)
— **rotate/revoke first**, rewrite history only if warranted by the runbook's
severity triage table.


## Finding contributions to work on
Looking at the existing issues is a great way to find something to contribute on. As our projects, by default, use the default GitHub issue labels (enhancement/bug/duplicate/help wanted/invalid/question/wontfix), looking at any 'help wanted' issues is a great place to start.


## Code of Conduct
This project has adopted the [Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct).
For more information see the [Code of Conduct FAQ](https://aws.github.io/code-of-conduct-faq) or contact
opensource-codeofconduct@amazon.com with any additional questions or comments.


## Security issue notifications
If you discover a potential security issue in this project we ask that you notify AWS/Amazon Security via our [vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/). Please do **not** create a public github issue.


## Licensing

See the [LICENSE](LICENSE) file for our project's licensing. We will ask you to confirm the licensing of your contribution.
