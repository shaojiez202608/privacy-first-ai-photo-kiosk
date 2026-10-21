"""Offline demo of scenarios D1-D6 with the in-memory adapters (no AWS, no network, no Docker).

    uv run python scripts/demo_local.py [--write-audit-sample]

Prints one line per check and a summary; exits 1 if any check fails. D6 reads the synthesized
templates in cdk.out-regions/ if `make synth-regions` has been run (it needs no AWS credentials).
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts"), str(ROOT / "infra")]
from gen_samples import build  # noqa: E402

from kiosk import ids  # noqa: E402
from kiosk.settings import Settings  # noqa: E402
from kiosk.wiring import LocalStack  # noqa: E402

RESULTS: list[tuple[str, str, bool]] = []


def check(scn: str, what: str, ok: bool) -> None:
    RESULTS.append((scn, what, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {scn} {what}")


class Clock:
    t = 1_792_540_800.0  # 2026-10-21T00:00:00Z

    def __call__(self) -> float:
        return self.t


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--params", default=str(ROOT / "config" / "params.example.json"))
    ap.add_argument("--write-audit-sample", action="store_true")
    args = ap.parse_args()
    params = json.loads(Path(args.params).read_text())
    settings = Settings.from_params(params)
    clock = Clock()
    k = LocalStack(settings, clock=clock)
    svc = k.service
    img = {n: base64.b64encode(v[0]).decode() for n, v in build().items()}
    notice = svc.s.notice()

    def consent(decision: str):
        return svc.create_session({"consent": decision, "notice_version": notice["notice_version"],
                                   "notice_sha256": notice["notice_sha256"]})

    print(f"== notice {notice['notice_version']} retention={settings.retention_seconds}s "
          f"link<={settings.share_link_ttl_seconds}s")

    print("== D1 normal flow")
    _, s = consent("accepted")
    sid, sec = s["session_id"], s["session_secret"]
    ref = ids.session_ref(sid)
    code, _ = svc.submit_photo(sid, sec, {"image_base64": img["landscape-gps.jpg"], "style": "comic"})
    st = svc.status(sid, sec)[1]
    q = st.get("share", {}).get("url", "?").split("?", 1)[-1]
    check("D1", "photo accepted (202) and result ready", code == 202 and st["status"] == "ready")
    check("D1", "input deleted right after processing (0 versions)",
          k.objects.count_versions(ids.object_prefix(ref) + "input.png") == 0)
    check("D1", "download link works", k.objects.resolve_share(q)[0] == 200)
    check("D1", "QR code returned", st.get("share", {}).get("qr_svg", "").startswith("data:image/svg"))
    clock.t += settings.share_link_ttl_seconds + 1
    check("D1", "link refused after its expiry", k.objects.resolve_share(q)[0] == 403)

    print("== D2 decline")
    before = k.objects.total_versions()
    code, body = consent("declined")
    check("D2", "declined -> no session id returned", code == 200 and "session_id" not in body)
    check("D2", "no object written", k.objects.total_versions() == before)

    print("== D3 delete on request")
    code, d = svc.delete(sid, sec)
    r = d.get("receipt", {})
    check("D3", "deletion receipt returned and MAC verifies", code == 200 and k.signer.verify(r, r["mac"]))
    check("D3", "no object version or delete marker left", k.objects.count_versions(ids.object_prefix(ref)) == 0)
    check("D3", "session record deleted", k.sessions.get(ref) is None)
    check("D3", "audit log has session_deleted", "session_deleted" in k.audit_sink.names())

    print("== D4 expiry (retention elapses)")
    _, s = consent("accepted")
    sid4, sec4 = s["session_id"], s["session_secret"]
    svc.submit_photo(sid4, sec4, {"image_base64": img["blocks.png"], "style": "pixel"})
    clock.t += settings.retention_seconds + 1
    out = k.run_due_expiries()
    check("D4", "expiry workflow purged the session", out and out[0]["status"] == "deleted")
    check("D4", "nothing left in storage", k.objects.total_versions() == 0)
    check("D4", "audit log has session_expired", "session_expired" in k.audit_sink.names())

    print("== D5 content and embedded instructions (mock moderation markers)")
    for sample in ("unsafe-marker.png", "instruction.png"):
        _, s = consent("accepted")
        svc.submit_photo(s["session_id"], s["session_secret"], {"image_base64": img[sample], "style": "comic"})
        st = svc.status(s["session_id"], s["session_secret"])[1]
        check("D5", f"{sample}: rejected, no share link", st["status"] == "rejected" and "share" not in st)
        svc.delete(s["session_id"], s["session_secret"])
    check("D5", "rejections recorded (input_blocked)", k.audit_sink.names().count("input_blocked") == 2)
    check("D5", "nothing left in storage", k.objects.total_versions() == 0)

    print("== D6 region pinning")
    from region_check import check as region_check

    found = 0
    for region in ("us-east-1", "us-west-2"):
        tpl = ROOT / "cdk.out-regions" / region / f"{params['deploy']['stack_name']}.template.json"
        if not tpl.is_file():
            print(f"  [SKIP] D6 {region}: run `make synth-regions` first (no AWS credentials needed)")
            continue
        found += 1
        rc = region_check(json.loads(tpl.read_text()), params, region)
        check("D6", f"{region}: synthesized, no regional resource outside the region, cross-region calls: "
                    f"{rc['cross_region_calls']}", not rc["unexplained_findings"])

    text = "\n".join(k.audit_sink.lines)
    check("LOG", "audit log has no session id, secret or image data",
          sid not in text and sec not in text and img["landscape.png"][:60] not in text)
    if args.write_audit_sample:
        (ROOT / "sample-data" / "audit-log-sample.jsonl").write_text(text + "\n")
        print("  wrote sample-data/audit-log-sample.jsonl")

    failed = [r for r in RESULTS if not r[2]]
    print(f"== summary: {len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed"
          + (f", D6 templates found for {found} region(s)" if found else ", D6 skipped (no templates)"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
