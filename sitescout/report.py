"""Compiles all module outputs into one site report: printed to the
terminal and optionally saved as JSON + Markdown.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("sitescout.report")


def build_report(query: str, resolved, geo, sections: dict) -> dict:
    return {
        "query": query,
        "resolved_address": resolved.address_text,
        "eircode": resolved.eircode,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "location": {
            "lat": geo.lat,
            "lon": geo.lon,
            "label": geo.label,
            "source": geo.source,
            "precise": geo.precise,
            "location_type": geo.location_type,
            "warning": (
                None if geo.precise else
                "APPROXIMATE — this is an area/postcode-level estimate, not the "
                "exact building. See geocode.py docstring / CLAUDE.md."
            ),
        },
        "sections": sections,
    }


def print_report(report: dict) -> None:
    print("\n" + "=" * 72)
    print(f"SITE REPORT — {report['resolved_address']}")
    if report["eircode"]:
        print(f"Eircode: {report['eircode']}")
    loc = report["location"]
    print(f"Coordinates: {loc['lat']:.6f}, {loc['lon']:.6f}  (via {loc['source']})")
    if loc["warning"]:
        print(f"⚠  {loc['warning']}")
    print("=" * 72)

    s = report["sections"]

    if "boundary" in s:
        b = s["boundary"]
        print("\n-- Property boundary (cadastral) --")
        if b.get("found"):
            print(f"  {b['tenure']} parcel, {b['county']}: ~{b['area_hectares']} ha ({b['area_acres']} acres)")
            print(f"  {b['caveat']}")
        else:
            print(f"  Not found. {b.get('note', '')}")

    if "geology" in s:
        g = s["geology"]
        print("\n-- Geology & subsoil --")
        print(f"  Bedrock: {g.get('bedrock_unit') or 'no data at this point'}")
        print(f"  Subsoil: {g.get('subsoil_type') or 'no data at this point'}")

    if "groundwater" in s:
        w = s["groundwater"]
        print("\n-- Water table --")
        print(f"  Groundwater vulnerability: {w.get('vulnerability_category') or 'no data at this point'}")

    if "archaeology" in s:
        a = s["archaeology"]
        count_label = f"{a['monument_count']}{'+' if a.get('more_exist') else ''}"
        print(f"\n-- Archaeology & heritage ({count_label} within 2km) --")
        for m in a["monuments"][:6]:
            print(f"  - {m['class']} ({m['smr_ref']})")
        for link in a.get("also_check", []):
            print(f"  Also check: {link}")

    if "smr_zone" in s:
        z = s["smr_zone"]
        zone_count_label = f"{z.get('zone_count', 0)}{'+' if z.get('more_exist') else ''}"
        print(f"\n-- SMR Zone (archaeological notification zone; {zone_count_label} mapped within 2km) --")
        if z.get("in_zone"):
            print(f"  Within SMR Zone {z['zone_id']} (~{z['area_hectares']} ha)")
        print(f"  {z['caveat']}")

    if "niah" in s:
        n = s["niah"]
        count_label = f"{n['structure_count']}{'+' if n.get('more_exist') else ''}"
        print(f"\n-- Protected structures / NIAH ({count_label} within 500m) --")
        for st in n["structures"][:6]:
            print(f"  - {st['name'] or st['address']} ({st['rating']}, reg. {st['reg_no']})")

    if "planning_applications" in s:
        p = s["planning_applications"]
        if p.get("site_match") and p["site_match"]["application_count"]:
            sm = p["site_match"]
            print(f"\n-- Planning applications — this Eircode ({sm['application_count']} exact match) --")
            for app in sm["applications"][:6]:
                print(f"  - {app['application_number']}: {app['status']} / {app['decision']} — {(app['description'] or '')[:70]}")
        count_label = f"{p['application_count']}{'+' if p.get('more_exist') else ''}"
        print(f"\n-- Planning applications ({count_label} within {p.get('search_radius_m', 500)}m) --")
        for app in p["applications"][:6]:
            print(f"  - {app['application_number']}: {app['status']} / {app['decision']} — {(app['description'] or '')[:70]}")

    if "flood_risk" in s:
        f = s["flood_risk"]
        print("\n-- Flood risk (OPW CFRAM, current climate) --")
        print(f"  Fluvial (river): {f['fluvial_probability'] or 'not mapped at this point'}")
        print(f"  Coastal: {f['coastal_probability'] or 'not mapped at this point'}")
        print(f"  {f['caveat']}")

    if "ecology" in s:
        e = s["ecology"]
        count_label = f"{e['site_count']}{'+' if e.get('more_exist') else ''}"
        print(f"\n-- Ecology & nature conservation (NPWS; {count_label} designated area(s) within 2km) --")
        if e["any_within"]:
            for v in e["within"].values():
                if v:
                    print(f"  ⚠ Within {v['type_label']}: {v['site_name']} ({v['site_code']})")
        print(f"  {e['caveat']}")

    if "rps_aca" in s:
        r = s["rps_aca"]
        print("\n-- RPS & ACA (statutory protected structures / conservation areas) --")
        if not r["covered"]:
            print(f"  {r['note']}")
        else:
            print(f"  Local authority: {r['authority']}")
            rp = r["rps"]
            if rp.get("available"):
                count_label = f"{rp['structure_count']}{'+' if rp.get('more_exist') else ''}"
                print(f"  RPS: {count_label} protected structure(s) within 500m")
                for st in rp["structures"][:6]:
                    print(f"    - {st.get('address') or st.get('description')} ({st.get('ref')})")
            else:
                print("  RPS: no live source for this local authority yet")
            ac = r["aca"]
            if ac.get("available"):
                if ac["in_aca"]:
                    print(f"  ACA: within {ac['current']['name']}")
                else:
                    print(f"  ACA: not within one ({ac['area_count']} mapped nearby)")
            else:
                print("  ACA: no live source for this local authority yet")
            print(f"  {r['note']}")

    if "utilities" in s:
        u = s["utilities"]
        print("\n-- Utilities (request-based, no open API) --")
        print(f"  Electricity -> {u['electricity']['to']}")
        print(f"  Water/wastewater -> {u['water_wastewater']['to']}")

    if "planning" in s:
        p = s["planning"]
        print("\n-- Planning context (links) --")
        for k, v in p.items():
            if k != "note":
                print(f"  {k}: {v}")

    print("\n" + "=" * 72 + "\n")


def save_report(report: dict, out_dir: str = "output") -> Path:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    stamp = report["eircode"] or report["query"]
    stamp = "".join(c if c.isalnum() else "_" for c in stamp)

    json_path = out_path / f"{stamp}.json"
    json_path.write_text(json.dumps(report, indent=2))
    log.info("Saved JSON report: %s", json_path)

    md_path = out_path / f"{stamp}.md"
    md_path.write_text(_to_markdown(report))
    log.info("Saved Markdown report: %s", md_path)

    return md_path


def _to_markdown(report: dict) -> str:
    loc = report["location"]
    lines = [
        f"# Site report — {report['resolved_address']}",
        "",
        f"- Eircode: {report['eircode'] or 'n/a'}",
        f"- Coordinates: {loc['lat']:.6f}, {loc['lon']:.6f} (via {loc['source']})",
    ]
    if loc["warning"]:
        lines.append(f"- ⚠ {loc['warning']}")
    lines.append("")

    s = report["sections"]

    if "boundary" in s:
        b = s["boundary"]
        lines.append("## Property boundary (cadastral)")
        if b.get("found"):
            lines.append(f"{b['tenure']} parcel, {b['county']}: ~{b['area_hectares']} ha ({b['area_acres']} acres)")
            lines.append(f"> {b['caveat']}")
        else:
            lines.append(f"Not found. {b.get('note', '')}")
        lines.append("")

    if "geology" in s:
        g = s["geology"]
        lines.append("## Geology & subsoil")
        lines.append(f"- Bedrock: {g.get('bedrock_unit') or 'no data at this point'}")
        lines.append(f"- Subsoil: {g.get('subsoil_type') or 'no data at this point'}")
        lines.append("")

    if "groundwater" in s:
        w = s["groundwater"]
        lines.append("## Water table")
        lines.append(f"- Groundwater vulnerability: {w.get('vulnerability_category') or 'no data at this point'}")
        lines.append("")

    if "archaeology" in s:
        a = s["archaeology"]
        count_label = f"{a['monument_count']}{'+' if a.get('more_exist') else ''}"
        lines.append(f"## Archaeology & heritage ({count_label} within 2km)")
        for m in a["monuments"][:6]:
            lines.append(f"- {m['class']} ({m['smr_ref']})")
        lines.append("")

    if "smr_zone" in s:
        z = s["smr_zone"]
        zone_count_label = f"{z.get('zone_count', 0)}{'+' if z.get('more_exist') else ''}"
        lines.append(f"## SMR Zone (archaeological notification zone; {zone_count_label} mapped within 2km)")
        if z.get("in_zone"):
            lines.append(f"Within SMR Zone {z['zone_id']} (~{z['area_hectares']} ha)")
        lines.append(f"> {z['caveat']}")
        lines.append("")

    if "niah" in s:
        n = s["niah"]
        count_label = f"{n['structure_count']}{'+' if n.get('more_exist') else ''}"
        lines.append(f"## Protected structures / NIAH ({count_label} within 500m)")
        for st in n["structures"][:6]:
            lines.append(f"- {st['name'] or st['address']} ({st['rating']}, reg. {st['reg_no']})")
        lines.append("")

    if "planning_applications" in s:
        p = s["planning_applications"]
        if p.get("site_match") and p["site_match"]["application_count"]:
            sm = p["site_match"]
            lines.append(f"## Planning applications — this Eircode ({sm['application_count']} exact match)")
            for app in sm["applications"][:6]:
                lines.append(f"- {app['application_number']}: {app['status']} / {app['decision']} — {(app['description'] or '')[:70]}")
            lines.append("")
        count_label = f"{p['application_count']}{'+' if p.get('more_exist') else ''}"
        lines.append(f"## Planning applications ({count_label} within {p.get('search_radius_m', 500)}m)")
        for app in p["applications"][:6]:
            lines.append(f"- {app['application_number']}: {app['status']} / {app['decision']} — {(app['description'] or '')[:70]}")
        lines.append("")

    if "flood_risk" in s:
        f = s["flood_risk"]
        lines.append("## Flood risk (OPW CFRAM, current climate)")
        lines.append(f"- Fluvial (river): {f['fluvial_probability'] or 'not mapped at this point'}")
        lines.append(f"- Coastal: {f['coastal_probability'] or 'not mapped at this point'}")
        lines.append(f"> {f['caveat']}")
        lines.append("")

    if "ecology" in s:
        e = s["ecology"]
        count_label = f"{e['site_count']}{'+' if e.get('more_exist') else ''}"
        lines.append(f"## Ecology & nature conservation (NPWS; {count_label} designated area(s) within 2km)")
        if e["any_within"]:
            for v in e["within"].values():
                if v:
                    lines.append(f"- Within {v['type_label']}: {v['site_name']} ({v['site_code']})")
        lines.append(f"> {e['caveat']}")
        lines.append("")

    if "rps_aca" in s:
        r = s["rps_aca"]
        lines.append("## RPS & ACA (statutory protected structures / conservation areas)")
        if not r["covered"]:
            lines.append(r["note"])
        else:
            lines.append(f"Local authority: {r['authority']}")
            rp = r["rps"]
            if rp.get("available"):
                count_label = f"{rp['structure_count']}{'+' if rp.get('more_exist') else ''}"
                lines.append(f"- RPS: {count_label} protected structure(s) within 500m")
                for st in rp["structures"][:6]:
                    lines.append(f"  - {st.get('address') or st.get('description')} ({st.get('ref')})")
            else:
                lines.append("- RPS: no live source for this local authority yet")
            ac = r["aca"]
            if ac.get("available"):
                if ac["in_aca"]:
                    lines.append(f"- ACA: within {ac['current']['name']}")
                else:
                    lines.append(f"- ACA: not within one ({ac['area_count']} mapped nearby)")
            else:
                lines.append("- ACA: no live source for this local authority yet")
            lines.append(f"> {r['note']}")
        lines.append("")

    if "utilities" in s:
        u = s["utilities"]
        lines.append("## Utilities (request-based)")
        lines.append(f"- Electricity: {u['electricity']['to']}")
        lines.append(f"- Water/wastewater: {u['water_wastewater']['to']}")
        lines.append("")

    if "planning" in s:
        p = s["planning"]
        lines.append("## Planning context")
        for k, v in p.items():
            if k != "note":
                lines.append(f"- {k}: {v}")
        lines.append("")

    return "\n".join(lines)
