#!/usr/bin/env python3
"""
rns-failover - priority failover between Reticulum uplink interfaces.

Reticulum has no "backup interface" setting: every attached interface carries
your announces and path requests, and receives every announce the far side
floods. Linking one node to several hubs that are already bridged to each
other (for example your own gateway <-> hub A <-> hub B) multiplies that
traffic and makes the node prefer whichever hub is one hop closer instead of
your own gateway.

This watchdog keeps ONLY the best working uplink attached, using rnsd's live
interface management (rnstatus --attach / --detach, RNS >= 1.5.6), so rnsd
and the apps using it are never restarted.

  tiers      interface names (the [[section]] names in the Reticulum config),
             highest priority first.
  failover   when no attached tier has been online for --grace seconds,
             attach the next tier down (higher tiers stay attached so they
             can reconnect on their own; a disconnected TCP client sends
             nothing, so this causes no duplication).
  failback   when a higher tier has been online for --hold seconds, detach
             every tier below it.

Interfaces not listed as tiers (AutoInterface, RNode, ...) are never touched.

  --sync-config  also set each tier's "enabled" line in the Reticulum config
             file to match what is attached (yes when attached, no when
             detached), so apps that show interfaces from the config file
             (MeshChatX's Interfaces page) show the live uplink. Only that one
             line in the matching [[section]] is changed; the file is replaced
             atomically and its permissions are kept.
  --meshchatx-refresh URL
             (with --sync-config) MeshChatX keeps its own in-memory copy of
             the config, so its Interfaces page would not see the change. When
             MeshChatX's view of the tier flags differs from the file, re-save
             the unchanged config through its raw-config API, which reloads its
             copy without restarting RNS. Each such save adds one entry to
             MeshChatX's config version history (it keeps the last 25).
"""
import argparse
import json
import os
import re
import tempfile
import http.cookiejar
import urllib.request
import subprocess
import sys
import time


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


