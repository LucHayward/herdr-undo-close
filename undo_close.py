#!/usr/bin/env python3
"""herdr-undo-close: iTerm2-style undo for closed panes and tabs.

Closing a pane (or tab) moves it, still running, into a holding workspace.
Undo moves it back to where it was. After a grace period the held pane is
closed for real by a detached timer process.

Runs on the herdr server's python3 and targets 3.7+, so stick to the 3.7
standard library. Talks to herdr over HERDR_SOCKET_PATH (one request per
connection, newline-delimited JSON).

Usage: undo_close.py close|close-tab|undo|expire-daemon|startup|status
       [--pane ID] [--tab ID]   (overrides the invocation context, for tests)
"""

import fcntl
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager

PLUGIN_ID = "luchayward.undo-close"
HOLD_LABEL = "🗑 closed"
STAGING_LABEL = "↺"
DEFAULT_GRACE = 15.0

SOCK = os.environ.get("HERDR_SOCKET_PATH", "")
STATE_DIR = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
    "herdr", "plugins", PLUGIN_ID)
CONFIG_DIR = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or os.path.join(
    os.path.expanduser("~/.config/herdr/plugins/config"), PLUGIN_ID)

# The state dir is shared by every herdr session, so key files by socket.
_SESSION_KEY = hashlib.sha1(SOCK.encode()).hexdigest()[:10]
STATE_FILE = os.path.join(STATE_DIR, "stack-%s.json" % _SESSION_KEY)
LOCK_FILE = os.path.join(STATE_DIR, "stack-%s.lock" % _SESSION_KEY)
TIMER_LOCK = os.path.join(STATE_DIR, "timer-%s.lock" % _SESSION_KEY)
LOG_FILE = os.path.join(STATE_DIR, "undo-close.log")


