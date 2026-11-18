# rns-failover

**Priority failover between Reticulum uplinks: use your best hub, keep the
others as backups.**

Reticulum has no "backup interface" setting. If your node connects to
several hubs, for example your own gateway plus one or two public ones, it
uses **all of them at once**:

- every attached interface carries your announces and path requests;
- every attached interface receives every announce the far side floods;
- when the hubs are also linked to each other, you get the same traffic
  several times over;
- your node picks whichever hub is one hop closer to a destination, which
  may not be the one you wanted to use.

`rns-failover.py` keeps **only the highest-priority working uplink
attached**, and brings the others in only when it is needed.

```
   Primary Hub      attached        <- normal operation
   Secondary Hub    detached
   Fallback Hub     detached

   Primary Hub      down, attached  <- keeps trying to reconnect
   Secondary Hub    attached        <- failover after --grace seconds
   Fallback Hub     detached

   Primary Hub      back up         <- failback after --hold seconds
   Secondary Hub    detached
   Fallback Hub     detached
```

## How it works

Every `--interval` seconds (default 15) it reads interface status with
`rnstatus -j`, then:

- **Failover:** if no attached uplink has been online for `--grace` seconds
  (default 30), it attaches the next one down the list.
- **Failback:** once a higher-priority uplink has been back online for
  `--hold` seconds (default 20), it detaches everything below it.
- Higher-priority uplinks stay attached while they are down, so they can
  reconnect on their own. A disconnected TCP client sends nothing, so this
  causes no duplication.
- Interfaces you don't list (AutoInterface, RNode, server interfaces, ...)
  are never touched.

Changes are made with rnsd's live interface management (`rnstatus --attach`
/ `--detach`), so neither rnsd nor any program using it is restarted.

## Requirements

- **RNS 1.5.6 or newer**, for `rnstatus --attach/--detach`.
- rnsd running as a **shared instance** (`share_instance = Yes`, the
  default).
- Python 3. There are no extra packages; the script calls `rnstatus`.

## Setup

1. **Mark the backup uplinks disabled** in `~/.reticulum/config`, so rnsd
   starts with only the primary attached. `--attach` re-reads the config and
   works on disabled sections.
   ```ini
   [[Primary Hub]]
     type = TCPClientInterface
     enabled = yes
     ...

   [[Secondary Hub]]
     type = TCPClientInterface
     enabled = false
     ...

   [[Fallback Hub]]
     type = TCPClientInterface
     enabled = false
     ...
   ```
2. **Install the script** for the user that runs rnsd:
   ```bash
   chmod +x rns-failover.py
   cp rns-failover.py ~/.local/bin/
   ```
3. **Try one pass by hand.** Use your own `[[section names]]`, exactly as
   `rnstatus` prints them in square brackets:
   ```bash
   python3 ~/.local/bin/rns-failover.py --once \
       --tier "Primary Hub" --tier "Secondary Hub" --tier "Fallback Hub"
   ```
4. **Run it as a service** (below).

### Linux (systemd)

Edit `rns-failover.service`: set `User=`, the paths, and one `--tier` per
uplink. Then:

```bash
sudo cp rns-failover.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rns-failover.service
sudo journalctl -u rns-failover -f     # watch what it does
```

If you use [reticulum-readygate](https://github.com/jwheeler188/reticulum-readygate),
make the unit depend on `reticulum-ready.service` instead (see the comment
in the unit), and add `rns-failover.service` to the `DEPENDENTS` list in its
stack scripts.

### macOS (launchd)

Edit `example-rns-failover.plist` (username, paths, tiers), then:

```bash
cp example-rns-failover.plist ~/Library/LaunchAgents/local.rns-failover.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.rns-failover.plist
tail -f ~/Library/Logs/rns-failover.log
```

## Options

| Option | Default | Meaning |
|---|---|---|
| `--tier NAME` | (required) | Uplink interface name. Repeat it, highest priority first. |
| `--rnstatus PATH` | `rnstatus` | Path to `rnstatus`. Use the full path when running as a service. |
| `--config DIR` | RNS default | Reticulum config directory, if not the default. |
| `--interval S` | 15 | Seconds between checks. |
| `--grace S` | 30 | Seconds with no uplink online before failing over. |
| `--hold S` | 20 | Seconds a higher tier must stay up before failing back. |
| `--once` | | Run one check and exit. |

## Example log

```
2026-11-17 09:17:49 tiers: Primary Hub  >  Secondary Hub  >  Fallback Hub
2026-11-17 09:18:25 Primary Hub is up; failing back
2026-11-17 09:18:27 detach  Secondary Hub: Interface Secondary Hub was detached
```

## Troubleshooting

- **"rnsd shared instance not reachable; waiting"**: rnsd is not running,
  not acting as a shared instance, or `--rnstatus` points at a different
  Python environment than rnsd. Use the full path to the `rnstatus` that
  belongs to your rnsd.
- **Nothing is ever attached or detached**: each `--tier` must match the
  interface's `[[section name]]` exactly, including spaces and case.
- **The log shows `rnstatus` rejecting `--attach` or `--detach`
  ("unrecognized arguments")**: your RNS is older than 1.5.6. Upgrade RNS.

## Tested on

- Raspberry Pi, Ubuntu 24.04 LTS, RNS 1.6.0 (systemd)
- macOS 13, RNS 1.5.7 (launchd)

## License

Public domain, released under [The Unlicense](https://unlicense.org). See
[`LICENSE`](LICENSE).
