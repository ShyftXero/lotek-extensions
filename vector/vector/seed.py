"""Idempotent first-boot seed: the Spark Range attack path as a read-only ``builtin`` example.

This is a faithful port of the hand-built reference deliverable
(``CS2026-OPFOR/DAY_4_SANDWORM/PUPPY/spark-range-attack-path_4.html``) into ``vector.attackpath/v1``.
It doubles as proof the abstraction round-trips the real document, and as a rich fixture for tests/e2e.
Names/IPs/domains are the fictional range scenario, unchanged from the reference.
"""

from __future__ import annotations

import json

from vector.models import Diagram
from vector.schema import normalize

EXAMPLE_NAME = "Spark Range — Red Team Attack Path (example)"


def _model() -> dict:
    zones = [
        {"id": "attacker", "title": "ATTACKER INFRA", "subtitle": "Chitauri 203.0.113.0/24", "accent": "red", "order": 0},
        {"id": "itdmz", "title": "IT DMZ", "subtitle": "spark-it-dmz 172.18.3.0/24", "accent": "cyan", "order": 1},
        {"id": "itsvc", "title": "IT SERVICES", "subtitle": "spark-it-services 172.18.8.0/24", "accent": "cyan", "order": 2},
        {"id": "otdmz", "title": "OT DMZ", "subtitle": "spark-ot-DMZ 172.18.5.0/24", "accent": "amber", "order": 3},
        {"id": "ics", "title": "OT / ICS", "subtitle": "spark-ot-ICS 172.18.7.0/24", "accent": "amber", "order": 4},
    ]
    boundaries = [
        {"afterZone": "attacker", "top": "internet edge", "bottom": "north-america-region"},
        {"afterZone": "itdmz", "top": "spark-it-fw-1", "bottom": "ot-edge-router · 172.18.2.x"},
        {"afterZone": "itsvc", "top": "spark-it-fw-2", "bottom": "172.18.4.x gate"},
        {"afterZone": "otdmz", "top": "engineering-2", "bottom": "dual-homed bridge"},
    ]
    nodes = [
        {"id": "opfor-1", "label": "opfor-1", "ip": "203.0.113.24", "domain": "juliet.future.org",
         "zone": "attacker", "row": 0, "role": "rshell", "states": []},
        {"id": "opfor-2", "label": "opfor-2", "ip": "203.0.113.28", "domain": "core-align.com",
         "zone": "attacker", "row": 1, "role": "c2", "states": [{"at": 0, "label": "QUANTUMCAT C2"}]},
        {"id": "opfor-3", "label": "opfor-3", "ip": "203.0.113.25", "domain": "guest.network.com",
         "zone": "attacker", "row": 2, "role": "stager",
         "reIp": {"at": 15, "ip": "203.0.113.29", "domain": "coast.titan.org"},
         "states": [{"at": 0, "label": "CATSONBROADWAY"}, {"at": 15, "label": "SSH TUNNEL"}]},
        {"id": "opfor-4", "label": "opfor-4", "ip": "203.0.113.30", "domain": "db.inspire.net",
         "zone": "attacker", "row": 3, "role": "backup", "activateAt": 15,
         "states": [{"at": 15, "label": "QUANTUMCAT #2"}]},

        {"id": "ubuntu-dmz-1", "label": "ubuntu-dmz-1", "ip": "172.18.3.11", "zone": "itdmz", "row": 0,
         "states": [{"at": 1, "state": "target"}, {"at": 1, "state": "owned", "label": "SHELL"},
                    {"at": 2, "label": "HOST · ROOT"}, {"at": 4, "state": "beacon", "label": "beacon.elf"}]},
        {"id": "ubuntu-dmz-2", "label": "ubuntu-dmz-2", "ip": "172.18.3.13", "zone": "itdmz", "row": 1, "context": True},
        {"id": "spark-it-file", "label": "spark-it-file", "ip": "172.18.3.7", "zone": "itdmz", "row": 2, "context": True},

        {"id": "spark-ot-it-3", "label": "spark-ot-it-3", "ip": "172.18.8.33", "zone": "itsvc", "row": 0,
         "states": [{"at": 6, "state": "target"}, {"at": 7, "state": "owned", "label": "RDP"},
                    {"at": 8, "label": "SYSMON OFF"}, {"at": 9, "state": "beacon", "label": "csrss.exe"}]},
        {"id": "ubuntu-spark-1", "label": "ubuntu-spark-1", "ip": "172.18.8.51", "zone": "itsvc", "row": 1,
         "states": [{"at": 14, "state": "beacon", "label": "beacon"}]},
        {"id": "ubuntu-spark-2", "label": "ubuntu-spark-2", "ip": "172.18.8.52", "zone": "itsvc", "row": 2,
         "states": [{"at": 14, "state": "beacon", "label": "beacon"}]},
        {"id": "billing", "label": "billing", "ip": "172.18.8.3", "zone": "itsvc", "row": 3, "context": True},

        {"id": "engineering-2", "label": "engineering-2", "ip": "172.18.5.22", "zone": "otdmz", "row": 0,
         "dualIp": "172.18.7.24",
         "states": [{"at": 10, "state": "target"}, {"at": 11, "state": "owned", "label": "RDP"},
                    {"at": 12, "state": "beacon", "label": "beacon"}]},
        {"id": "engineering-1", "label": "engineering-1", "ip": "172.18.5.21", "zone": "otdmz", "row": 1, "context": True},
        {"id": "historian-1", "label": "historian-1", "ip": "172.18.5.41", "zone": "otdmz", "row": 2, "context": True},

        {"id": "spark-ot-hmi-1", "label": "spark-ot-hmi-1", "ip": "172.18.7.51", "zone": "ics", "row": 0,
         "states": [{"at": 13, "state": "beacon", "label": "beacon"}]},
        {"id": "spark-ot-hmi-2", "label": "spark-ot-hmi-2", "ip": "172.18.7.52", "zone": "ics", "row": 1,
         "states": [{"at": 13, "state": "beacon", "label": "beacon"}, {"at": 16, "state": "impacted", "label": "MySQL"}]},
        {"id": "spark-plc-1", "label": "spark-plc-1", "ip": "172.18.7.21", "zone": "ics", "row": 2,
         "states": [{"at": 13, "state": "beacon", "label": "beacon"}, {"at": 16, "state": "impacted", "label": "PLC"}]},
        {"id": "spark-plc-2", "label": "spark-plc-2", "ip": "172.18.7.22", "zone": "ics", "row": 3,
         "states": [{"at": 16, "state": "impacted", "label": "PLC"}]},
        {"id": "spark-ot-relay-1", "label": "spark-ot-relay-1", "ip": "172.18.7.41", "zone": "ics", "row": 4,
         "states": [{"at": 13, "state": "beacon", "label": "beacon"}, {"at": 16, "state": "impacted", "label": "relay"}]},
        {"id": "spark-ot-relay-2", "label": "spark-ot-relay-2", "ip": "172.18.7.42", "zone": "ics", "row": 5,
         "states": [{"at": 13, "state": "beacon", "label": "beacon"}, {"at": 16, "state": "impacted", "label": "relay"}]},
        {"id": "spark-ot-relay-3", "label": "spark-ot-relay-3", "ip": "172.18.7.43", "zone": "ics", "row": 6,
         "states": [{"at": 13, "state": "beacon", "label": "beacon"}, {"at": 16, "state": "impacted", "label": "relay"}]},
    ]
    edges = [
        {"id": "exploit", "from": "opfor-1", "to": "ubuntu-dmz-1", "kind": "attack", "at": 1, "route": "flow", "offset": -8, "label": "Cacti RCE → rev shell"},
        {"id": "stage", "from": "opfor-3", "to": "ubuntu-dmz-1", "kind": "transfer", "at": 3, "route": "flow", "offset": 0, "label": ".elf · CatsonBroadway"},
        {"id": "c2_1", "from": "ubuntu-dmz-1", "to": "opfor-2", "kind": "c2", "at": 4, "route": "arcTop", "lane": -34, "label": "raw TCP / 443"},
        {"id": "socks_1", "from": "opfor-2", "to": "ubuntu-dmz-1", "kind": "tunnel", "at": 5, "route": "flow", "offset": 15, "label": "SOCKS"},
        {"id": "disc_1", "from": "ubuntu-dmz-1", "to": "spark-ot-it-3", "kind": "disc", "at": 6, "route": "flow", "offset": -13, "label": "crackmapexec"},
        {"id": "rdp_1", "from": "ubuntu-dmz-1", "to": "spark-ot-it-3", "kind": "attack", "at": 7, "route": "flow", "offset": 13, "label": "proxychains xfreerdp"},
        {"id": "c2_2", "from": "spark-ot-it-3", "to": "opfor-2", "kind": "c2", "at": 9, "route": "arcTop", "lane": -56, "label": "csrss.exe beacon"},
        {"id": "disc_2", "from": "spark-ot-it-3", "to": "engineering-2", "kind": "disc", "at": 10, "route": "flow", "offset": -13, "label": "crackmapexec"},
        {"id": "rdp_2", "from": "spark-ot-it-3", "to": "engineering-2", "kind": "attack", "at": 11, "route": "flow", "offset": 13, "label": "proxychains xfreerdp"},
        {"id": "c2_3", "from": "engineering-2", "to": "opfor-2", "kind": "c2", "at": 12, "route": "arcTop", "lane": -78, "label": "beacon"},
        {"id": "mesh_1", "from": "spark-ot-it-3", "to": "engineering-2", "kind": "mesh", "at": 12, "route": "flow", "offset": 30, "label": "P2P mesh"},
        {"id": "mesh_h1", "from": "engineering-2", "to": "spark-ot-hmi-1", "kind": "mesh", "at": 13, "route": "flow", "offset": 0},
        {"id": "mesh_h2", "from": "engineering-2", "to": "spark-ot-hmi-2", "kind": "mesh", "at": 13, "route": "flow", "offset": -7, "label": "ICS beacon mesh"},
        {"id": "mesh_p1", "from": "engineering-2", "to": "spark-plc-1", "kind": "mesh", "at": 13, "route": "flow", "offset": -7},
        {"id": "mesh_r1", "from": "engineering-2", "to": "spark-ot-relay-1", "kind": "mesh", "at": 13, "route": "flow", "offset": -7},
        {"id": "mesh_r2", "from": "engineering-2", "to": "spark-ot-relay-2", "kind": "mesh", "at": 13, "route": "flow", "offset": -7},
        {"id": "mesh_r3", "from": "engineering-2", "to": "spark-ot-relay-3", "kind": "mesh", "at": 13, "route": "flow", "offset": -7},
        {"id": "persist", "from": "spark-ot-it-3", "to": "ubuntu-spark-1", "kind": "transfer", "at": 14, "route": "intra"},
        {"id": "persist2", "from": "spark-ot-it-3", "to": "ubuntu-spark-2", "kind": "transfer", "at": 14, "route": "intra", "label": "beacons"},
        {"id": "ssh", "from": "ubuntu-spark-1", "to": "opfor-3", "kind": "ssh", "at": 15, "route": "arcBot", "lane": 498, "label": "SSH tunnel out"},
        {"id": "ssh_bak", "from": "opfor-3", "to": "opfor-4", "kind": "ssh", "at": 15, "route": "intra", "label": "tunnel backup"},
        {"id": "socks_3", "from": "ubuntu-spark-1", "to": "engineering-2", "kind": "tunnel", "at": 15, "route": "flow", "offset": -14, "label": "SOCKS → Eng"},
        {"id": "act_sql", "from": "engineering-2", "to": "spark-ot-hmi-2", "kind": "action", "at": 16, "route": "flow", "offset": 7, "label": "MySQL · via tunnel"},
        {"id": "dis_p1", "from": "engineering-2", "to": "spark-plc-1", "kind": "disrupt", "at": 16, "route": "flow", "offset": -7, "label": "disrupt · Quantumcat mesh"},
        {"id": "dis_p2", "from": "engineering-2", "to": "spark-plc-2", "kind": "disrupt", "at": 16, "route": "flow", "offset": 0},
        {"id": "dis_r1", "from": "engineering-2", "to": "spark-ot-relay-1", "kind": "disrupt", "at": 16, "route": "flow", "offset": -7},
        {"id": "dis_r2", "from": "engineering-2", "to": "spark-ot-relay-2", "kind": "disrupt", "at": 16, "route": "flow", "offset": -7},
        {"id": "dis_r3", "from": "engineering-2", "to": "spark-ot-relay-3", "kind": "disrupt", "at": 16, "route": "flow", "offset": -7},
    ]
    meta = {
        "title": "Spark Range — Red Team Attack Path",
        "subtitle": "IT → OT kill chain · 16 phases",
        "badge": "Attack Walkthrough",
        "railLabels": ["Initial access", "IT pivot", "OT DMZ", "ICS impact"],
        "intro": {
            "eyebrow": "Objective · purple-team walkthrough",
            "objective": (
                "A 16-phase red-team path across the Spark range. Starting from the public-facing Cacti "
                "server in the IT DMZ, we escalate, establish C2, and pivot through four trust zones — "
                "IT DMZ → IT Services → OT DMZ → OT/ICS — building a beacon mesh until we can command the "
                "PLCs, relays and HMI."
            ),
            "readingNotes": (
                "Each phase lights up the hosts it touches and draws its network edge. Compromise state "
                "accumulates — by phase 16 the full kill-chain graph is on screen. Red = offense, orange = "
                "C2 phone-home, cyan = tunnels, amber = OT interaction. Flip to the Blue Team Detection tab "
                "for tools, example queries, and what was (or was not) seen on range."
            ),
            "note": (
                "Attacker infra: opfor-1 juliet.future.org (reverse shells) · opfor-2 core-align.com "
                "(Quantumcat console) · opfor-3 guest.network.com (CatsonBroadway staging + SSH tunnel "
                "egress) · opfor-4 db.inspire.net (Quantumcat #2 backup). Later OT/ICS phases are first-pass "
                "designs — amber gap chips mean validate live."
            ),
        },
    }
    phases = [{"n": 0, "intro": True}] + _phases()
    return {"schema": "vector.attackpath/v1", "meta": meta, "zones": zones,
            "boundaries": boundaries, "nodes": nodes, "edges": edges, "phases": phases}


