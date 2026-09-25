"""Compiles all module outputs into one site report: printed to the
terminal and optionally saved as JSON + Markdown.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("sitescout.report")


def location_dict(geo) -> dict:
    """The "location" shape used everywhere a geocode result gets shown —
    the full report, and the web UI's lightweight `/api/scout` response
    (which returns before any section data exists, see webapp.py).
    """
    return {
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
    }


def build_report(query: str, resolved, geo, sections: dict) -> dict:
    return {
        "query": query,
        "resolved_address": resolved.address_text,
        "eircode": resolved.eircode,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "location": location_dict(geo),
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

    if "soil" in s:
        so = s["soil"]
        print("\n-- Soil survey (ISIS) --")
        if so.get("found"):
            depth = f"{so['depth_cm']}cm" if (so.get("depth_cm") or "").strip() else "n/a"
            print(f"  {so['soil_type']} ({so['association_name']} association) — drainage: {so['drainage']}, texture: {so['texture']}, depth: {depth}")
            if so.get("soil_organic_carbon_t_ha") is not None:
                print(f"  Soil organic carbon: {so['soil_organic_carbon_t_ha']:.1f} t/ha")
        else:
            print("  No ISIS soil polygon at this exact point")

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
        if p.get("on_site_applications"):
            print(f"\n-- Planning applications ON THIS SITE ({p['on_site_count']}, within the confirmed plot boundary) --")
            for app in p["on_site_applications"][:6]:
                print(f"  - {app['application_number']}: {app['status']} / {app['decision']} — {(app['description'] or '')[:70]}")
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
        print("\n-- Flood risk (OPW CFRAM) --")
        print(f"  Fluvial (river), current climate: {f['fluvial_probability'] or 'not mapped at this point'}")
        print(f"  Coastal, current climate: {f['coastal_probability'] or 'not mapped at this point'}")
        print(f"  Pluvial (surface water), current climate: {f['pluvial_probability'] or 'not mapped at this point'}")
        for scenario, scenario_label in (("mid_future", "mid-range future"), ("high_future", "high-end future")):
            fs = f["future_scenarios"][scenario]
            print(f"  Fluvial, {scenario_label} climate: {fs['fluvial_probability'] or 'not mapped at this point'}")
            print(f"  Coastal, {scenario_label} climate: {fs['coastal_probability'] or 'not mapped at this point'}")
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

    if "epa" in s:
        e = s["epa"]
        print("\n-- Environmental hazards (EPA) --")
        radon = e["radon"]
        if radon.get("found"):
            print(f"  Radon: {radon['risk_description']}")
        else:
            print("  Radon: no classification returned at this point")
        lf = e["landfills"]
        lf_count_label = f"{lf['landfill_count']}{'+' if lf.get('more_exist') else ''}"
        print(f"  Closed landfills: {lf_count_label} within 1km")
        for site in lf["landfills"][:6]:
            print(f"    - {site['name']} ({site['operated']})")
        ippc = e["ippc"]
        ippc_count_label = f"{ippc['facility_count']}{'+' if ippc.get('more_exist') else ''}"
        print(f"  Licensed IPPC/IED facilities: {ippc_count_label} within 1km")
        for fac in ippc["facilities"][:6]:
            print(f"    - {fac['name']} ({fac['licence_status']})")
        mines = e["mines"]
        mines_count_label = f"{mines['site_count']}{'+' if mines.get('more_exist') else ''}"
        print(f"  Historic mine sites: {mines_count_label} within {mines.get('search_radius_m', 2000)}m ({mines['boundary_count']} mapped working(s))")
        for site in mines["sites"][:6]:
            print(f"    - {site['name']} ({site['commodity']})")
        maj = e["major_industrial"]
        maj_count_label = f"{maj['facility_count']}{'+' if maj.get('more_exist') else ''}"
        print(f"  Major industrial facilities (EPA PRTR): {maj_count_label} within {maj.get('search_radius_m', 10000)}m")
        for fac in maj["facilities"][:8]:
            print(f"    - {fac['name']} — {fac['sector']}")

    if "geohazards" in s:
        g = s["geohazards"]
        print("\n-- Geohazards & aquifer (GSI) --")
        ls = g["landslide"]
        print(f"  Landslide susceptibility: {ls.get('class_description') or 'no data at this point'}")
        aq = g["aquifer"]
        print(f"  Bedrock aquifer: {aq.get('bedrock_aquifer_description') or 'no data at this point'}")
        if aq.get("sand_gravel_aquifer_description"):
            print(f"  Sand & gravel aquifer: {aq['sand_gravel_aquifer_description']}")
        karst = g["karst"]
        print(f"  Karst features: {karst['feature_count']} within {karst['search_radius_m']}m")
        sp = g["source_protection"]
        if sp["in_source_protection_area"]:
            print(f"  ⚠ Within public water supply source protection area: {sp['source_protection_area_name']}")
        elif sp["in_group_water_scheme_zone"]:
            print(f"  ⚠ Within group water scheme zone of contribution: {sp['group_water_scheme_name']}")
        else:
            print(f"  Not within a mapped source protection area ({sp['nearby_count']} nearby within {sp['search_radius_m']}m)")

    if "water_quality" in s:
        wq = s["water_quality"]
        print("\n-- Water body status (EPA WFD) --")
        gw = wq["groundwater_body"]
        if gw:
            print(f"  Groundwater body: {gw['name']} — {gw['status']}")
        for b in wq["surface_water_bodies"][:8]:
            print(f"  {b['type'].capitalize()}: {b['name']} — {b['status']}")
        if not wq["surface_water_bodies"]:
            print("  No nearby surface water bodies mapped")
        print(f"  {wq['caveat']}")

    if "wells" in s:
        w = s["wells"]
        print(f"\n-- Wells, springs & boreholes (GSI; {w['well_count']} within {w['search_radius_m']}m) --")
        if w["shallowest_water_strike_m"] is not None:
            print(f"  Shallowest recorded water-strike depth nearby: {w['shallowest_water_strike_m']}m "
                  f"({w['wells_with_depth_count']} of {w['well_count']} well(s) have a recorded depth)")
        else:
            print(f"  No depth-to-water data recorded at any of the {w['well_count']} well(s)/spring(s) nearby")
        for well in sorted(w["wells"], key=lambda x: (x["water_strike_m"] is None, x["water_strike_m"]))[:8]:
            depth_label = f"{well['water_strike_m']}m water strike" if well["water_strike_m"] is not None else "no recorded depth"
            print(f"    - {well['gsi_ref']} ({well['source_type'] or 'unknown type'}): {depth_label}")
        print(f"  {w['caveat']}")
        for source_type, est in (w.get("depth_estimates") or {}).items():
            if est.get("estimated_depth_m") is not None:
                print(f"  Estimated {source_type.lower()} water-strike depth: ~{est['estimated_depth_m']}m "
                      f"(from {est['sample_count']} real record(s) within {est['search_radius_m']}m, "
                      f"range {est['min_depth_m']}-{est['max_depth_m']}m)")
            else:
                print(f"  {est.get('note')}")

    if "biodiversity" in s:
        b = s["biodiversity"]
        print(f"\n-- Species records (GBIF; {b['total_record_count']} occurrence record(s) within {b['search_radius_km']}km) --")
        if b["threatened_species"]:
            print(f"  ⚠ {b['threatened_count']} threatened species (IUCN Red List) recorded nearby:")
            for sp in b["threatened_species"][:8]:
                print(f"    - {sp['species']} ({sp['common_name'] or 'no common name'}) — {sp['category_label']}, last observed {sp['last_observed']}")
        else:
            print("  No threatened (IUCN VU/EN/CR) species recorded nearby")
        print(f"  {b['caveat']}")

    if "terrain" in s:
        t = s["terrain"]
        print("\n-- Terrain & elevation --")
        p = t["precise"]
        if p["found"]:
            print(f"  Ground elevation (OPW LIDAR, {p['resolution_m']}m, surveyed {p['survey_date']}): {p['ground_elevation_m']}m")
            if p["surface_elevation_m"] is not None:
                print(f"  Surface elevation (incl. vegetation/buildings): {p['surface_elevation_m']}m (+{p['canopy_or_building_height_m']}m above ground)")
        else:
            print("  No precise LIDAR elevation at this point (OPW's survey concentrates on rivers/floodplains/coasts)")
        c = t["contours"]
        if c["elevation_range_m"]:
            print(f"  Nearby contours ({c['contour_count']} within {c['search_radius_m']}m): {c['elevation_range_m'][0]}-{c['elevation_range_m'][1]}m")
        else:
            print(f"  No contour lines within {c['search_radius_m']}m")
        print(f"  {t['caveat']}")

    if "utilities" in s:
        u = s["utilities"]
        print("\n-- Utilities (request-based, no open API) --")
        print(f"  Electricity -> {u['electricity']['to']}")
        print(f"  Water/wastewater -> {u['water_wastewater']['to']}")
        grid = u.get("grid")
        if grid:
            print("\n-- Transmission grid (EirGrid) --")
            ns = grid["nearest_station"]
            if ns:
                print(f"  Nearest substation: {ns['name']} ({ns['voltage']}), ~{ns['distance_m']}m away")
            else:
                print(f"  No substation within {grid['station_search_radius_m']}m")
            print(f"  {len(grid['lines'])} overhead line(s), {len(grid['cables'])} underground cable(s) within {grid['line_search_radius_m']}m")
            if grid["likely_crossing"]:
                print("  ⚠ A transmission line or cable likely crosses or closely borders this site")
            if grid["committed_stations"] or grid["committed_lines"]:
                print(f"  {len(grid['committed_stations'])} committed (planned) station(s), {len(grid['committed_lines'])} committed line(s)/cable(s) nearby")
            print(f"  {grid['caveat']}")

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

    if "soil" in s:
        so = s["soil"]
        lines.append("## Soil survey (ISIS)")
        if so.get("found"):
            depth = f"{so['depth_cm']}cm" if (so.get("depth_cm") or "").strip() else "n/a"
            lines.append(f"- {so['soil_type']} ({so['association_name']} association) — drainage: {so['drainage']}, texture: {so['texture']}, depth: {depth}")
            if so.get("soil_organic_carbon_t_ha") is not None:
                lines.append(f"- Soil organic carbon: {so['soil_organic_carbon_t_ha']:.1f} t/ha")
            lines.append(f"> {so['caveat']}")
        else:
            lines.append("No ISIS soil polygon at this exact point")
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
        if p.get("on_site_applications"):
            lines.append(f"## Planning applications ON THIS SITE ({p['on_site_count']}, within the confirmed plot boundary)")
            for app in p["on_site_applications"][:6]:
                lines.append(f"- {app['application_number']}: {app['status']} / {app['decision']} — {(app['description'] or '')[:70]}")
            lines.append("")
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
        lines.append("## Flood risk (OPW CFRAM)")
        lines.append(f"- Fluvial (river), current climate: {f['fluvial_probability'] or 'not mapped at this point'}")
        lines.append(f"- Coastal, current climate: {f['coastal_probability'] or 'not mapped at this point'}")
        lines.append(f"- Pluvial (surface water), current climate: {f['pluvial_probability'] or 'not mapped at this point'}")
        for scenario, scenario_label in (("mid_future", "mid-range future"), ("high_future", "high-end future")):
            fs = f["future_scenarios"][scenario]
            lines.append(f"- Fluvial, {scenario_label} climate: {fs['fluvial_probability'] or 'not mapped at this point'}")
            lines.append(f"- Coastal, {scenario_label} climate: {fs['coastal_probability'] or 'not mapped at this point'}")
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

    if "epa" in s:
        e = s["epa"]
        lines.append("## Environmental hazards (EPA)")
        radon = e["radon"]
        lines.append(f"- Radon: {radon['risk_description'] if radon.get('found') else 'no classification returned at this point'}")
        lf = e["landfills"]
        lf_count_label = f"{lf['landfill_count']}{'+' if lf.get('more_exist') else ''}"
        lines.append(f"- Closed landfills: {lf_count_label} within 1km")
        for site in lf["landfills"][:6]:
            lines.append(f"  - {site['name']} ({site['operated']})")
        ippc = e["ippc"]
        ippc_count_label = f"{ippc['facility_count']}{'+' if ippc.get('more_exist') else ''}"
        lines.append(f"- Licensed IPPC/IED facilities: {ippc_count_label} within 1km")
        for fac in ippc["facilities"][:6]:
            lines.append(f"  - {fac['name']} ({fac['licence_status']})")
        mines = e["mines"]
        mines_count_label = f"{mines['site_count']}{'+' if mines.get('more_exist') else ''}"
        lines.append(f"- Historic mine sites: {mines_count_label} within {mines.get('search_radius_m', 2000)}m ({mines['boundary_count']} mapped working(s))")
        for site in mines["sites"][:6]:
            lines.append(f"  - {site['name']} ({site['commodity']})")
        maj = e["major_industrial"]
        maj_count_label = f"{maj['facility_count']}{'+' if maj.get('more_exist') else ''}"
        lines.append(f"- Major industrial facilities (EPA PRTR): {maj_count_label} within {maj.get('search_radius_m', 10000)}m")
        for fac in maj["facilities"][:8]:
            lines.append(f"  - {fac['name']} — {fac['sector']}")
        lines.append("")

    if "geohazards" in s:
        g = s["geohazards"]
        lines.append("## Geohazards & aquifer (GSI)")
        ls = g["landslide"]
        lines.append(f"- Landslide susceptibility: {ls.get('class_description') or 'no data at this point'}")
        aq = g["aquifer"]
        lines.append(f"- Bedrock aquifer: {aq.get('bedrock_aquifer_description') or 'no data at this point'}")
        if aq.get("sand_gravel_aquifer_description"):
            lines.append(f"- Sand & gravel aquifer: {aq['sand_gravel_aquifer_description']}")
        karst = g["karst"]
        lines.append(f"- Karst features: {karst['feature_count']} within {karst['search_radius_m']}m")
        sp = g["source_protection"]
        if sp["in_source_protection_area"]:
            lines.append(f"- ⚠ Within public water supply source protection area: {sp['source_protection_area_name']}")
        elif sp["in_group_water_scheme_zone"]:
            lines.append(f"- ⚠ Within group water scheme zone of contribution: {sp['group_water_scheme_name']}")
        else:
            lines.append(f"- Not within a mapped source protection area ({sp['nearby_count']} nearby within {sp['search_radius_m']}m)")
        lines.append("")

    if "water_quality" in s:
        wq = s["water_quality"]
        lines.append("## Water body status (EPA WFD)")
        gw = wq["groundwater_body"]
        if gw:
            lines.append(f"- Groundwater body: {gw['name']} — {gw['status']}")
        for b in wq["surface_water_bodies"][:8]:
            lines.append(f"- {b['type'].capitalize()}: {b['name']} — {b['status']}")
        if not wq["surface_water_bodies"]:
            lines.append("- No nearby surface water bodies mapped")
        lines.append(f"> {wq['caveat']}")
        lines.append("")

    if "wells" in s:
        w = s["wells"]
        lines.append(f"## Wells, springs & boreholes (GSI; {w['well_count']} within {w['search_radius_m']}m)")
        if w["shallowest_water_strike_m"] is not None:
            lines.append(f"- Shallowest recorded water-strike depth nearby: {w['shallowest_water_strike_m']}m "
                          f"({w['wells_with_depth_count']} of {w['well_count']} well(s) have a recorded depth)")
        else:
            lines.append(f"- No depth-to-water data recorded at any of the {w['well_count']} well(s)/spring(s) nearby")
        for well in sorted(w["wells"], key=lambda x: (x["water_strike_m"] is None, x["water_strike_m"]))[:8]:
            depth_label = f"{well['water_strike_m']}m water strike" if well["water_strike_m"] is not None else "no recorded depth"
            lines.append(f"- {well['gsi_ref']} ({well['source_type'] or 'unknown type'}): {depth_label}")
        lines.append(f"> {w['caveat']}")
        for source_type, est in (w.get("depth_estimates") or {}).items():
            if est.get("estimated_depth_m") is not None:
                lines.append(f"- Estimated {source_type.lower()} water-strike depth: ~{est['estimated_depth_m']}m "
                              f"(from {est['sample_count']} real record(s) within {est['search_radius_m']}m, "
                              f"range {est['min_depth_m']}-{est['max_depth_m']}m)")
            else:
                lines.append(f"- {est.get('note')}")
        lines.append("")

    if "biodiversity" in s:
        b = s["biodiversity"]
        lines.append(f"## Species records (GBIF; {b['total_record_count']} occurrence record(s) within {b['search_radius_km']}km)")
        if b["threatened_species"]:
            lines.append(f"⚠ {b['threatened_count']} threatened species (IUCN Red List) recorded nearby:")
            for sp in b["threatened_species"][:8]:
                lines.append(f"- {sp['species']} ({sp['common_name'] or 'no common name'}) — {sp['category_label']}, last observed {sp['last_observed']}")
        else:
            lines.append("No threatened (IUCN VU/EN/CR) species recorded nearby")
        lines.append(f"> {b['caveat']}")
        lines.append("")

    if "terrain" in s:
        t = s["terrain"]
        lines.append("## Terrain & elevation")
        p = t["precise"]
        if p["found"]:
            lines.append(f"- Ground elevation (OPW LIDAR, {p['resolution_m']}m, surveyed {p['survey_date']}): {p['ground_elevation_m']}m")
            if p["surface_elevation_m"] is not None:
                lines.append(f"- Surface elevation (incl. vegetation/buildings): {p['surface_elevation_m']}m (+{p['canopy_or_building_height_m']}m above ground)")
        else:
            lines.append("- No precise LIDAR elevation at this point (OPW's survey concentrates on rivers/floodplains/coasts)")
        c = t["contours"]
        if c["elevation_range_m"]:
            lines.append(f"- Nearby contours ({c['contour_count']} within {c['search_radius_m']}m): {c['elevation_range_m'][0]}-{c['elevation_range_m'][1]}m")
        else:
            lines.append(f"- No contour lines within {c['search_radius_m']}m")
        lines.append(f"> {t['caveat']}")
        lines.append("")

    if "utilities" in s:
        u = s["utilities"]
        lines.append("## Utilities (request-based)")
        lines.append(f"- Electricity: {u['electricity']['to']}")
        lines.append(f"- Water/wastewater: {u['water_wastewater']['to']}")
        lines.append("")
        grid = u.get("grid")
        if grid:
            lines.append("## Transmission grid (EirGrid)")
            ns = grid["nearest_station"]
            if ns:
                lines.append(f"- Nearest substation: {ns['name']} ({ns['voltage']}), ~{ns['distance_m']}m away")
            else:
                lines.append(f"- No substation within {grid['station_search_radius_m']}m")
            lines.append(f"- {len(grid['lines'])} overhead line(s), {len(grid['cables'])} underground cable(s) within {grid['line_search_radius_m']}m")
            if grid["likely_crossing"]:
                lines.append("- ⚠ A transmission line or cable likely crosses or closely borders this site")
            if grid["committed_stations"] or grid["committed_lines"]:
                lines.append(f"- {len(grid['committed_stations'])} committed (planned) station(s), {len(grid['committed_lines'])} committed line(s)/cable(s) nearby")
            lines.append(f"> {grid['caveat']}")
            lines.append("")

    if "planning" in s:
        p = s["planning"]
        lines.append("## Planning context")
        for k, v in p.items():
            if k != "note":
                lines.append(f"- {k}: {v}")
        lines.append("")

    return "\n".join(lines)