class Failover:
    def __init__(self, a):
        self.a = a
        self.tiers = a.tiers
        self.online_since = {t: None for t in self.tiers}
        self.all_down_since = None

    def rnstatus(self, *args):
        cmd = [self.a.rnstatus] + (["--config", self.a.config] if self.a.config else []) + list(args)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30)

    def status(self):
        r = self.rnstatus("-j")
        # rnstatus may print RNS log lines before the JSON, so start at the
        # first line that opens the JSON object.
        start = next((i for i, l in enumerate(r.stdout.splitlines()) if l.startswith("{")), None)
        if r.returncode != 0 or start is None:
            return None
        data = json.loads("\n".join(r.stdout.splitlines()[start:]))
        return {i.get("short_name"): bool(i.get("status")) for i in data.get("interfaces", [])}

    def config_file(self):
        base = self.a.config or os.path.expanduser("~/.reticulum")
        return base if os.path.isfile(base) else os.path.join(base, "config")

    def set_enabled(self, name, on):
        """Set the enabled line of [[name]] in the config file. Returns True if changed."""
        path = self.config_file()
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        want = "yes" if on else "no"
        head = re.compile(r"^\s*\[\[\s*" + re.escape(name) + r"\s*\]\]\s*$")
        sect = re.compile(r"^\s*\[")
        key = re.compile(r"^(\s*)(interface_enabled|enabled)(\s*=\s*)(\S+)(.*)$", re.I)
        i = next((n for n, l in enumerate(lines) if head.match(l)), None)
        if i is None:
            log(f"sync-config: [[{name}]] not found in {path}")
            return False
        j = i + 1
        while j < len(lines) and not sect.match(lines[j]):
            m = key.match(lines[j].rstrip("\n"))
            if m:
                if m.group(4).lower() in (("yes", "true", "1") if on else ("no", "false", "0")):
                    return False
                cur = m.group(4).lower()
                if cur in ("true", "false"):
                    want = "true" if on else "false"
                lines[j] = f"{m.group(1)}{m.group(2)}{m.group(3)}{want}{m.group(5)}\n"
                break
            j += 1
        else:
            # no enabled line in the section: add one right after the header
            lines.insert(i + 1, f"    enabled = {want}\n")
        st = os.stat(path)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".config.failover.")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(lines)
        os.chmod(tmp, st.st_mode & 0o7777)
        os.replace(tmp, path)
        log(f"sync-config: [[{name}]] enabled = {want}")
        return True

    def attach(self, name):
        r = self.rnstatus("--attach", name)
        log(f"attach  {name}: {(r.stdout or r.stderr).strip()}")
        if self.a.sync_config and r.returncode == 0:
            self.set_enabled(name, True)

    def detach(self, name):
        r = self.rnstatus("--detach", name)
        log(f"detach  {name}: {(r.stdout or r.stderr).strip()}")
        if self.a.sync_config and r.returncode == 0:
            self.set_enabled(name, False)

    def meshchatx_refresh(self):
        """Reload MeshChatX's in-memory config if its tier flags differ from the file."""
        base = self.a.meshchatx_refresh.rstrip("/")
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        try:
            with op.open(base + "/api/v1/reticulum/interfaces", timeout=10) as r:
                shown = json.load(r).get("interfaces", {})
        except Exception:
            return  # MeshChatX not running (or not reachable): nothing to refresh
        on = lambda v: str(v).strip().lower() in ("yes", "true", "1", "on")
        want = self.file_flags()
        stale = [t for t in self.tiers if t in want and t in shown and isinstance(shown[t], dict)
                 and on(shown[t].get("interface_enabled", shown[t].get("enabled", "yes"))) != want[t]]
        if not stale:
            return
        try:
            with op.open(base + "/api/v1/auth/csrf", timeout=10) as r:
                token = json.load(r).get("csrf_token", "")
            with open(self.config_file(), encoding="utf-8") as f:
                body = json.dumps({"content": f.read()}).encode()
            req = urllib.request.Request(base + "/api/v1/reticulum/config/raw", data=body, method="PUT",
                                         headers={"Content-Type": "application/json", "X-CSRF-Token": token})
            with op.open(req, timeout=20) as r:
                r.read()
            log(f"meshchatx: reloaded its config view ({', '.join(stale)})")
        except Exception as e:
            log(f"meshchatx: refresh failed: {e}")

    def file_flags(self):
        """{tier: enabled?} as written in the config file."""
        flags, cur = {}, None
        head = re.compile(r"^\s*\[\[\s*(.+?)\s*\]\]\s*$")
        key = re.compile(r"^\s*(interface_enabled|enabled)\s*=\s*(\S+)", re.I)
        with open(self.config_file(), encoding="utf-8") as f:
            for line in f:
                m = head.match(line)
                if m:
                    cur = m.group(1)
                    continue
                if line.lstrip().startswith("["):
                    cur = None
                    continue
                k = key.match(line)
                if k and cur in self.tiers and cur not in flags:
                    flags[cur] = k.group(2).lower() in ("yes", "true", "1", "on")
        return flags

    def sync_all(self, st):
        """Make the config's enabled lines match what is attached right now."""
        for t in self.tiers:
            self.set_enabled(t, t in st)
        if self.a.meshchatx_refresh:
            self.meshchatx_refresh()

    def step(self):
        st = self.status()
        if st is None:
            log("rnsd shared instance not reachable; waiting")
            return
        if self.a.sync_config:
            # every pass: also repairs the flags if another program (e.g.
            # MeshChatX saving its own copy of the config) wrote them back
            self.sync_all(st)
        now = time.time()
        attached = [t for t in self.tiers if t in st]
        for t in self.tiers:
            if st.get(t):
                self.online_since[t] = self.online_since[t] or now
            else:
                self.online_since[t] = None

        # Highest-priority tier that is online right now.
        best = next((i for i, t in enumerate(self.tiers) if st.get(t)), None)

        if best is not None:
            self.all_down_since = None
            # keep everything above best attached so it can reconnect by itself
            for t in self.tiers[:best]:
                if t not in st:
                    self.attach(t)
            # drop everything below best once best has been stable for --hold
            if now - self.online_since[self.tiers[best]] >= self.a.hold:
                for t in self.tiers[best + 1:]:
                    if t in st:
                        log(f"{self.tiers[best]} is up; failing back")
                        self.detach(t)
            return

        # Nothing online.
        if not attached:
            self.attach(self.tiers[0])
            self.all_down_since = now
            return
        self.all_down_since = self.all_down_since or now
        lowest = max(self.tiers.index(t) for t in attached)
        for t in self.tiers[:lowest]:
            if t not in st:
                self.attach(t)
        if now - self.all_down_since >= self.a.grace and lowest + 1 < len(self.tiers):
            log(f"no uplink online for {int(now - self.all_down_since)}s; failing over")
            self.attach(self.tiers[lowest + 1])
            self.all_down_since = now

    def run(self):
        log("tiers: " + "  >  ".join(self.tiers))
        while True:
            try:
                self.step()
            except Exception as e:
                log(f"error: {e}")
            if self.a.once:
                break
            time.sleep(self.a.interval)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tier", dest="tiers", action="append", required=True,
                   help="interface name, repeat in priority order")
    p.add_argument("--rnstatus", default="rnstatus")
    p.add_argument("--config", default=None, help="Reticulum config dir")
    p.add_argument("--interval", type=float, default=15)
    p.add_argument("--grace", type=float, default=30, help="seconds all-down before failing over")
    p.add_argument("--hold", type=float, default=20, help="seconds a higher tier must be up before failing back")
    p.add_argument("--once", action="store_true")
    p.add_argument("--sync-config", action="store_true",
                   help="keep each tier's enabled line in the config file matching what is attached")
    p.add_argument("--meshchatx-refresh", metavar="URL", default=None,
                   help="with --sync-config: reload MeshChatX's config view when it is stale, "
                        "e.g. http://127.0.0.1:9337")
    a = p.parse_args()
    Failover(a).run()


if __name__ == "__main__":
    sys.exit(main())