def log(msg):
    try:
        with open(LOG_FILE, "a") as f:
            f.write("%s [%d] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid(), msg))
    except OSError:
        pass


# ---------------------------------------------------------------- herdr API

class HerdrError(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, "%s: %s" % (code, message))
        self.code = code


def call(method, **params):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(30)
    try:
        s.connect(SOCK)
        req = {"id": "undo-close", "method": method, "params": params}
        s.sendall(json.dumps(req).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    resp = json.loads(buf.decode())
    if "error" in resp:
        err = resp["error"]
        raise HerdrError(err.get("code"), err.get("message"))
    return resp["result"]


def try_call(method, quiet=False, **params):
    try:
        return call(method, **params)
    except HerdrError as e:
        if not quiet:
            log("%s %s failed: %s" % (method, json.dumps(params), e))
        return None


def notify(title, body=None):
    try_call("notification.show", title=title, body=body)


def workspaces():
    return call("workspace.list")["workspaces"]


def workspace(ws_id):
    r = try_call("workspace.get", quiet=True, workspace_id=ws_id) if ws_id else None
    return r and r.get("workspace")


def tabs(ws_id):
    return call("tab.list", workspace_id=ws_id)["tabs"]


def tab_exists(tab_id):
    return bool(tab_id) and try_call("tab.get", quiet=True, tab_id=tab_id) is not None


def panes(ws_id=None):
    params = {"workspace_id": ws_id} if ws_id else {}
    return call("pane.list", **params)["panes"]


def pane(pane_id):
    r = try_call("pane.get", quiet=True, pane_id=pane_id) if pane_id else None
    return r and r.get("pane")


def shell_pid(pane_id):
    r = try_call("pane.process_info", quiet=True, pane_id=pane_id)
    return r and r["process_info"].get("shell_pid")


def export(pane_id):
    return call("layout.export", pane_id=pane_id)["layout"]


def move(pane_id, dest, focus=False):
    """Move a pane; returns the moved pane record (its id may change)."""
    r = call("pane.move", pane_id=pane_id, destination=dest, focus=focus)["move_result"]
    if not r.get("changed"):
        raise HerdrError("move_unchanged", "%s: %s" % (pane_id, r.get("reason")))
    return r["pane"]


def unzoom(pane_id):
    if export(pane_id).get("zoomed"):
        call("pane.zoom", pane_id=pane_id, mode="off")


def nudge(pane_id):
    """Zoom on/off so every pane of the tab gets resized to its new rect.

    herdr does not resize the panes that grow into space freed by a move
    (a native close does). A zoom toggle does, and also focuses pane_id.
    """
    try_call("pane.zoom", pane_id=pane_id, mode="on")
    try_call("pane.zoom", pane_id=pane_id, mode="off")


# ------------------------------------------------------------- layout trees

def leaves(node):
    if node["type"] == "pane":
        return [node["pane_id"]]
    return leaves(node["first"]) + leaves(node["second"])


def find_path(node, pane_id, path=()):
    if node["type"] == "pane":
        return list(path) if node["pane_id"] == pane_id else None
    return (find_path(node["first"], pane_id, path + (False,))
            or find_path(node["second"], pane_id, path + (True,)))


def node_at(node, path):
    for second in path:
        node = node["second"] if second else node["first"]
    return node


def find_node(node, leafset, path=()):
    """Path of the node whose leaves are exactly leafset, or None."""
    if set(leaves(node)) == leafset:
        return list(path)
    if node["type"] == "split":
        return (find_node(node["first"], leafset, path + (False,))
                or find_node(node["second"], leafset, path + (True,)))
    return None


def map_tree(node, idmap):
    if node["type"] == "pane":
        return dict(node, pane_id=idmap.get(node["pane_id"], node["pane_id"]))
    return dict(node, first=map_tree(node["first"], idmap), second=map_tree(node["second"], idmap))


def set_ratios(tab_id, node, path=()):
    if node["type"] != "split":
        return
    try_call("layout.set_split_ratio", tab_id=tab_id, path=list(path), ratio=node["ratio"])
    set_ratios(tab_id, node["first"], path + (False,))
    set_ratios(tab_id, node["second"], path + (True,))


def build(node, tab_id, idmap):
    """Grow `node` top-down from its leftmost leaf, which is already in tab_id.

    Every other leaf is moved in next to it with `pane move --split`. Only
    leaf splits exist, so each split is made while its region is still one
    pane: the moved-in pane (leftmost of `second`) splits the leftmost of
    `first`, and `ratio` is the first child's share.
    """
    if node["type"] != "split":
        return
    target = idmap[leaves(node["first"])[0]]
    moving = leaves(node["second"])[0]
    moved = move(moving, {"type": "tab", "tab_id": tab_id, "target_pane_id": target,
                          "split": node["direction"], "ratio": node["ratio"]})
    idmap[moving] = moved["pane_id"]
    build(node["first"], tab_id, idmap)
    build(node["second"], tab_id, idmap)


def transplant(tree, dest):
    """Move a whole tab tree to a new tab described by `dest`. Returns (tab_id, idmap)."""
    anchor = leaves(tree)[0]
    moved = move(anchor, dest)
    tab_id = moved["tab_id"]
    idmap = {anchor: moved["pane_id"]}
    build(tree, tab_id, idmap)
    set_ratios(tab_id, tree)
    return tab_id, idmap


# -------------------------------------------------------------------- state

@contextmanager
def locked_state():
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LOCK_FILE, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
        except (OSError, ValueError):
            state = {}
        state.setdefault("entries", [])
        state.setdefault("hold_ws", None)
        before = json.dumps(state, sort_keys=True)
        yield state
        if json.dumps(state, sort_keys=True) != before:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f, indent=1, ensure_ascii=False)
            os.replace(tmp, STATE_FILE)


def grace_seconds():
    env = os.environ.get("HERDR_UNDO_CLOSE_GRACE")
    if env:
        return float(env)
    try:
        with open(os.path.join(CONFIG_DIR, "config")) as f:
            for line in f:
                key, sep, value = line.partition("=")
                if sep and key.strip() == "grace_seconds":
                    return float(value.split("#")[0].strip().strip('"'))
    except (OSError, ValueError):
        pass
    return DEFAULT_GRACE


def hold_ws(state):
    """The holding workspace id, if it exists. Trusts the label, not just the id."""
    ws = workspace(state.get("hold_ws"))
    if ws and ws.get("label") == HOLD_LABEL:
        return ws["workspace_id"]
    for ws in workspaces():
        if ws.get("label") == HOLD_LABEL:
            state["hold_ws"] = ws["workspace_id"]
            return ws["workspace_id"]
    state["hold_ws"] = None
    return None


def hold_dest(state, label):
    hw = hold_ws(state)
    if hw:
        return {"type": "new_tab", "workspace_id": hw, "label": label}
    return {"type": "new_workspace", "label": HOLD_LABEL, "tab_label": label}


def park_hold_ws(state):
    """Keep the holding workspace last in the sidebar."""
    hw = hold_ws(state)
    if hw:
        try_call("workspace.move", workspace_id=hw, insert_index=len(workspaces()))


def entry_alive(entry):
    """True if every held pane is still in the holding workspace with its original shell."""
    held = entry["held"]
    for pane_id, pid in held.items():
        p = pane(pane_id)
        if not p or shell_pid(pane_id) != pid:
            return False
    return True


def discard(entry, state, reason):
    """Really close what an entry holds, if it is still ours."""
    hw = hold_ws(state)
    for pane_id, pid in entry["held"].items():
        p = pane(pane_id)
        if p and p["workspace_id"] == hw:
            # After a server restart the pane id survives but the process doesn't:
            # the respawned shell is ours to clean up either way.
            try_call("pane.close", pane_id=pane_id)
    log("discarded %s (%s): %s" % (entry["id"], entry["kind"], reason))


def reconcile(state, now=None):
    """Drop dead entries and expire overdue ones. Returns the number expired."""
    now = now or time.time()
    keep, expired = [], 0
    for entry in state["entries"]:
        if entry["expires_at"] <= now:
            discard(entry, state, "expired")
            expired += 1
        elif not entry_alive(entry):
            discard(entry, state, "held pane gone or restarted")
        else:
            keep.append(entry)
    state["entries"] = keep
    if expired:
        state["last_expired_at"] = now
    return expired


def ensure_timer():
    """Start the detached expiry process unless one is already running."""
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "expire-daemon"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


def cmd_expire_daemon():
    """Sleep until the next deadline, expire, repeat; exit when the stack is empty.

    Plugin actions run inside the herdr server, but a setsid'd child outlives
    the action. The timer lock keeps it a singleton. It is released while the
    state lock is held, so a concurrent close either sees this process still
    running (and its new entry gets picked up) or starts a fresh one.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    timer = open(TIMER_LOCK, "a")
    try:
        fcntl.flock(timer, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return
    failures = 0
    while True:
        try:
            with locked_state() as state:
                reconcile(state)
                deadlines = [e["expires_at"] for e in state["entries"]]
                if not deadlines:
                    timer.close()
                    return
            failures = 0
            time.sleep(min(max(min(deadlines) - time.time(), 0.2), 60))
        except (OSError, HerdrError, ValueError) as e:
            # Server gone (stopped or restarting): retry a while, then leave
            # cleanup to the startup hook.
            failures += 1
            log("timer error %d: %s" % (failures, e))
            if failures > 30:
                timer.close()
                return
            time.sleep(2)


# ---------------------------------------------------------------- context

def context(args):
    ctx = {}
    try:
        ctx = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    except ValueError:
        pass
    pane_id = args.get("--pane") or ctx.get("focused_pane_id") or os.environ.get("HERDR_PANE_ID")
    tab_id = args.get("--tab")
    if not tab_id and pane_id:
        p = pane(pane_id)
        tab_id = p and p["tab_id"]
    return pane_id, tab_id or ctx.get("tab_id")


def ws_index(ws_id):
    ids = [w["workspace_id"] for w in workspaces()]
    return ids.index(ws_id) if ws_id in ids else len(ids)


def tab_info(tab_id):
    p = pane(next(iter_tab_panes(tab_id)))
    ws_id = p["workspace_id"]
    ts = tabs(ws_id)
    ids = [t["tab_id"] for t in ts]
    label = next(t.get("label") for t in ts if t["tab_id"] == tab_id)
    if label == str(ids.index(tab_id) + 1):
        label = None  # unnamed tabs report their position; keep them unnamed
    w = workspace(ws_id)
    return {"workspace_id": ws_id, "workspace_label": w.get("label"),
            "workspace_index": ws_index(ws_id), "tab_id": tab_id,
            "tab_label": label, "tab_index": ids.index(tab_id)}


def iter_tab_panes(tab_id):
    ws_id = tab_id.split(":")[0]
    for p in panes(ws_id):
        if p["tab_id"] == tab_id:
            yield p["pane_id"]


def new_entry(state, kind, info, held):
    now = time.time()
    state["seq"] = state.get("seq", 0) + 1
    entry = dict(info, id=state["seq"], kind=kind, closed_at=now,
                 expires_at=now + grace_seconds(), held=held)
    state["entries"].append(entry)
    return entry


# ------------------------------------------------------------------ close

def cmd_close(args):
    pane_id, tab_id = context(args)
    if not pane_id or not pane(pane_id):
        notify("Undo close", "No focused pane to close")
        return
    with locked_state() as state:
        reconcile(state)
        p = pane(pane_id)
        if p["workspace_id"] == hold_ws(state):
            # Closing inside the holding area is a real close.
            call("pane.close", pane_id=pane_id)
            drop_held(state, [pane_id])
            return
        unzoom(pane_id)
        layout = export(pane_id)
        tree = layout["root"]
        info = tab_info(layout["tab_id"])
        path = find_path(tree, pane_id)
        parent = None
        if path:
            split = node_at(tree, path[:-1])
            sibling = split["first"] if path[-1] else split["second"]
            parent = {"direction": split["direction"], "ratio": split["ratio"],
                      "position": "second" if path[-1] else "first",
                      "sibling_leaves": leaves(sibling)}
        info.update(orig_pane_id=pane_id, parent=parent,
                    tab_pane_count=len(leaves(tree)))
        pid = shell_pid(pane_id)
        held = move(pane_id, hold_dest(state, "⏳ %s" % (info["tab_label"] or "")))
        state["hold_ws"] = held["workspace_id"]
        entry = new_entry(state, "pane", info, {held["pane_id"]: pid})
        if parent:
            left = pane(parent["sibling_leaves"][0])
            focused = left and export(left["pane_id"])["focused_pane_id"]
            if focused:
                nudge(focused)
        park_hold_ws(state)
        log("held pane %s as %s (entry %d)" % (pane_id, held["pane_id"], entry["id"]))
    ensure_timer()


def cmd_close_tab(args):
    pane_id, tab_id = context(args)
    if not tab_id or not tab_exists(tab_id):
        notify("Undo close", "No focused tab to close")
        return
    with locked_state() as state:
        reconcile(state)
        if tab_id.split(":")[0] == hold_ws(state):
            held_panes = list(iter_tab_panes(tab_id))
            call("tab.close", tab_id=tab_id)
            drop_held(state, held_panes)
            return
        first = next(iter_tab_panes(tab_id))
        unzoom(first)
        layout = export(first)
        tree = layout["root"]
        info = tab_info(tab_id)
        order = leaves(tree)
        info.update(focused_index=order.index(layout["focused_pane_id"])
                    if layout["focused_pane_id"] in order else 0)
        pids = {p: shell_pid(p) for p in order}
        held_tab, idmap = transplant(tree, hold_dest(state, "⏳ %s" % (info["tab_label"] or "")))
        state["hold_ws"] = held_tab.split(":")[0]
        held = {idmap[p]: pids[p] for p in order}
        entry = new_entry(state, "tab", info, held)
        entry["held_tab"] = held_tab
        park_hold_ws(state)
        log("held tab %s as %s (entry %d)" % (tab_id, held_tab, entry["id"]))
    ensure_timer()


def drop_held(state, pane_ids):
    """Forget entries whose held panes were really closed by hand."""
    gone = set(pane_ids)
    keep = []
    for entry in state["entries"]:
        for p in gone & set(entry["held"]):
            del entry["held"][p]
        if entry["held"]:
            keep.append(entry)
    state["entries"] = keep


# ------------------------------------------------------------------- undo

def cmd_undo(args):
    with locked_state() as state:
        expired = reconcile(state)
        if not state["entries"]:
            recent = time.time() - state.get("last_expired_at", 0) < 600
            notify("Nothing to undo",
                   "Closed panes are kept for %gs" % grace_seconds() if (expired or recent) else None)
            return
        entry = state["entries"].pop()
        try:
            if entry["kind"] == "tab":
                focus_id = restore_tab(entry)
            else:
                focus_id = restore_pane(entry)
        except HerdrError as e:
            log("undo of entry %d failed: %s" % (entry["id"], e))
            notify("Undo close failed", str(e))
            if entry_alive(entry):
                state["entries"].append(entry)
            return
        if focus_id:
            nudge(focus_id)
            try_call("pane.focus", pane_id=focus_id)
        log("restored entry %d (%s) as %s" % (entry["id"], entry["kind"], focus_id))


def reopen_dest(entry):
    """Destination for a pane/tab whose original tab is gone, or None if the tab exists."""
    if tab_exists(entry["tab_id"]):
        return None
    if workspace(entry["workspace_id"]):
        return {"type": "new_tab", "workspace_id": entry["workspace_id"],
                "label": entry["tab_label"]}
    return {"type": "new_workspace", "label": entry["workspace_label"],
            "tab_label": entry["tab_label"]}


def put_back_tab(entry, new_tab_id, dest):
    """Restore sidebar/tab-bar position after a tab or workspace was recreated."""
    if dest["type"] == "new_workspace":
        ws_id = new_tab_id.split(":")[0]
        try_call("workspace.move", workspace_id=ws_id,
                 insert_index=min(entry["workspace_index"], len(workspaces())))
    ws_id = new_tab_id.split(":")[0]
    try_call("tab.move", tab_id=new_tab_id, insert_index=min(entry["tab_index"], len(tabs(ws_id))))


def restore_tab(entry):
    held = list(entry["held"])
    tree = export(held[0])["root"]
    dest = reopen_dest(entry) or {"type": "new_tab", "workspace_id": entry["workspace_id"],
                                  "label": entry["tab_label"]}
    tab_id, idmap = transplant(tree, dest)
    put_back_tab(entry, tab_id, dest)
    order = leaves(tree)
    return idmap[order[min(entry.get("focused_index", 0), len(order) - 1)]]


def restore_pane(entry):
    held_id = next(iter(entry["held"]))
    dest = reopen_dest(entry)
    if dest:
        moved = move(held_id, dest)
        put_back_tab(entry, moved["tab_id"], dest)
        return moved["pane_id"]

    tab_id = entry["tab_id"]
    anchor_pane = next(iter_tab_panes(tab_id))
    unzoom(anchor_pane)
    layout = export(anchor_pane)
    tree = layout["root"]
    parent = entry["parent"] or {"direction": "right", "ratio": 0.5,
                                 "position": "second", "sibling_leaves": []}
    sibling_path = find_node(tree, set(parent["sibling_leaves"])) if parent["sibling_leaves"] else None

    if sibling_path is not None:
        # Exact: the whole sibling subtree is still intact. Collapse it to its
        # leftmost pane (staging the rest in a scratch tab), split that pane,
        # then grow the subtree back next to us.
        sibling = node_at(tree, sibling_path)
        anchor = leaves(sibling)[0]
        rest = leaves(sibling)[1:]
        staging = None
        for p in rest:
            if staging is None:
                staging = move(p, {"type": "new_tab", "workspace_id": entry["workspace_id"],
                                   "label": STAGING_LABEL})["tab_id"]
            else:
                move(p, {"type": "tab", "tab_id": staging, "split": "right"})
        restored = split_next_to(held_id, tab_id, anchor, parent)
        build(sibling, tab_id, {p: p for p in leaves(sibling)})
        tree = export(restored)["root"]
        path = find_path(tree, restored)
        if path:
            try_call("layout.set_split_ratio", tab_id=tab_id, path=path[:-1], ratio=parent["ratio"])
            set_ratios(tab_id, sibling, tuple(path[:-1]) + (not path[-1],))
        return restored

    # The sibling changed shape: split the nearest recorded neighbour that is
    # still in the tab, else the tab's focused pane.
    in_tab = set(leaves(tree))
    near = parent["sibling_leaves"] if parent["position"] == "first" else parent["sibling_leaves"][::-1]
    neighbour = next((p for p in near if p in in_tab), None)
    if neighbour:
        return split_next_to(held_id, tab_id, neighbour, parent)
    return split_next_to(held_id, tab_id, layout["focused_pane_id"],
                         {"direction": "right", "ratio": 0.5, "position": "second"})


def split_next_to(held_id, tab_id, target, parent):
    """Split `target` and put the held pane on the recorded side of it."""
    moved = move(held_id, {"type": "tab", "tab_id": tab_id, "target_pane_id": target,
                           "split": parent["direction"], "ratio": parent["ratio"]})
    if parent["position"] == "first":
        call("pane.swap", source_pane_id=moved["pane_id"], target_pane_id=target)
    return moved["pane_id"]


# --------------------------------------------------------- startup/status

def cmd_startup():
    """After a server restart held processes are gone: clean up what's left."""
    with locked_state() as state:
        reconcile(state)
        log("startup: %d entries still held" % len(state["entries"]))
    ensure_timer()


def cmd_status():
    with locked_state() as state:
        now = time.time()
        out = {"hold_ws": hold_ws(state), "grace_seconds": grace_seconds(),
               "entries": [dict(e, expires_in=round(e["expires_at"] - now, 1))
                           for e in state["entries"]]}
    print(json.dumps(out, indent=1, ensure_ascii=False))


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]
    args = dict(zip(rest[::2], rest[1::2]))
    try:
        if cmd == "close":
            cmd_close(args)
        elif cmd == "close-tab":
            cmd_close_tab(args)
        elif cmd == "undo":
            cmd_undo(args)
        elif cmd == "expire-daemon":
            cmd_expire_daemon()
        elif cmd == "startup":
            cmd_startup()
        elif cmd == "status":
            cmd_status()
        else:
            print(__doc__)
            return 2
    except HerdrError as e:
        log("%s failed: %s" % (cmd, e))
        notify("Undo close: %s failed" % cmd, str(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
