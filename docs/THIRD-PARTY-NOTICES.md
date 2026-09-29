# Third-party notices

## Project code

The project is distributed under the MIT license. The original copyright
notice credits Pawel Huryn. Subsequent contributions remain under MIT.
Preserve the complete [project license](https://github.com/mlizaso/claude-usage/blob/main/LICENSE)
when distributing copies or substantial portions of the software.

The project originated from [phuryn/claude-usage](https://github.com/phuryn/claude-usage).
Historical contributor credits remain in the changelog. An import or condensed
history does not transfer authorship of upstream work to the fork maintainer.

## Chart.js

`vendor/chart.umd.js` is a pinned third-party browser runtime. Its MIT copyright
and permission notice is in
[LICENSE.chartjs.md](https://github.com/mlizaso/claude-usage/blob/main/vendor/LICENSE.chartjs.md).
That notice must accompany the asset in Python, Docker, Homebrew and VSIX builds.
The bundled header identifies the version; packaging checks verify its checksum.

## Build tools and platforms

The Python runtime uses the standard library. The extension's locked npm
dependencies are development and packaging tools; the VSIX is built with
`--no-dependencies`. Their individual licenses remain in their distributions.
Python, Node, VS Code and Docker are separately installed platforms with their
own terms. A Docker image also carries its base distribution's notices.

Claude Code, Codex, Anthropic, OpenAI, VS Code and Docker names identify
compatible products. Their owners do not endorse this independent project;
the MIT license does not grant rights to third-party trademarks.
