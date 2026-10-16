# herdr-undo-close

iTerm2-style undo close for [herdr](https://herdr.dev). Closing a pane doesn't
kill it: the pane, with its processes and scrollback, moves into a holding
workspace (`🗑 closed`, last in the sidebar). Undo moves the same live pane
back to its old spot. After a grace period (15s by default) it is closed for
real.

| key | action | does |
| --- | --- | --- |
| `prefix+x` | `luchayward.undo-close.close` | hold the focused pane (a real close if it is already in `🗑 closed`) |
| `prefix+shift+x` | `luchayward.undo-close.close-tab` | hold the whole tab, layout included |
| `prefix+u` | `luchayward.undo-close.undo` | put back the most recent held pane or tab; press again to go further back |

## Install

```sh
git clone <repo-url> herdr-undo-close
herdr plugin link ./herdr-undo-close
```

Then add the bindings to your herdr `config.toml`, unbinding herdr's own
`close_pane` / `close_tab` so they don't clash:

```toml
[keys]
close_pane = ""
close_tab = ""

[[keys.command]]
key = "prefix+x"
type = "plugin_action"
command = "luchayward.undo-close.close"
description = "close pane (undo: prefix+u)"

[[keys.command]]
key = "prefix+shift+x"
type = "plugin_action"
command = "luchayward.undo-close.close-tab"
description = "close tab (undo: prefix+u)"

[[keys.command]]
key = "prefix+u"
type = "plugin_action"
command = "luchayward.undo-close.undo"
description = "undo close pane/tab"
```

and run `herdr server reload-config`.

Needs `python3` (3.7+) on the herdr server's `PATH`. There is no build step.

## Configure

`herdr plugin config-dir luchayward.undo-close` prints a directory. To change the
grace period, put a file named `config` there:

```
grace_seconds = 30
```

`HERDR_UNDO_CLOSE_GRACE` in the server's environment overrides it.

## How it works

- **Close** records the pane's place: workspace and tab (id, label, position),
  the parent split's direction and ratio, which side the pane was on, and the
  panes in the sibling subtree. Then it runs `pane.move` to send the pane into
  its own `⏳ <tab>` tab in the holding workspace. A held pane gets a new pane
  id, but its process keeps running.
- **Undo** puts the pane back:
  - If the sibling subtree is still intact, the restore is exact. The plugin
    stages the subtree in a scratch `↺` tab, splits its anchor pane, and
    rebuilds the subtree with the recorded ratios. Panes in the same workspace
    keep their ids and processes.
  - If the subtree changed, the plugin splits the nearest recorded neighbour
    that still exists, using the same direction and ratio.
  - If no recorded neighbour is left, it splits the tab's focused pane.
  - If the tab is gone, it recreates the tab with its label at its old
    position. If the workspace is gone, it recreates that too.
  - Tabs are rebuilt top-down with `pane move --split` and their ratios are set
    again. `layout.apply` is never used, because it kills panes.
- **Expiry**: a detached (`setsid`) timer process survives the action that
  started it, sleeps until the next deadline, closes expired held panes, and
  exits once nothing is held. A lock keeps it to one process per session. Every
  action also expires overdue entries, in case the timer died. herdr closes
  the holding workspace by itself once it is empty.
- **Server restart**: held processes die with the server. The `[[startup]]`
  hook (and any later action) finds that each held pane's shell PID no longer
  matches, closes the respawned panes, and clears the stack.
- **State** is stored as `stack-<socket hash>.json` in `HERDR_PLUGIN_STATE_DIR`,
  one file per herdr session, plus `undo-close.log`. `python3 undo_close.py
  status` dumps it.

## Limitations

- The holding workspace shows in the sidebar, and held agents still appear in
  agent lists and can still send notifications. herdr 0.9.3 can't hide a
  workspace; the plugin keeps it last in the sidebar and never focuses it.
- Restored panes get new pane ids (herdr assigns one on every cross-workspace
  move). Live agent names follow the pane.
- Undoing into a split whose sibling is a subtree briefly moves those sibling
  panes through a scratch tab. Their processes and ids are unaffected, but you
  may see a flicker.
- Zoom isn't restored: a zoomed tab is unzoomed to close or restore a pane.
- An expired pane can't be brought back. Undo then shows a "Nothing to undo"
  notification.
- `plugin action invoke` from the CLI acts on the focused pane of the attached
  client, the same as a key press. For scripted tests, run
  `python3 undo_close.py close --pane <id>` against a test session.
