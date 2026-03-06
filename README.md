# Video Duperz

Video Duperz is a Windows-first PySide6 app for finding perceptual duplicate videos.
Packaging backend: Hatchling (`hatchling.build`).
Workflow frontend: uv.

## Quick start

```powershell
uv sync --extra dev
uv run python -m video_duperz gui
```

## Build

```powershell
uv build
```

## CLI

```powershell
python -m video_duperz scan --roots D:\Videos E:\Archive --profile balanced
python -m video_duperz export --scan-id 1 --out C:\temp\dup-report
python -m video_duperz clean --full-reset
python -m video_duperz clean --full-reset --delay-ms 1500 --relaunch
```

`File > Full reset` in the GUI closes the app, runs the companion cleaner command, and relaunches after cleanup.

## Quality checks

```powershell
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src tests
uv run pytest -q
```

For packaging and release notes, see `docs/dev-packaging.md`.

---

<!-- legal-disclaimer:start -->
## Legal Disclaimer

THIS SOFTWARE IS PROVIDED "AS IS" AND "AS AVAILABLE," WITHOUT WARRANTIES OF ANY KIND, WHETHER EXPRESS, IMPLIED, STATUTORY, OR OTHERWISE, INCLUDING, WITHOUT LIMITATION, ANY IMPLIED WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, TITLE, NON-INFRINGEMENT, ACCURACY, OR QUIET ENJOYMENT. TO THE MAXIMUM EXTENT PERMITTED BY APPLICABLE LAW, THE AUTHORS, CONTRIBUTORS, MAINTAINERS, DISTRIBUTORS, AND AFFILIATED PARTIES SHALL NOT BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, CONSEQUENTIAL, EXEMPLARY, OR PUNITIVE DAMAGES, OR FOR ANY LOSS OF DATA, PROFITS, GOODWILL, BUSINESS OPPORTUNITY, OR SERVICE INTERRUPTION, ARISING OUT OF OR RELATING TO THE USE OF, OR INABILITY TO USE, THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGES. THIS SOFTWARE HAS BEEN DEVELOPED, IN WHOLE OR IN PART, BY "INTELLIGENT TOOLS"; ACCORDINGLY, OUTPUTS MAY CONTAIN ERRORS OR OMISSIONS, AND YOU ASSUME FULL RESPONSIBILITY FOR INDEPENDENT VALIDATION, TESTING, LEGAL COMPLIANCE, AND SAFE OPERATION PRIOR TO ANY RELIANCE OR DEPLOYMENT.
<!-- legal-disclaimer:end -->
