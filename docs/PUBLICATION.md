# Preparing a public repository

Public distribution includes every reachable commit and tag, not only the
current files. A clean working tree is not evidence that an old screenshot,
credential, email address or private project name has disappeared.

## Source and history

- Review tracked files, commit messages, author/committer metadata and tags.
- Scan the proposed public history for credentials using a dedicated scanner
  such as TruffleHog. Use offline detection when validating suspected secrets
  would send them to an external service. Review false positives privately.
- Inspect images visually and remove EXIF/text metadata. Use synthetic data.
- Exclude local databases, exports, transcripts, credentials, editor state,
  agent scratch and generated packages. Inspect the final archive or VSIX too.
- Keep required copyright/license notices and credit upstream work.
- Use the account's verified GitHub noreply address for new public commits.

History consolidation preserves the intended final source tree while replacing
its commit sequence. A sanitized upstream import must be identified as such;
it must not claim byte-for-byte preservation if private files were removed.
Check every intermediate public commit, because later deletions do not hide it.

Keep the original history in a **private local backup**. Publish only the
reviewed branch with an explicit refspec. Do not use `git push --mirror`,
`--all` or `--tags`: those can copy old private refs into the public repository.

## Existing GitHub repositories

Changing visibility also exposes existing issues, pull requests, release notes
and assets, and Actions history/logs. GitHub explicitly documents the visibility
of Actions logs in its [repository visibility guidance](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/managing-repository-settings/setting-repository-visibility).
Replacing a branch alone does not clean those surfaces or necessarily remove
old commit objects and pull-request refs.

A new repository containing only the reviewed history provides a clean
publication boundary while the existing repository can remain private. If
reusing the existing repository, review those other surfaces and complete
any necessary removal before changing visibility. Rotate a real exposed
credential even if its old commit is later removed.

## Validation and release setup

Run the checks in [Contributing](CONTRIBUTING.md), inspect package contents,
and verify that the prepared branch contains only the intended commits.
Enable private vulnerability reporting, secret scanning and branch protection
as available for the destination. Review the license and project description.
Build new release artifacts from the reviewed tree; old assets may still
contain old documentation or personal paths.

Publishing a GitHub repository is separate from publishing to a package
registry. The VS Code package remains `private: true` to prevent accidental
npm publication. The release workflow can attach a VSIX to GitHub Releases
in a public repository after its validation gates pass.
