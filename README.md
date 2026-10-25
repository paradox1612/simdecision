# simdecision

**A cheap decision model drives Android, iOS and TV simulators; code checks the result.**

Your coding agent (Claude Code, Codex, ...) writes a test scenario in plain sentences. simdecision runs it on an emulator or
simulator. A small, typed decision model picks *which* on-screen element to act on, code does the *how* (taps, D-pad
steering, typing), and code-first oracles decide pass or fail. The agent gets back one compact verdict instead of
paying frontier-model prices for every tap.

```yaml
scenario: settings-about
platforms: [ios, phone]
steps:
  - {id: general, goal: "open General", only: [ios]}
  - open the About screen
  - expect:
      - app_foreground: true
      - {judge: "The screen shows information about this device, such as its name, model or software version"}
```

```
$ simdecision run examples/scenarios/settings-about.yaml --platform ios
PASS settings-about [ios] 4/4 steps 16s openai 3421tok $0.00034
```

> Status: **early (0.1)**. It grew out of the test harness of a production streaming app and works there daily on Android
> phone, Android TV, iPhone and Apple TV. The public examples and numbers below are small; read *Limits* before relying on it.

## Why it is different

| | Typical agent tool servers | simdecision |
|---|---|---|
| Who decides each step | the calling frontier agent (every tap is a big-model call) | a typed decision model inside the loop, a fraction of a cent per run |
| Pass / fail | usually the model says "done" | code oracles first (`ui_has`, logs, the app's own network calls, app-in-front); a model judge only for what code cannot see |
| TV apps | rarely; touch-only | D-pad focus steering on Android TV and tvOS: the model picks the target, code steers focus to it |
| Repeat runs | re-plan every time | passing routes are cached, and a **screen graph** reuses navigation across *different* goals |
| Safety | act and hope | before every tap: the screen is still the one the decision was made on, nothing covers the target; refused actions are never retried; 3 actions with no change stop the run |

Ideas borrowed with thanks from [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast) (one request per
decision, code-owned element table, event waits instead of sleeps, validate before acting). The closest published work is
*Jev-Mobile* ([arXiv 2609.30186](https://arxiv.org/abs/2609.30186)), which uses the same split on Android.

## How it works

```
scenario YAML ──> per step:  observe (accessibility tree -> compact text + indexed options)
                             decide  (ONE decision-model request: next option + "is the goal done?")
                             guard   (fresh tree: same screen? target covered? else refuse, re-observe)
                             act     (tap / D-pad steer / type / scroll / back) -> wait for the screen to change
              ──> oracles (code first) ──> one-line verdict + report.md with screenshots and the path taken
```

- **Decision models:** OpenAI Decisions or Cloudflare Workers AI ("Clef"). Both are multimodal; a screenshot is attached
  only when the text tree cannot place the goal (or the screen has no tree, e.g. a video player). With both configured, a
  failed call (quota, budget, HTTP error) retries on the other provider.
- **Drivers:** Android emulators through `adb` and a small persistent on-device snapshot helper
  (`simdecision/android_snap/`, built from source on first use, falls back to `uiautomator dump`); iOS and tvOS simulators through a small XCUITest bridge
  (`simdecision/bridge/`) that drives an *installed* app by bundle id: no changes to the app under test.
- **Memory:** routes per scenario step, and a per-platform screen graph (nodes = fuzzy screen signatures, edges = verified
  taps; shared chrome such as a tab bar is inferred and verified by arrival).

## Quick start

Requirements: macOS, Python 3.10+, a running Android emulator and/or iOS simulator. For iOS/tvOS: Xcode and
`brew install xcodegen`. A decision-model key (see `.env.example`).

```bash
pip install -e .                 # or: uv pip install -e .
cp .env.example .env             # add OPENAI_API_KEY (or the Cloudflare pair)
simdecision prepare phone        # checks the emulator and the app
simdecision prepare ios          # also builds the XCUITest bridge (~1 min, once)
simdecision run examples/scenarios/settings-display.yaml
```

With no `simdecision.yaml`, every platform drives its own **Settings app**, so the examples need nothing installed.
To test your app, add a `simdecision.yaml` (see [`examples/simdecision.yaml`](examples/simdecision.yaml)):

```yaml
app:
  phone: {package: com.example.app, activity: com.example.app.MainActivity}
  ios:   {bundle: com.example.app, process: Example}
```

### From a coding agent (MCP)

```bash
pip install -e '.[mcp]'
claude mcp add simdecision -- simdecision mcp     # run from the folder holding simdecision.yaml
```

Tools: `schema` (read first: the scenario format), `validate`, `run`, `prepare`, `report`, `list_scenarios`, `status`.

## Scenario format, briefly

- **Plain sentences** are dynamic goals; the done-condition is inferred, or set it with `until_ui_has:` / `until:`.
- **Verbs** are deterministic and free: `- tap: Settings`, `- key: [down, down, select]`, `- type: $query`, `- swipe: up`,
  `- back`, `- wait: 2`, `- deeplink: ...`, and more (`simdecision` validates per platform).
- **Inputs** are the only text the runtime may type (decision models never invent text). Secrets come from the
  environment (`inputs: {pw: {env: APP_PASSWORD}}`) and are masked everywhere.
- **Oracles:** `ui_has`, `ui_lacks`, `app_foreground`, `logcat_has`/`logcat_lacks`, `stable`, `requests_has`/`requests_lacks`
  (network evidence from fixtures), and `judge` (a yes/no question to the model, optionally with screenshots).
- **Fixtures:** local mock servers the app talks to (`kind: http` built in, or `kind: command` for your own server), with a
  request log for the network oracles.
- **Verdicts:** PASS, FAIL (an expectation is false), BLOCKED (could not reach the place: bug or harness limit, with the
  path it tried), ERROR (harness problem).

The full guide is what the MCP `schema` tool returns (`simdecision/scenario.py`).

## Measured so far

Examples on stock Settings apps, OpenAI Decisions (`gpt-6-luna`, $0.10 per million input tokens), fresh runs (no cached
routes), one run each, 2026-10-25:

| Scenario | Platform | Result | Time | Model tokens | Cost |
|---|---|---|---|---|---|
| settings-display | Android phone | PASS | 5 s | 1,491 | $0.00015 |
| settings-search (type + open result) | Android phone | PASS | 10 s | 3,210 | $0.00032 |
| settings-about | Android phone | PASS | 6 s | 2,908 | $0.00029 |
| settings-about | iPhone (iOS 26 sim) | PASS | 16 s | 3,421 | $0.00034 |

The Android snapshot helper is most of that: with `uiautomator dump` instead (`SIMDECISION_FAST_SNAPSHOT=0`) the same
phone runs took 28 s, 86 s and BLOCKED (the About screen never goes idle, so `uiautomator` gives up after 124 s). A screen
read went from about 2.2 s to 0.02–0.05 s.

On the production app it came from (3 navigations per run): Android phone 94 s → 13 s and iPhone 30 s → about 18 s; the
screen graph cut model tokens 88% when different goals reused known screens.

These are **not** comparable to AndroidWorld numbers (harder, open-ended tasks); an AndroidWorld run is on the roadmap.

## Limits (read these)

- **Android allows one UiAutomation client per device.** While the snapshot helper runs, other tools' `uiautomator dump`
  on that emulator fails; the helper stops at the end of every run and after 60 s without use. Building it needs the
  Android SDK (`platforms/*/android.jar`, `build-tools/*/d8`) and a JDK.
- **Scripted goals, not open-ended tasks.** The authoring agent breaks intent into steps; there is no runtime planner yet,
  so "do this multi-app chore" style tasks are out of scope for now.
- The covered-target check exists on Android only and has been verified on synthetic trees, not yet on a live overlay.
- iOS/tvOS need the bridge build and Xcode; physical devices are not driven (by design: simulators and emulators only).
- Screenshots in reports are **not** redacted; masked secrets are masked in text only.
- macOS only so far. One Android emulator at a time is the tested setup.

## Development

```bash
python -m unittest discover -s tests      # offline: no device, no model (39 tests)
```

Device runs are manual: `simdecision run ...` against a running emulator/simulator.

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
