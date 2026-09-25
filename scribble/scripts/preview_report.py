"""Dev tool: render the report deliverable from SYNTHETIC data for eyeballing the design, without a live
stack or any real engagement.

Builds a throwaway scribble engagement on an in-memory sqlite (the standalone harness), seeds a realistic
spread of findings — the real vuln-kind multiplicity (one vuln on many hosts), descriptions, remediation,
a code block, and an evidence screenshot — renders the ``.docx``, and (optionally) converts it to PDF via
a Gotenberg service. All synthetic: generic public vuln names + RFC5737 hosts, no client data.

    # docx only:
    uv run --extra dev python -m scripts.preview_report --out /tmp/preview
    # + PDF via a running Gotenberg (docker run -p 3011:3000 gotenberg/gotenberg:8):
    uv run --extra dev python -m scripts.preview_report --out /tmp/preview --gotenberg http://127.0.0.1:3011

Then open ``<out>/report.docx`` / ``<out>/report.pdf``.
See ``scribble/report_templates/build_default_docx.py`` for the template this exercises.
"""
from __future__ import annotations

import argparse
import struct
import uuid
import zlib
from pathlib import Path

from flask import Flask
from sqlalchemy import create_engine, event

import scribble
from scribble import models as M
from scribble.content import schema
from scribble.enums import ArtifactKind, ArtifactPlacement, Severity
from scribble.reporting import build_report_context
from scribble.reporting.render_docx import render_report_docx
from scribble.seed import seed_defaults
from scribble.testing import register_kit_assets_shim, wire_mock_host

# (title, instances-on-distinct-hosts, severity) — the real-shape multiplicity (fleet-wide vulns), so the
# preview exercises the by-vulnerability collapse at scale. Generic public vuln names; no client data.
KIND_DIST: list[tuple[str, int, Severity]] = [
    ("Multi-function printer - unauthorized access [mfp-unauth-exposure]", 50, Severity.medium),
    ("Toshiba TopAccess - default-login [topaccess-default-login]", 15, Severity.high),
    ("Brother MFC-L9570CDW - information disclosure [cve-2024-51977]", 9, Severity.medium),
    ("Brother printers - authentication bypass via default admin password", 8, Severity.critical),
    ("Sharp multifunction printers - local file inclusion [sharp-printers-lfi]", 8, Severity.high),
    ("Sharp multifunction printers - directory listing [cve-2024-33605]", 8, Severity.high),
    ("Dahua IPC/VTH/VTO - authentication bypass [cve-2021-33044]", 4, Severity.critical),
    ("Nortek Linear eMerge E3-series - SQL injection [cve-2022-38627]", 2, Severity.critical),
    ("vsftpd < 3.0.3 - denial of service [cve-2021-30047]", 1, Severity.high),
]
PATHS = ["/setup", "/contentwebserver", "/etc/mnt_info.csv", "/general/status.html",
         "/installed_emanual_down.html?path=/manual", "/installed_emanual_list.html",
         "/RPC2_Login", "/badging/badge_template_print.php?tpl=aa.xml&idt=1337", "/"]


def _png(w: int, h: int, rgb: tuple[int, int, int]) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(t: bytes, d: bytes) -> bytes:
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def _describe(title: str) -> str:
    name = title.split(" [")[0]
    return (
        f"The assessment identified {name} exposed on the internal network. The service is reachable "
        f"without prior authentication and responds to unauthenticated requests, placing it within the "
        f"attacker-reachable surface of the environment.\n\n"
        f"An adversary with network access to the affected hosts can leverage this weakness to obtain "
        f"unauthorized access or disclose sensitive configuration, a likely foothold for lateral movement."
    )


def build(out_dir: Path, gotenberg: str | None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "preview"
    engine = create_engine(f"sqlite:///{out_dir / 'preview.db'}", future=True)

    @event.listens_for(engine, "connect")
    def _fk(c, _r):  # noqa: ANN001
        cur = c.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    cfg = scribble.register(app, engine, instance_path=str(out_dir), base_template="scribble/base.html")
    with cfg.session_factory() as s:
        seed_defaults(s)
        s.commit()
    wire_mock_host(app.extensions["scribble"])
    register_kit_assets_shim(app)

    shot = _png(560, 300, (18, 34, 52))
    shots: dict[str, bytes] = {}
    sf = cfg.session_factory
    with sf() as db:
        client = M.Client(name="Northwind Logistics")
        db.add(client)
        db.flush()
        board = M.ReportBoard(name="20260101_northwind_int", client_id=client.id,
                              company_name="Northwind Logistics", scope_type="external")
        grp = M.FindingGroup(engagement=board, name="Internal Network", order_index=0)
        db.add_all([board, grp])
        db.flush()
        idx = 0
        for k, (title, n, sev) in enumerate(KIND_DIST):
            path = PATHS[k % len(PATHS)]
            for _ in range(n):
                idx += 1
                host = f"192.0.2.{idx % 254 + 1}"
                content = {"description": schema.doc_from_text(_describe(title)),
                           "remediation": schema.doc_from_text(
                               "Restrict network access to the affected service, require authentication, "
                               "and update the device firmware to a vendor-patched release.")}
                if k % 3 == 0:
                    content["details"] = schema.code_block_doc(
                        f"GET {path} HTTP/1.1\nHost: {host}\nUser-Agent: assessment\nAccept: */*")
                bf = M.BoardFinding(
                    engagement_id=board.id, group_id=grp.id, order_index=idx, title=title, severity=sev,
                    cvss_score=9.8 if sev == Severity.critical else 7.5,
                    target_host=host, target_port="80", target_url=f"http://{host}:80{path}",
                    content_json=content,
                    source_facts={"dedupe_key": f"nuclei:{title[:24]}:{idx}", "source": "nuclei"})
                db.add(bf)
                db.flush()
                if idx <= 2:
                    sp = f"obj:{uuid.uuid7()}"
                    shots[sp] = shot
                    db.add(M.Artifact(engagement_id=board.id, finding_id=bf.id,
                                      kind=ArtifactKind.screenshot, placement=ArtifactPlacement.attached,
                                      filename=f"evidence_{idx}.png", content_type="image/png",
                                      storage_path=sp, byte_size=len(shot)))
        db.commit()
        bid = board.id

    with sf() as db, app.app_context():
        docx_bytes = render_report_docx(build_report_context(db.get(M.ReportBoard, bid)),
                                        artifact_bytes=lambda sp: shots.get(sp))
    docx_path = out_dir / "report.docx"
    docx_path.write_bytes(docx_bytes)
    print(f"wrote {docx_path} ({len(docx_bytes)} bytes)")

    if gotenberg:
        import requests
        resp = requests.post(
            gotenberg.rstrip("/") + "/forms/libreoffice/convert",
            files={"files": ("report.docx", docx_bytes,
                             "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
            data={"updateIndexes": "true", "exportBookmarks": "true"}, timeout=180)
        resp.raise_for_status()
        pdf_path = out_dir / "report.pdf"
        pdf_path.write_bytes(resp.content)
        print(f"wrote {pdf_path} ({len(resp.content)} bytes)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Render the scribble report from synthetic data (dev tool).")
    ap.add_argument("--out", default="/tmp/scribble-preview", help="output directory")
    ap.add_argument("--gotenberg", default=None, help="Gotenberg base URL to also render a PDF")
    a = ap.parse_args()
    build(Path(a.out), a.gotenberg)


if __name__ == "__main__":
    main()