def _phases() -> list[dict]:
    return [
        {"n": 1, "title": "Cacti RCE → reverse shell", "tactics": [{"label": "Initial Access", "kind": "attack"}],
         "mitre": "T1190 · Exploit Public-Facing Application",
         "desc": "Exploit the Cacti web app on ubuntu-dmz-1 for unauthenticated RCE. The reverse shell calls back to opfor-1 and lands inside the Cacti Docker container — not yet the host.",
         "targets": ["ubuntu-dmz-1", "opfor-1"],
         "watch": "Red exploit edge from opfor-1 (reverse-shell listener) into ubuntu-dmz-1; the box flips to owned.",
         "blue": {"tool": "Security Onion · Suricata / Kibana · Host", "finding": "Cacti remote_agent.php poller_id injection → reverse shell as www-data inside the container. Confirmed on range: Unusual Process Spawned from Web Server Parent (Medium/47) on ubuntu-dmz-1.",
                  "query": "rule.name:\"Unusual Process Spawned from Web Server Parent\" and host.name:ubuntu-dmz-1\nhttp.request.uri:*remote_agent.php* and http.request.uri:*poller_id*",
                  "seen": "Exploit URI with mkfifo / bash -i / openssl s_client in poller_id. Host: www-data spawning interactive shell from web parent on ubuntu-dmz-1.",
                  "note": "Also cheap signals: preceding nmap -sV on 80/443/8080/8443, and any admin:admin login attempt on the DMZ apps. Day 3 xp_cmdshell Suricata often fires at the start of Day 4 — that is Day 3 bleed, not initial access.", "gap": False}},
        {"n": 2, "title": "Container escape via SSH to host", "tactics": [{"label": "Privilege Esc", "kind": "attack"}],
         "mitre": "T1611 Escape to Host · T1078 Valid Accounts",
         "desc": "Break out of the Cacti container by SSHing to the ubuntu-dmz-1 host IP with known credentials. We now control the host, not just the container.",
         "targets": ["ubuntu-dmz-1"], "watch": "Status on ubuntu-dmz-1 changes from container SHELL to HOST · ROOT.",
         "blue": {"tool": "Host SSH auth / Elastic", "finding": "SSH escape from Cacti container to Docker host as local_svc_act (Day 3 SAM dump reuse) via 172.20.0.1.",
                  "query": "rule.name:\"Successful SSH Authentication from Unusual IP Address\" and user.name:local_svc_act\nauth / secure: Accepted password for local_svc_act from 172.20.*",
                  "seen": "local_svc_act authenticating from container-adjacent space.",
                  "note": "Any local_svc_act SSH sourced from 172.20.0.0/16 should be treated as confirmed compromise — first reuse of the Day 3 shared local admin.", "gap": False}},
        {"n": 3, "title": "Stage the beacon (.elf)", "tactics": [{"label": "Command & Control", "kind": "c2"}],
         "mitre": "T1105 · Ingress Tool Transfer",
         "desc": "From the host, pull a Linux beacon (.elf) staged on opfor-3 (guest.network.com) with the CatsonBroadway tool. The beacon will call home to the C2 console on opfor-2.",
         "targets": ["ubuntu-dmz-1", "opfor-3"], "watch": "Dashed transfer edge from opfor-3 (CatsonBroadway) to ubuntu-dmz-1.",
         "blue": {"tool": "Host / Elastic · Network", "finding": "Beacon .elf staged via CatsonBroadway from opfor-3 (guest.network.com / 203.0.113.25). On range: sftp-server wrote .ntp_key (Remote File Creation in World Writeable Directory).",
                  "query": "rule.name:\"Remote File Creation in World Writeable Directory\" and process.name:sftp-server\nfile.name:.ntp_key OR file.path:*ntp_key*",
                  "seen": "Medium/47 on ubuntu-spark-2 — local_svc_act, sftp-server → .ntp_key ~1s after non-standard-port SSH.",
                  "note": "Pull .ntp_key for hash hunt elsewhere. Staging host is CatsonBroadway on opfor-3 — later reused as SSH tunnel egress (phase 15).", "gap": False}},
        {"n": 4, "title": "Beacon callback — raw TCP / 443", "tactics": [{"label": "Command & Control", "kind": "c2"}],
         "mitre": "T1095 · Non-Application Layer Protocol",
         "desc": "Execute the beacon. It calls home to the C2 console (opfor-2) as a raw TCP connection over port 443 — no TLS, just the port.",
         "targets": ["ubuntu-dmz-1", "opfor-2"], "watch": "Orange C2 arc rises over the top from ubuntu-dmz-1 back to opfor-2.",
         "blue": {"tool": "Security Onion · Zeek/Suricata · Linux process audit", "finding": "Beacon executes with argv spoof exec -a '[kworker/0:1-events]' and callbacks to C2 core-align.com / 203.0.113.28 over raw TCP/443.",
                  "query": "destination.ip:203.0.113.28 OR dns.query:core-align.com\nauditd/EDR: process.name:*kworker* AND NOT executable:/proc/*",
                  "seen": "DMZ egress to C2. ps aux may show a fake kworker; real process audit shows path on disk.",
                  "note": "Kernel worker threads never have a backing file — that mismatch is higher confidence than a name mismatch alone.", "gap": False}},
        {"n": 5, "title": "SOCKS pivot into the range", "tactics": [{"label": "Command & Control", "kind": "tunnel"}],
         "mitre": "T1572 Protocol Tunneling · T1090 Proxy",
         "desc": "Stand up a SOCKS proxy through the beacon so we can drive tooling from Kali straight into the victim network.",
         "targets": ["ubuntu-dmz-1"], "watch": "Cyan dashed SOCKS tunnel through ubuntu-dmz-1. Everything downstream now rides this.",
         "blue": {"tool": "Elastic / Host · Network", "finding": "Reverse SOCKS through the beacon — subsequent “internal” actions originate from the external operator via this pivot.",
                  "query": "rule.name:\"Potential Linux Tunneling and/or Port Forwarding\"\nprocess.name:ssh and parent.process.name:sh and user.name:local_svc_act",
                  "seen": "Confirmed twice (Medium/47) on ubuntu-spark-2 — ssh from sh as local_svc_act.",
                  "note": "Existence means cut this host → stall the chain.", "gap": False}},
        {"n": 6, "title": "crackmapexec → IT Services reachable", "tactics": [{"label": "Discovery", "kind": "disc"}],
         "mitre": "T1018 Remote System Discovery · T1046",
         "desc": "Run crackmapexec with the known creds over the tunnel. The creds are valid into spark-it-services (172.18.8.0/24) over RDP and SSH.",
         "targets": ["spark-ot-it-3"], "watch": "Faint cyan discovery edge reaching into IT Services; spark-ot-it-3 marked as a target.",
         "blue": {"tool": "Security Onion + Kibana Security", "finding": "crackmapexec sprays local_svc_act across 172.18.8.0/26 over RDP and SSH — textbook credential stuffing fan-out.",
                  "query": "event.code:(4624 or 4625) and winlog.event_data.TargetUserName:local_svc_act\nrule.name:\"Multiple Alerts Involving a User\" and user.name:local_svc_act",
                  "seen": "Multiple Alerts Involving a User High/73 — 4× local_svc_act (expected fan-out).",
                  "note": "Standing rule: one account hitting many hosts in a short window. Rotate / LAPS this account.", "gap": False}},
        {"n": 7, "title": "RDP onto spark-ot-it-3", "tactics": [{"label": "Lateral Movement", "kind": "attack"}],
         "mitre": "T1021.001 Remote Services: RDP",
         "desc": "proxychains + xfreerdp through the SOCKS tunnel to land an interactive session on spark-ot-it-3.",
         "targets": ["spark-ot-it-3"], "watch": "Red RDP edge lands on spark-ot-it-3; it flips to owned.",
         "blue": {"tool": "Kibana · Security Event Log", "finding": "Interactive RDP as local_svc_act onto spark-ot-it-3 (172.18.8.33) via proxychains xfreerdp.",
                  "query": "event.code:\"4624\" and winlog.event_data.LogonType:10 and winlog.event_data.TargetUserName:local_svc_act\nhost.name:spark-ot-it-3 OR destination.ip:172.18.8.33",
                  "seen": "Logon Type 10 for a shared local admin.",
                  "note": "Shared local admin grabbing a desktop on an OT-adjacent IT host is inherently worth a look. MFA / JIT would raise the bar.", "gap": False}},
        {"n": 8, "title": "Kill the telemetry", "tactics": [{"label": "Defense Evasion", "kind": "evasion"}],
         "mitre": "T1562.001 Disable Tools · T1562.006 Indicator Blocking",
         "desc": "Unload the Sysmon driver and sinkhole its log shipping so our activity stops reaching the SOC.",
         "targets": ["spark-ot-it-3"], "watch": "Status on spark-ot-it-3 reads SYSMON OFF — we are now dark on this host.",
         "blue": {"tool": "Kibana · Windows Firewall / Sysmon", "finding": "Unload SysmonDrv and/or add firewall rules that sinkhole telemetry toward the SOC subnet (172.25.6.4). Plausible fake rule names: Windows Telemetry Update.",
                  "query": "event.code:\"2004\" and winlog.channel:\"Microsoft-Windows-Windows Firewall With Advanced Security/Firewall\"\nmessage:*SysmonDrv* OR process.command_line:*fltmc*unload*",
                  "seen": "New local firewall rules allow-listing egress to SOC subnet; Sysmon stops shipping.",
                  "note": "Rules named like Windows Update but pointing at your monitoring subnet are about as clean a signal as this env gets.", "gap": False}},
        {"n": 9, "title": "Drop beacon as csrss.exe", "tactics": [{"label": "Persistence", "kind": "persist"}, {"label": "Defense Evasion", "kind": "evasion"}],
         "mitre": "T1036.005 Masquerading: Match Legitimate Name",
         "desc": "Drop the next beacon on spark-ot-it-3, named csrss.exe to blend in with normal Windows processes.",
         "targets": ["spark-ot-it-3", "opfor-2"], "watch": "Second orange C2 arc, from spark-ot-it-3 (csrss.exe) home to opfor-2.",
         "blue": {"tool": "Kibana · Sysmon / Elastic", "finding": "beacon renamed csrss.exe under AppData\\Roaming\\Microsoft\\Crypto\\RSA\\ and launched (schtasks SysUpdate as SYSTEM).",
                  "query": "process.name:csrss.exe and NOT process.executable:*\\\\System32\\\\csrss.exe\nrule.name:\"Execution from Unusual Directory - Command Line\" and host.name:spark-ot-it-3",
                  "seen": "Execution from Unusual Directory ×4 (Medium/47) on spark-ot-it-3.",
                  "note": "Real csrss.exe only from System32, protected, never user-launched. Any other path = compromise.", "gap": False}},
        {"n": 10, "title": "New tunnel → OT DMZ reachable", "tactics": [{"label": "Discovery", "kind": "disc"}],
         "mitre": "T1018 · T1046 · T1078 Valid Accounts",
         "desc": "Open a fresh tunnel through the IT-subnet beacon and re-run crackmapexec. The creds now reach into the OT DMZ (172.18.5.0/24).",
         "targets": ["engineering-2"], "watch": "Discovery edge from spark-ot-it-3 into OT DMZ; engineering-2 becomes the target.",
         "blue": {"tool": "SO / Kibana — unvalidated past early IT pivot", "finding": "Fresh tunnel via IT beacon; crackmapexec of local_svc_act into OT DMZ 172.18.5.0/24 — engineering hosts light up.",
                  "query": "event.code:(4624 or 4625) and winlog.event_data.TargetUserName:local_svc_act\ndestination.ip:172.18.5.*",
                  "seen": "Design-time: same stuffing pattern as phase 6, new subnet. engineering-2 (172.18.5.22) is the interesting hit.",
                  "note": "Hotwash only validated through the early IT pivot. Treat phases 10–16 as first-pass until confirmed on range.", "gap": True}},
        {"n": 11, "title": "RDP onto Engineering-2", "tactics": [{"label": "Lateral Movement", "kind": "attack"}],
         "mitre": "T1021.001 Remote Services: RDP",
         "desc": "proxychains the new tunnel and RDP onto the Engineering-2 workstation in the OT DMZ. This box is dual-homed into the ICS subnet.",
         "targets": ["engineering-2"], "watch": "Red RDP edge lands on engineering-2 (note its 172.18.7.24 leg into OT/ICS).",
         "blue": {"tool": "Kibana · Security Event Log — unvalidated", "finding": "RDP as local_svc_act onto engineering-2 (172.18.5.22), dual-homed to 172.18.7.24 into ICS.",
                  "query": "event.code:\"4624\" and winlog.event_data.LogonType:10 and winlog.event_data.TargetUserName:local_svc_act\nhost.name:*engineering-2* OR destination.ip:172.18.5.22",
                  "seen": "Interactive logon on OT DMZ engineering box. Dual-home means ICS reachability from this session.",
                  "note": "Priority host once owned — bridge into OT/ICS.", "gap": True}},
        {"n": 12, "title": "Beacon mesh: IT ⇄ OT DMZ", "tactics": [{"label": "Command & Control", "kind": "mesh"}],
         "mitre": "T1572 · peer-to-peer C2",
         "desc": "Drop a beacon on Engineering-2 and have the spark-ot-it-3 beacon discover it. The two beacons link into a peer mesh.",
         "targets": ["engineering-2", "spark-ot-it-3"], "watch": "Third C2 arc + an amber P2P mesh link between spark-ot-it-3 and engineering-2.",
         "blue": {"tool": "SO egress + host — unvalidated", "finding": "Beacon on engineering-2; peer mesh with spark-ot-it-3. Third C2 arc to core-align.com plus amber P2P mesh.",
                  "query": "destination.ip:203.0.113.28 and source.ip:(172.18.5.22 or 172.18.8.33)",
                  "seen": "Design-time: second Windows beacon + mesh link IT⇄OT DMZ.",
                  "note": "Mesh means cutting one C2 arc may not isolate the peer.", "gap": True}},
        {"n": 13, "title": "Mesh expands into OT / ICS", "tactics": [{"label": "Discovery", "kind": "disc"}, {"label": "Command & Control", "kind": "mesh"}],
         "mitre": "T1018 Remote System Discovery",
         "desc": "From Engineering-2 (dual-homed into 172.18.7.0/24) the beacon peers with beacons in the OT ICS subnet — HMI-1, HMI-2, PLC-1 and relays 1–3 — meshing them into the rest of the infrastructure.",
         "targets": ["spark-ot-hmi-1", "spark-ot-hmi-2", "spark-plc-1", "spark-ot-relay-1", "spark-ot-relay-2", "spark-ot-relay-3"],
         "watch": "Amber mesh links fan from engineering-2 to HMI-1, HMI-2, PLC-1 and relays 1–3.",
         "blue": {"tool": "SO / ICS monitoring — unvalidated", "finding": "Mesh expands from engineering-2 into HMI-1/2, PLC-1, relays 1–3 on 172.18.7.0/24.",
                  "query": "source.ip:(172.18.5.22 or 172.18.7.24) and destination.ip:172.18.7.*",
                  "seen": "Design-time: amber mesh fan-out on map. ICS hosts join the C2 infrastructure.",
                  "note": "Highest-impact lateral move. No confirmed queries yet — validate against Zeek/ICS sensors.", "gap": True}},
        {"n": 14, "title": "Persistence on IT Ubuntu boxes", "tactics": [{"label": "Persistence", "kind": "persist"}],
         "mitre": "T1105 · T1078 Valid Accounts",
         "desc": "Plant beacons on the Ubuntu workstations in spark-it-services (ubuntu-spark-1/2) to harden persistence in the IT tier.",
         "targets": ["ubuntu-spark-1", "ubuntu-spark-2"], "watch": "Two persistence beacons dropped down the IT Services column.",
         "blue": {"tool": "Host SSH / Elastic — partial", "finding": "Persistence beacons on ubuntu-spark-1 (172.18.8.51) and ubuntu-spark-2 (172.18.8.52) via local_svc_act.",
                  "query": "user.name:local_svc_act and host.name:(ubuntu-spark-1 or ubuntu-spark-2)\nfile.name:.ntp_key OR process.command_line:*kworker*",
                  "seen": "Heavy signal on ubuntu-spark-2 earlier — confirm by timestamp vs phase.",
                  "note": "Hardens IT tier so OT mesh survives loss of spark-ot-it-3 alone.", "gap": False}},
        {"n": 15, "title": "SSH + SOCKS chain to OT", "tactics": [{"label": "Command & Control", "kind": "tunnel"}],
         "mitre": "T1572 Protocol Tunneling",
         "desc": "Open an SSH tunnel from IT-services out to opfor-3 (guest.network.com), then ride it plus a new SOCKS tunnel terminating on Engineering-2 to run commands against the OT hosts. opfor-4 (db.inspire.net) stands by as the tunnel backup.",
         "targets": ["opfor-3", "engineering-2", "opfor-4"], "watch": "Cyan SSH arc dips under to opfor-3, with a backup link to opfor-4; a SOCKS tunnel terminates on engineering-2.",
         "note": "Kill CatsonBroadway on opfor-3 first. The same box that staged the beacon is repurposed as the SSH tunnel egress — tear down the staging service, then it re-IPs to 203.0.113.29 (coast.titan.org) as the tunnel comes up.",
         "blue": {"tool": "SO egress — unvalidated", "finding": "SSH tunnel from IT Ubuntu out to opfor-3; opfor-3 re-IPs to coast.titan.org / 203.0.113.29; SOCKS terminates on engineering-2; opfor-4 (db.inspire.net) as backup.",
                  "query": "destination.ip:(203.0.113.25 or 203.0.113.29 or 203.0.113.30)\ndns.query:(guest.network.com or coast.titan.org or db.inspire.net)",
                  "seen": "Design-time: cyan SSH arc under the map + SOCKS to Eng-2. Staging host repurposed as egress.",
                  "note": "Kill CatsonBroadway on opfor-3 before tunnel — same box changes role and IP.", "gap": True}},
        {"n": 16, "title": "ICS impact — DB read + PLC disruption", "tactics": [{"label": "Impact / ICS", "kind": "impact"}],
         "mitre": "ICS ATT&CK · Modbus / SQL",
         "desc": "Two paths, deliberately split: the MySQL DB on HMI-2 is read directly over the SOCKS tunnel terminating on Engineering-2. The PLC and relay disruption is pushed through the beacon mesh — not the tunnel — so the destructive commands ride C2, not the pivot.",
         "targets": ["spark-ot-hmi-2", "spark-plc-1", "spark-plc-2", "spark-ot-relay-1", "spark-ot-relay-2", "spark-ot-relay-3"],
         "watch": "Solid amber MySQL edge to HMI-2 rides the tunnel; dashed amber disrupt edges to the PLCs and relays ride the beacon mesh.",
         "blue": {"tool": "SO / ICS / DB audit — unvalidated", "finding": "Split impact: MySQL read on HMI-2 over SOCKS tunnel; PLC/relay disruption via beacon mesh (not the tunnel).",
                  "query": "destination.ip:172.18.7.52 and source.ip:(172.18.5.22 or proxied)\ndestination.ip:(172.18.7.21 or 172.18.7.22 or 172.18.7.41 or 172.18.7.42 or 172.18.7.43)",
                  "seen": "Design-time only. Destructive path rides C2 mesh; DB read rides tunnel — hunt both channels.",
                  "note": "Do not assume one sensor covers both. Writeup stops before this ICS impact — validate live.", "gap": True}},
    ]


def seed_defaults(session) -> None:
    """Insert the read-only Spark Range example if it isn't already present. Idempotent."""
    existing = session.query(Diagram).filter(Diagram.builtin.is_(True), Diagram.name == EXAMPLE_NAME).first()
    if existing is not None:
        return
    doc = normalize(_model())
    session.add(
        Diagram(name=EXAMPLE_NAME, builtin=True, owner_id=None, created_by="seed",
                model_json=json.dumps(doc, ensure_ascii=False))
    )
    session.commit()
