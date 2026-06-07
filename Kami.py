#!/usr/bin/env python3
"""
╔═══════════════════════════════════════════════════════════════╗
║  KAMI - OCI Compute Service Limits Dashboard                  ║
║  Displays Compute shapes usage & limits per Availability      ║
║  Domain across all regions in your OCI tenancy.              ║
╚═══════════════════════════════════════════════════════════════╝

Usage (OCI Cloud Shell):
    python3 kami.py
    python3 kami.py --region eu-frankfurt-1
    python3 kami.py --all-regions
    python3 kami.py --format table     (default)
    python3 kami.py --format csv
"""

import oci
import argparse
import sys
import csv
import io
from datetime import datetime

# ──────────────────────────────────────────────────────────────
# ANSI colours (disabled automatically when piped / CSV mode)
# ──────────────────────────────────────────────────────────────
def supports_color():
    return sys.stdout.isatty()

class C:
    RESET   = "\033[0m"    if supports_color() else ""
    BOLD    = "\033[1m"    if supports_color() else ""
    CYAN    = "\033[96m"   if supports_color() else ""
    GREEN   = "\033[92m"   if supports_color() else ""
    YELLOW  = "\033[93m"   if supports_color() else ""
    RED     = "\033[91m"   if supports_color() else ""
    MAGENTA = "\033[95m"   if supports_color() else ""
    BLUE    = "\033[94m"   if supports_color() else ""
    DIM     = "\033[2m"    if supports_color() else ""

# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────
def bar(used, limit, width=20):
    """Visual usage bar."""
    if limit == 0:
        return f"{'─' * width}"
    pct = min(used / limit, 1.0)
    filled = int(pct * width)
    empty  = width - filled
    color  = C.GREEN if pct < 0.70 else (C.YELLOW if pct < 0.90 else C.RED)
    return f"{color}{'█' * filled}{C.DIM}{'░' * empty}{C.RESET}"

def pct_label(used, limit):
    if limit == 0:
        return f"{C.DIM}  N/A{C.RESET}"
    pct = used / limit * 100
    color = C.GREEN if pct < 70 else (C.YELLOW if pct < 90 else C.RED)
    return f"{color}{pct:5.1f}%{C.RESET}"

def banner():
    print(f"""
{C.CYAN}{C.BOLD}╔══════════════════════════════════════════════════════════════════╗
║   K A M I  ·  OCI Compute Service Limits Dashboard              ║
║   {C.RESET}{C.DIM}Surfacing your tenant limits, one shape at a time{C.RESET}{C.CYAN}{C.BOLD}             ║
╚══════════════════════════════════════════════════════════════════╝{C.RESET}
  Generated : {C.YELLOW}{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC{C.RESET}
""")

# ──────────────────────────────────────────────────────────────
# OCI data fetching
# ──────────────────────────────────────────────────────────────
def get_config_and_signer():
    """
    Cloud Shell auth (confirmed via env vars):
      - Config file : /etc/oci/config          (OCI_CLI_CONFIG_FILE)
      - Token file  : /etc/oci/delegation_token (OCI_DELEGATION_TOKEN_FILE)
      - Auth type   : instance_obo_user         (OCI_CLI_AUTH)
      Uses InstancePrincipalsDelegationTokenSigner — Instance Principal
      acting On Behalf Of the logged-in user.
    """
    import os

    # ── Priority 1: Cloud Shell OBO delegation token ──────────────
    token_file = (
        os.environ.get("OCI_DELEGATION_TOKEN_FILE") or
        os.environ.get("OCI_obo_token_path") or
        "/etc/oci/delegation_token"
    )
    config_file = (
        os.environ.get("OCI_CLI_CONFIG_FILE") or
        os.environ.get("OCI_CONFIG_FILE") or
        "/etc/oci/config"
    )

    if os.path.exists(token_file) and os.path.exists(config_file):
        try:
            cfg = oci.config.from_file(file_location=config_file)
            with open(token_file) as f:
                delegation_token = f.read().strip()
            signer = oci.auth.signers.InstancePrincipalsDelegationTokenSigner(
                delegation_token=delegation_token
            )
            # Override tenancy from env if available (more reliable)
            tenancy = os.environ.get("OCI_TENANCY") or cfg.get("tenancy") or signer.tenancy_id
            region  = os.environ.get("OCI_REGION")  or cfg.get("region")  or signer.region
            cfg["tenancy"] = tenancy
            cfg["region"]  = region
            print(f"{C.DIM}  Auth : Cloud Shell (instance_obo_user delegation token){C.RESET}")
            return cfg, signer
        except Exception as e:
            print(f"{C.DIM}  OBO delegation attempt failed: {e}{C.RESET}")

    # ── Priority 2: ~/.oci/config with API key (local dev) ────────
    try:
        cfg = oci.config.from_file()
        oci.config.validate_config(cfg)
        print(f"{C.DIM}  Auth : ~/.oci/config (API Key){C.RESET}")
        return cfg, None
    except Exception:
        pass

    # ── Priority 3: bare Instance Principal ───────────────────────
    try:
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        cfg = {"region": signer.region, "tenancy": signer.tenancy_id}
        print(f"{C.DIM}  Auth : Instance Principal{C.RESET}")
        return cfg, signer
    except Exception as e:
        print(f"{C.RED}ERROR: Cannot authenticate with OCI.\n{e}{C.RESET}")
        sys.exit(1)

def list_regions(limits_client, tenancy_id):
    """List subscribed regions via Limits API (no Identity permission needed)."""
    # The limits endpoint itself tells us which regions exist via the tenancy
    # Best approach without Identity: use well-known OCI region list or
    # rely on the user passing --region. For --all-regions we still need identity.
    # We fall back to identity but catch the error gracefully.
    try:
        import oci as _oci
        id_client = _oci.identity.IdentityClient(
            config=limits_client._config if hasattr(limits_client, "_config") else {},
            signer=limits_client.base_client.signer
        )
        id_client.base_client.set_region(
            limits_client.base_client.endpoint.split(".")[1]
            if "." in limits_client.base_client.endpoint else "us-ashburn-1"
        )
        subscribed = id_client.list_region_subscriptions(tenancy_id).data
        return [r.region_name for r in subscribed]
    except Exception:
        # Cannot list regions without Identity — return current region only
        return []

def list_ads_from_limits(limits_client, tenancy_id):
    """
    Discover AD names without calling the Identity API.
    We list limit definitions scoped to 'AD' and extract unique AD names
    from the availability values. This avoids needing Identity permissions.
    """
    ads = set()
    try:
        # list_limit_values with scope_type=AD returns one entry per AD
        resp = oci.pagination.list_call_get_all_results(
            limits_client.list_limit_values,
            compartment_id=tenancy_id,
            service_name="compute",
            scope_type="AD"
        ).data
        for item in resp:
            if item.availability_domain:
                ads.add(item.availability_domain)
    except Exception:
        pass

    if not ads:
        # Fallback: try well-known AD naming patterns
        # This is a last resort — real names come from the API
        pass

    return sorted(ads)

def list_ads(identity_client, tenancy_id, region_name):
    ads = identity_client.list_availability_domains(tenancy_id).data
    return [ad.name for ad in ads]

def fetch_compute_limits(limits_client, tenancy_id, ad_name,
                         _region_values_cache={}):
    """
    Fetch compute limit values scoped to a given AD.
    Uses list_limit_values then get_resource_availability in parallel.
    Returns dict: { resource_name -> {limit, used, scope} }

    Optimisations vs original:
      1. REGION-scoped list_limit_values is fetched once per process and cached
         (it is identical for every AD in the same region).
      2. get_resource_availability calls are fired in parallel via a
         ThreadPoolExecutor (up to 20 workers) instead of sequentially.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # ── Step 1 : AD-scoped limit list ────────────────────────────
    try:
        ad_values = oci.pagination.list_call_get_all_results(
            limits_client.list_limit_values,
            compartment_id=tenancy_id,
            service_name="compute",
            scope_type="AD",
            availability_domain=ad_name
        ).data
    except oci.exceptions.ServiceError:
        ad_values = []

    # ── Step 2 : REGION-scoped limit list (cached across AD calls) ─
    # Key by the client's endpoint so different regions get their own cache
    cache_key = limits_client.base_client.endpoint
    if cache_key not in _region_values_cache:
        try:
            _region_values_cache[cache_key] = oci.pagination.list_call_get_all_results(
                limits_client.list_limit_values,
                compartment_id=tenancy_id,
                service_name="compute",
                scope_type="REGION"
            ).data
        except oci.exceptions.ServiceError:
            _region_values_cache[cache_key] = []

    region_values = _region_values_cache[cache_key]

    all_values = [(v, "AD") for v in ad_values] + [(v, "REGION") for v in region_values]

    # ── Step 3 : parallel get_resource_availability ───────────────
    def _fetch_one(lv, scope):
        name      = lv.name
        limit_val = lv.value if lv.value is not None else 0
        try:
            av_resp = limits_client.get_resource_availability(
                compartment_id=tenancy_id,
                service_name="compute",
                limit_name=name,
                availability_domain=ad_name if scope == "AD" else None
            )
            av = av_resp.data
            available = av.available if av.available is not None else limit_val
            used = max(int(limit_val) - int(available), 0)
        except oci.exceptions.ServiceError:
            used = 0
        return name, {"limit": int(limit_val), "used": used, "scope": scope}

    results = {}
    # 20 workers: enough parallelism without hammering the rate limiter
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = {pool.submit(_fetch_one, lv, scope): (lv, scope)
                   for lv, scope in all_values}
        for future in as_completed(futures):
            try:
                name, info = future.result()
                results[name] = info
            except Exception:
                pass  # individual failures are silently skipped

    return results

# ──────────────────────────────────────────────────────────────
# Display
# ──────────────────────────────────────────────────────────────
SHAPE_KEYWORDS = [
    "standard", "flex", "dense", "high-memory", "optimized",
    "gpu", "hpc", "bm.", "vm.", "e2.", "e3.", "e4.", "e5.",
    "a1.", "a2.", "x9.", "x7.", "x6.", "-ocpu", "-memory",
]

def is_shape_related(name):
    n = name.lower()
    return any(kw in n for kw in SHAPE_KEYWORDS)

def print_region_table(region, ads_data):
    """
    ads_data = { ad_name: { resource_name: {limit, used, scope} } }
    """
    print(f"\n{C.BOLD}{C.BLUE}{'═'*70}{C.RESET}")
    print(f"{C.BOLD}{C.BLUE}  Region : {C.CYAN}{region}{C.RESET}")
    print(f"{C.BOLD}{C.BLUE}{'═'*70}{C.RESET}")

    # Collect all resource names that appear in any AD and are shape-related
    all_names = set()
    for ad_data in ads_data.values():
        for name in ad_data:
            if is_shape_related(name):
                all_names.add(name)
    all_names = sorted(all_names)

    if not all_names:
        print(f"  {C.YELLOW}No compute shape limits found for this region.{C.RESET}\n")
        return

    ad_list = sorted(ads_data.keys())

    # Header row
    col_w = 36
    ad_col_w = 26
    header = f"  {C.BOLD}{'Resource / Shape Limit':<{col_w}}{C.RESET}"
    for ad in ad_list:
        short_ad = ad.split(":")[-1]  # e.g. AD-1
        header += f"  {C.BOLD}{C.MAGENTA}{short_ad:^{ad_col_w}}{C.RESET}"
    print(header)
    print(f"  {'─'*col_w}" + (f"  {'─'*ad_col_w}" * len(ad_list)))

    for name in all_names:
        row = f"  {C.CYAN}{name:<{col_w}}{C.RESET}"
        for ad in ad_list:
            ad_data = ads_data.get(ad, {})
            info = ad_data.get(name)
            if info is None:
                row += f"  {'─':^{ad_col_w}}"
            else:
                used  = info["used"]
                limit = info["limit"]
                scope = info["scope"]
                if scope == "REGION":
                    cell = f"REGION: {used}/{limit}"
                else:
                    cell = f"{used:>4} / {limit:<6} {pct_label(used, limit)}"
                row += f"  {cell}"
        print(row)

    print()

def print_region_table_detailed(region, ads_data):
    """Per-AD detailed breakdown with visual bars."""
    print(f"\n{C.BOLD}{C.BLUE}{'═'*68}{C.RESET}")
    print(f"{C.BOLD}{C.BLUE}  Region : {C.CYAN}{region}{C.RESET}")
    print(f"{C.BOLD}{C.BLUE}{'═'*68}{C.RESET}")

    for ad_name in sorted(ads_data.keys()):
        short = ad_name.split(":")[-1]
        print(f"\n  {C.BOLD}{C.MAGENTA}▶ Availability Domain : {short}{C.RESET}")
        print(f"  {'─'*64}")
        fmt = f"  {{:<38}} {{:>5}} / {{:<6}}  {{}}  {{}}"
        print(f"  {C.BOLD}{'Resource':<38} {'Used':>5}   {'Limit':<6}  {'Bar':<22}  {'%'}{C.RESET}")
        print(f"  {'─'*64}")

        ad_data = ads_data[ad_name]
        found = False
        for name in sorted(ad_data.keys()):
            if not is_shape_related(name):
                continue
            found = True
            info  = ad_data[name]
            used  = info["used"]
            limit = info["limit"]
            b     = bar(used, limit)
            p     = pct_label(used, limit)
            print(f"  {C.CYAN}{name:<38}{C.RESET} {used:>5} / {limit:<6}  {b}  {p}")

        if not found:
            print(f"  {C.DIM}  (no shape limits found){C.RESET}")

    print()

def export_csv(region, ads_data):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Region", "Availability Domain", "Resource", "Used", "Limit", "Pct Used", "Scope"])

    for ad_name in sorted(ads_data.keys()):
        def csv_pct(n):
            v = ads_data[ad_name][n]
            if not v["limit"]: return -1
            return v["used"] / v["limit"]
        sorted_names = sorted(
            [n for n in ads_data[ad_name] if is_shape_related(n)],
            key=csv_pct, reverse=True
        )
        for name in sorted_names:
            if not is_shape_related(name):
                continue
            info  = ads_data[ad_name][name]
            used  = info["used"]
            limit = info["limit"]
            pct   = round(used / limit * 100, 1) if limit else 0
            writer.writerow([region, ad_name, name, used, limit, pct, info["scope"]])

    return output.getvalue()

# ──────────────────────────────────────────────────────────────
# Main

# ──────────────────────────────────────────────────────────────
# HTML Renderer
# ──────────────────────────────────────────────────────────────
def render_html(all_html_data, tenancy_id, output_path):
    """Render a self-contained HTML dashboard from collected limits data."""
    from datetime import datetime

    def pct(used, limit):
        if not limit: return 0
        return round(min(used / limit * 100, 100), 1)

    def bar_color(p):
        if p == 0:    return "#2a3a4a"
        if p < 70:    return "#00c896"
        if p < 90:    return "#f0b429"
        return "#ff4d4d"

    def badge_class(p):
        if p == 0:    return "zero"
        if p < 70:    return "ok"
        if p < 90:    return "warn"
        return "crit"

    # Build region/AD/limit rows — tab switcher when multiple regions
    num_regions  = len(all_html_data)
    region_slugs = []   # used for tab IDs

    def make_ad_blocks(ads_data):
        ad_blocks = ""
        for ad_name in sorted(ads_data.keys()):
            short_ad = ad_name.split(":")[-1]
            rows = ""
            def util_pct(item):
                v = item[1]
                if not v["limit"]: return -1
                return v["used"] / v["limit"]
            items = sorted(
                [(n, v) for n, v in ads_data[ad_name].items() if is_shape_related(n)],
                key=util_pct, reverse=True
            )
            if not items:
                rows = '<tr><td colspan="5" class="empty">No shape limits found</td></tr>'
            for name, info in items:
                u  = info["used"]
                l  = info["limit"]
                p  = pct(u, l)
                bc = bar_color(p)
                badge   = badge_class(p)
                pct_str = f"{p}%" if l else "N/A"
                bar_w   = f"{p}%" if l else "0%"
                rows += f"""
                <tr data-shape="{name.lower()}" data-pct="{p}">
                  <td class="res-name">{name}</td>
                  <td class="num">{u}</td>
                  <td class="num">{l if l else "—"}</td>
                  <td class="bar-cell">
                    <div class="bar-track">
                      <div class="bar-fill" style="width:{bar_w};background:{bc}"></div>
                    </div>
                  </td>
                  <td class="pct-cell"><span class="pct-badge {badge}">{pct_str}</span></td>
                </tr>"""
            ad_blocks += f"""
          <div class="ad-block">
            <div class="ad-header">
              <span class="ad-icon">◈</span>
              <span class="ad-name">{short_ad}</span>
              <span class="ad-count">{len(items)} limits</span>
            </div>
            <table class="limits-table">
              <thead>
                <tr>
                  <th>Resource / Shape</th>
                  <th class="num">Used</th>
                  <th class="num">Limit</th>
                  <th>Utilization</th>
                  <th>%</th>
                </tr>
              </thead>
              <tbody>{rows}</tbody>
            </table>
          </div>"""
        return ad_blocks

    if num_regions == 1:
        # ── Single region: original stacked layout ────────────────
        region, ads_data = all_html_data[0]
        ad_blocks = make_ad_blocks(ads_data)
        regions_html = f"""
        <section class="region-section">
          <div class="region-header">
            <div class="region-title">
              <span class="region-dot"></span>
              <h2>{region}</h2>
            </div>
            <span class="region-ad-count">{len(ads_data)} Availability Domain(s)</span>
          </div>
          <div class="ad-grid">{ad_blocks}</div>
        </section>"""
    else:
        # ── Multi-region: tab switcher layout ─────────────────────
        tab_buttons = ""
        tab_panels  = ""
        for idx, (region, ads_data) in enumerate(all_html_data):
            slug      = region.replace(".", "-").replace("_", "-").lower()
            region_slugs.append(slug)
            active_btn   = "active" if idx == 0 else ""
            active_panel = "active" if idx == 0 else ""
            total_shapes = sum(
                len([n for n in v if is_shape_related(n)])
                for v in ads_data.values()
            )
            tab_buttons += f"""
              <button class="region-tab {active_btn}" data-tab="{slug}">
                <span class="tab-dot"></span>
                <span class="tab-label">{region.upper()}</span>
                <span class="tab-badge">{len(ads_data)} AD · {total_shapes} shapes</span>
              </button>"""
            ad_blocks = make_ad_blocks(ads_data)
            tab_panels += f"""
          <div class="region-panel {active_panel}" id="panel-{slug}">
            <div class="ad-grid">{ad_blocks}</div>
          </div>"""

        regions_html = f"""
        <div class="region-tabs-wrap">
          <div class="region-tabs">
            {tab_buttons}
          </div>
          {tab_panels}
        </div>"""

    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    short_tid = tenancy_id[:24] + "…" if len(tenancy_id) > 24 else tenancy_id

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>KAMI — OCI Compute Limits Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Rajdhani:wght@400;600;700&family=Exo+2:wght@300;400;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:        #070d14;
    --bg2:       #0c1520;
    --bg3:       #111d2a;
    --border:    #1a2e42;
    --border2:   #243d56;
    --accent:    #00e5ff;
    --accent2:   #00c896;
    --text:      #c8dae8;
    --text-dim:  #4a6a82;
    --text-head: #e8f4ff;
    --ok:        #00c896;
    --warn:      #f0b429;
    --crit:      #ff4d4d;
    --zero:      #2a3a4a;
    --font-mono: 'Share Tech Mono', monospace;
    --font-ui:   'Rajdhani', sans-serif;
    --font-body: 'Exo 2', sans-serif;
  }}

  * {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    background: var(--bg);
    color: var(--text);
    font-family: var(--font-body);
    font-size: 13px;
    line-height: 1.5;
    min-height: 100vh;
  }}

  /* ── Grid background ── */
  body::before {{
    content: '';
    position: fixed; inset: 0; z-index: 0;
    background-image:
      linear-gradient(rgba(0,229,255,.03) 1px, transparent 1px),
      linear-gradient(90deg, rgba(0,229,255,.03) 1px, transparent 1px);
    background-size: 40px 40px;
    pointer-events: none;
  }}

  .wrap {{ position: relative; z-index: 1; max-width: 1400px; margin: 0 auto; padding: 0 24px 60px; }}

  /* ── Header ── */
  .site-header {{
    display: flex; align-items: center; justify-content: space-between;
    padding: 32px 0 24px;
    border-bottom: 1px solid var(--border2);
    margin-bottom: 32px;
  }}
  .logo-block {{ display: flex; align-items: center; gap: 18px; }}
  .logo-glyph {{
    width: 52px; height: 52px;
    background: linear-gradient(135deg, #001a2e, #003050);
    border: 1px solid var(--accent);
    border-radius: 8px;
    display: flex; align-items: center; justify-content: center;
    font-family: var(--font-mono);
    color: var(--accent);
    font-size: 20px;
    box-shadow: 0 0 20px rgba(0,229,255,.2), inset 0 0 10px rgba(0,229,255,.05);
  }}
  .logo-text h1 {{
    font-family: var(--font-ui);
    font-size: 26px; font-weight: 700; letter-spacing: 6px;
    color: var(--text-head);
    text-transform: uppercase;
  }}
  .logo-text p {{
    font-family: var(--font-mono);
    font-size: 11px; color: var(--text-dim); letter-spacing: 2px;
    text-transform: uppercase; margin-top: 2px;
  }}
  .header-meta {{
    text-align: right;
    font-family: var(--font-mono);
    font-size: 11px; color: var(--text-dim); line-height: 1.8;
  }}
  .header-meta .ts {{ color: var(--accent); }}
  .header-meta .tid {{ color: var(--text-dim); font-size: 10px; }}

  /* ── Search bar ── */
  .search-wrap {{
    flex: 1;
    display: flex;
    justify-content: center;
    padding: 0 32px;
  }}
  .search-box {{
    position: relative;
    width: 100%;
    max-width: 520px;
  }}
  .search-box input {{
    width: 100%;
    background: rgba(0,229,255,.04);
    border: 1px solid var(--accent);
    border-radius: 5px;
    color: var(--text);
    font-family: var(--font-mono);
    font-size: 12px;
    letter-spacing: 1px;
    padding: 8px 36px 8px 14px;
    outline: none;
    transition: box-shadow .2s, background .2s;
    box-shadow: 0 0 0 0 rgba(0,229,255,0);
  }}
  .search-box input::placeholder {{
    color: var(--text-dim);
    letter-spacing: 1.5px;
  }}
  .search-box input:focus {{
    background: rgba(0,229,255,.07);
    box-shadow: 0 0 12px rgba(0,229,255,.25);
  }}
  .search-box .search-icon {{
    position: absolute;
    right: 11px; top: 50%;
    transform: translateY(-50%);
    color: var(--accent);
    font-size: 13px;
    pointer-events: none;
    opacity: .7;
  }}
  .search-box .clear-btn {{
    position: absolute;
    right: 11px; top: 50%;
    transform: translateY(-50%);
    color: var(--text-dim);
    font-size: 16px;
    cursor: pointer;
    display: none;
    background: none;
    border: none;
    line-height: 1;
    padding: 0;
  }}
  .search-box .clear-btn:hover {{ color: var(--accent); }}
  .search-count {{
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--text-dim);
    text-align: center;
    margin-top: 5px;
    min-height: 14px;
    letter-spacing: 1px;
  }}
  .search-count span {{ color: var(--accent); }}

  /* ── Region section ── */
  .region-section {{
    margin-bottom: 48px;
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 12px;
    overflow: hidden;
  }}
  .region-header {{
    display: flex; align-items: center; justify-content: space-between;
    padding: 16px 24px;
    background: linear-gradient(90deg, #001525, #070d14);
    border-bottom: 1px solid var(--border2);
  }}
  .region-title {{ display: flex; align-items: center; gap: 12px; }}
  .region-dot {{
    width: 10px; height: 10px; border-radius: 50%;
    background: var(--accent);
    box-shadow: 0 0 8px var(--accent);
    animation: pulse 2s infinite;
  }}
  @keyframes pulse {{
    0%,100% {{ opacity: 1; }} 50% {{ opacity: .4; }}
  }}
  .region-title h2 {{
    font-family: var(--font-ui);
    font-size: 18px; font-weight: 600; letter-spacing: 3px;
    color: var(--text-head); text-transform: uppercase;
  }}
  .region-ad-count {{
    font-family: var(--font-mono); font-size: 11px;
    color: var(--text-dim); letter-spacing: 1px;
  }}

  .ad-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(420px, 1fr));
    gap: 0;
  }}

  /* ── AD block ── */
  .ad-block {{
    border-right: 1px solid var(--border);
    border-bottom: 1px solid var(--border);
  }}
  .ad-block:last-child {{ border-right: none; }}
  .ad-header {{
    display: flex; align-items: center; gap: 10px;
    padding: 12px 20px;
    background: linear-gradient(90deg, #0a1822, transparent);
    border-bottom: 1px solid var(--border);
  }}
  .ad-icon {{ color: var(--accent2); font-size: 14px; }}
  .ad-name {{
    font-family: var(--font-ui); font-size: 14px; font-weight: 600;
    letter-spacing: 2px; color: var(--accent2); text-transform: uppercase; flex: 1;
  }}
  .ad-count {{
    font-family: var(--font-mono); font-size: 10px;
    color: var(--text-dim);
    background: rgba(0,200,150,.08);
    border: 1px solid rgba(0,200,150,.2);
    border-radius: 4px; padding: 2px 8px;
  }}

  /* ── Table ── */
  .limits-table {{
    width: 100%; border-collapse: collapse;
    font-family: var(--font-mono); font-size: 12px;
  }}
  .limits-table thead tr {{
    background: rgba(0,229,255,.04);
  }}
  .limits-table th {{
    padding: 8px 12px; text-align: left;
    color: var(--text-dim); font-size: 10px;
    letter-spacing: 1.5px; text-transform: uppercase;
    border-bottom: 1px solid var(--border);
    font-weight: 400;
  }}
  .limits-table th.num {{ text-align: right; }}
  .limits-table tbody tr {{
    border-bottom: 1px solid rgba(26,46,66,.6);
    transition: background .15s;
  }}
  .limits-table tbody tr:hover {{
    background: rgba(0,229,255,.04);
  }}
  .limits-table td {{
    padding: 7px 12px; vertical-align: middle;
  }}
  .limits-table td.num {{ text-align: right; color: var(--text-dim); }}
  .res-name {{ color: var(--text); word-break: break-all; }}
  .empty {{ text-align: center; color: var(--text-dim); padding: 20px; font-style: italic; }}

  /* ── Bar ── */
  .bar-cell {{ width: 120px; }}
  .bar-track {{
    height: 6px; background: rgba(255,255,255,.06);
    border-radius: 3px; overflow: hidden;
  }}
  .bar-fill {{
    height: 100%; border-radius: 3px;
    transition: width .3s ease;
    box-shadow: 0 0 6px currentColor;
  }}

  /* ── % badge ── */
  .pct-cell {{ width: 70px; text-align: right; }}
  .pct-badge {{
    display: inline-block;
    padding: 2px 7px; border-radius: 4px;
    font-size: 11px; font-weight: 600; letter-spacing: .5px;
  }}
  .pct-badge.zero  {{ background: rgba(42,58,74,.4);  color: var(--text-dim); }}
  .pct-badge.ok    {{ background: rgba(0,200,150,.15); color: var(--ok);  border: 1px solid rgba(0,200,150,.3); }}
  .pct-badge.warn  {{ background: rgba(240,180,41,.15); color: var(--warn); border: 1px solid rgba(240,180,41,.3); }}
  .pct-badge.crit  {{ background: rgba(255,77,77,.15);  color: var(--crit);  border: 1px solid rgba(255,77,77,.3); }}

  /* ── Scope tag ── */
  .scope-tag {{
    font-size: 9px; padding: 1px 5px; border-radius: 3px;
    letter-spacing: 1px; text-transform: uppercase;
  }}
  .scope-ad     {{ background: rgba(0,229,255,.1);  color: var(--accent); }}
  .scope-region {{ background: rgba(140,80,255,.1); color: #a78bfa; }}

  /* ── Footer ── */
  .footer {{
    margin-top: 48px; padding-top: 20px;
    border-top: 1px solid var(--border);
    text-align: center;
    font-family: var(--font-mono); font-size: 11px;
    color: var(--text-dim); letter-spacing: 1px;
  }}
  .footer span {{ color: var(--accent); }}

  /* ── Region tab switcher ── */
  .region-tabs-wrap {{
    margin-bottom: 32px;
  }}
  .region-tabs {{
    display: flex;
    gap: 8px;
    margin-bottom: 0;
    border-bottom: 2px solid var(--border2);
    padding-bottom: 0;
  }}
  .region-tab {{
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 12px 22px;
    background: var(--bg2);
    border: 1px solid var(--border);
    border-bottom: 2px solid transparent;
    border-radius: 8px 8px 0 0;
    cursor: pointer;
    font-family: var(--font-ui);
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 2px;
    color: var(--text-dim);
    text-transform: uppercase;
    transition: all .2s;
    position: relative;
    bottom: -2px;
  }}
  .region-tab:hover {{
    background: var(--bg3);
    color: var(--text);
    border-color: var(--border2);
    border-bottom-color: transparent;
  }}
  .region-tab.active {{
    background: linear-gradient(180deg, #001525, #0c1520);
    border-color: var(--border2);
    border-bottom-color: var(--bg2);
    color: var(--accent);
    box-shadow: 0 -2px 12px rgba(0,229,255,.1);
  }}
  .tab-dot {{
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--text-dim);
    transition: background .2s, box-shadow .2s;
  }}
  .region-tab.active .tab-dot {{
    background: var(--accent);
    box-shadow: 0 0 8px var(--accent);
    animation: pulse 2s infinite;
  }}
  .tab-label {{
    letter-spacing: 3px;
  }}
  .tab-badge {{
    font-family: var(--font-mono);
    font-size: 10px;
    font-weight: 400;
    letter-spacing: 1px;
    color: var(--text-dim);
    background: rgba(0,229,255,.06);
    border: 1px solid rgba(0,229,255,.15);
    border-radius: 4px;
    padding: 2px 8px;
    transition: all .2s;
  }}
  .region-tab.active .tab-badge {{
    color: var(--accent);
    background: rgba(0,229,255,.1);
    border-color: rgba(0,229,255,.3);
  }}
  .region-panel {{
    display: none;
    background: var(--bg2);
    border: 1px solid var(--border2);
    border-top: none;
    border-radius: 0 8px 8px 8px;
    overflow: hidden;
  }}
  .region-panel.active {{
    display: block;
  }}
</style>
</head>
<body>
<div class="wrap">

  <header class="site-header">
    <div class="logo-block">
      <div class="logo-glyph">⛩</div>
      <div class="logo-text">
        <h1>KAMI</h1>
        <p>OCI Compute Service Limits Dashboard</p>
      </div>
    </div>
    <div class="search-wrap">
      <div class="search-box">
        <input type="text" id="shapeSearch" placeholder="FILTER SHAPES…" autocomplete="off" spellcheck="false">
        <span class="search-icon" id="searchIcon">⌕</span>
        <button class="clear-btn" id="clearBtn" title="Clear filter">✕</button>
        <div class="search-count" id="searchCount"></div>
      </div>
    </div>
    <div class="header-meta">
      <div class="ts">{ts}</div>
      <div>Tenancy: <span style="color:var(--text)">{short_tid}</span></div>
      <div class="tid">Compute shapes &amp; limits per Availability Domain</div>
    </div>
  </header>

  {regions_html}

  <footer class="footer">
    Generated by <span>KAMI</span> · OCI Compute Limits Dashboard · {ts}
  </footer>

</div>

<script>
(function () {{
  const input     = document.getElementById('shapeSearch');
  const clearBtn  = document.getElementById('clearBtn');
  const searchIcon= document.getElementById('searchIcon');
  const countEl   = document.getElementById('searchCount');

  function applyFilter() {{
    const term = input.value.trim().toLowerCase();

    // Toggle clear button vs search icon
    if (term) {{
      clearBtn.style.display  = 'block';
      searchIcon.style.display = 'none';
    }} else {{
      clearBtn.style.display  = 'none';
      searchIcon.style.display = 'block';
    }}

    let totalVisible = 0;
    let totalRows    = 0;

    // Process every AD block independently
    document.querySelectorAll('.ad-block').forEach(function (block) {{
      const tbody = block.querySelector('tbody');
      if (!tbody) return;

      const allRows = Array.from(tbody.querySelectorAll('tr[data-shape]'));
      totalRows += allRows.length;

      if (!term) {{
        // Restore original order (sort by pct descending — matches Python render order)
        allRows
          .sort(function (a, b) {{
            return parseFloat(b.dataset.pct) - parseFloat(a.dataset.pct);
          }})
          .forEach(function (r) {{
            r.style.display = '';
            tbody.appendChild(r);
          }});
        totalVisible += allRows.length;

        // Restore ad-count badge
        const countBadge = block.querySelector('.ad-count');
        if (countBadge) countBadge.textContent = allRows.length + ' limits';

        // Remove any "no results" row
        const noRes = tbody.querySelector('.filter-empty');
        if (noRes) noRes.remove();
        return;
      }}

      // Filter + sort by % descending
      const matched = allRows.filter(function (r) {{
        return r.dataset.shape.includes(term);
      }});
      const unmatched = allRows.filter(function (r) {{
        return !r.dataset.shape.includes(term);
      }});

      // Sort matched by pct descending
      matched.sort(function (a, b) {{
        return parseFloat(b.dataset.pct) - parseFloat(a.dataset.pct);
      }});

      // Re-append in order: matched visible, unmatched hidden
      matched.forEach(function (r) {{
        r.style.display = '';
        tbody.appendChild(r);
      }});
      unmatched.forEach(function (r) {{
        r.style.display = 'none';
        tbody.appendChild(r);
      }});

      totalVisible += matched.length;

      // Update ad-count badge
      const countBadge = block.querySelector('.ad-count');
      if (countBadge) countBadge.textContent = matched.length + ' / ' + allRows.length + ' limits';

      // Show/hide "no results" placeholder
      let noRes = tbody.querySelector('.filter-empty');
      if (matched.length === 0) {{
        if (!noRes) {{
          noRes = document.createElement('tr');
          noRes.className = 'filter-empty';
          noRes.innerHTML = '<td colspan="5" class="empty">No shapes match <span style="color:var(--accent)">' + input.value.trim() + '</span></td>';
          tbody.appendChild(noRes);
        }} else {{
          noRes.style.display = '';
        }}
      }} else if (noRes) {{
        noRes.style.display = 'none';
      }}
    }});

    // Update global count
    if (term) {{
      countEl.innerHTML = '<span>' + totalVisible + '</span> of ' + totalRows + ' shapes';
    }} else {{
      countEl.textContent = '';
    }}
  }}

  input.addEventListener('input', applyFilter);

  clearBtn.addEventListener('click', function () {{
    input.value = '';
    applyFilter();
    input.focus();
  }});
}})();

// ── Region tab switcher ───────────────────────────────────────
(function () {{
  var tabs   = document.querySelectorAll('.region-tab');
  var panels = document.querySelectorAll('.region-panel');
  if (!tabs.length) return;

  tabs.forEach(function (tab) {{
    tab.addEventListener('click', function () {{
      var target = tab.dataset.tab;
      tabs.forEach(function (t) {{ t.classList.remove('active'); }});
      panels.forEach(function (p) {{ p.classList.remove('active'); }});
      tab.classList.add('active');
      var panel = document.getElementById('panel-' + target);
      if (panel) panel.classList.add('active');
    }});
  }});
}})();
</script>

</body>
</html>"""

    with open(output_path, "w") as f:
        f.write(html)

# ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="KAMI – OCI Compute Service Limits Dashboard"
    )
    parser.add_argument("--region",      help="Target a specific region (e.g. eu-frankfurt-1)")
    parser.add_argument("--regions",     help="Comma-separated list of regions (e.g. eu-frankfurt-1,us-ashburn-1)")
    parser.add_argument("--all-regions", action="store_true", help="Query all subscribed regions")
    parser.add_argument("--format",      choices=["table", "detailed", "csv", "html", "all"], default="all",
                        help="Output format: table | detailed | csv | html | all (default: all — HTML + CSV)")
    # Timestamped filenames are generated at runtime (see below)
    parser.add_argument("--output", default=None,
                        help="Output filename for HTML report (default: kami_YYYY-MM-DD-HH-MM.html)")
    parser.add_argument("--csv-output", default=None,
                        help="Output filename for CSV report (default: kami_YYYY-MM-DD-HH-MM.csv)")
    args = parser.parse_args()

    banner()
    _start_time = datetime.utcnow()

    cfg, signer = get_config_and_signer()

    # Build base clients
    def make_client(cls, region=None):
        target_region = region or cfg.get("region", "us-ashburn-1")
        if signer:
            # OBO delegation signer: pass full cfg so SDK has tenancy/region context
            c = cls(config=cfg, signer=signer)
        else:
            c = cls(config=cfg)
        c.base_client.set_region(target_region)
        return c

    tenancy_id = cfg.get("tenancy")
    if not tenancy_id:
        print(f"{C.RED}ERROR: Could not determine tenancy OCID. Check your OCI config.{C.RESET}")
        sys.exit(1)

    # Determine regions to query
    home = cfg.get("region", "us-ashburn-1")
    if args.regions:
        regions = [r.strip() for r in args.regions.split(",") if r.strip()]
    elif args.region:
        regions = [args.region]
    elif args.all_regions:
        # Need a limits client pointed at home region to try listing regions
        tmp_lim = make_client(oci.limits.LimitsClient, home)
        regions = list_regions(tmp_lim, tenancy_id)
        if not regions:
            print(f"{C.YELLOW}  ⚠ Could not list all regions (Identity permission needed).{C.RESET}")
            print(f"{C.YELLOW}    Falling back to current region: {home}{C.RESET}\n")
            regions = [home]
    else:
        regions = [home]

    # Resolve timestamped output filenames
    ts_stamp = datetime.utcnow().strftime("%Y-%m-%d-%H-%M")
    html_path = args.output    or f"kami_{ts_stamp}.html"
    csv_path  = args.csv_output or f"kami_{ts_stamp}.csv"

    print(f"  Tenant   : {C.YELLOW}{tenancy_id}{C.RESET}")
    print(f"  Regions  : {C.YELLOW}{', '.join(regions)}{C.RESET}")
    print(f"  Mode     : {C.YELLOW}Compute shapes only{C.RESET}")
    print(f"  HTML out : {C.CYAN}{html_path}{C.RESET}")
    print(f"  CSV out  : {C.CYAN}{csv_path}{C.RESET}\n")


    all_csv_rows = []
    all_html_data = []  # list of (region, ads_data)
    # 'all' = default mode: produce both HTML and CSV

    for region in regions:
        print(f"  {C.DIM}Fetching data for {region} …{C.RESET}", end="\r", flush=True)

        # Only need Limits client — no Identity API calls required
        lim_client = make_client(oci.limits.LimitsClient, region)

        # Discover ADs via Limits API — no Identity permissions needed
        ad_names = list_ads_from_limits(lim_client, tenancy_id)
        if not ad_names:
            print(f"\n  {C.YELLOW}⚠ Could not discover ADs for {region} via Limits API{C.RESET}")
            # Last resort: use a single placeholder so we still get REGION-scoped limits
            ad_names = ["REGION"]
        else:
            print(f"  {C.DIM}  Found {len(ad_names)} AD(s): {', '.join(a.split(':')[-1] for a in ad_names)}{C.RESET}")

        ads_data = {}
        for i, ad in enumerate(ad_names, 1):
            short = ad.split(":")[-1]
            print(f"  {C.DIM}  Scanning {short} ({i}/{len(ad_names)}) …{C.RESET}", end="\r", flush=True)
            try:
                ads_data[ad] = fetch_compute_limits(lim_client, tenancy_id, ad)
            except oci.exceptions.ServiceError as e:
                print(f"\n  {C.YELLOW}⚠ Skipping {ad}: {e.message}{C.RESET}")
                ads_data[ad] = {}

        print(" " * 60, end="\r")  # clear the "fetching" line

        if args.format in ("csv", "all"):
            all_csv_rows.append(export_csv(region, ads_data))
        if args.format in ("html", "all"):
            all_html_data.append((region, ads_data))
        if args.format == "table":
            print_region_table(region, ads_data)
        elif args.format == "detailed":
            print_region_table_detailed(region, ads_data)

    if args.format in ("csv", "all"):
        with open(csv_path, "w") as cf:
            cf.write("Region,Availability Domain,Resource,Used,Limit,Pct Used,Scope\n")
            for block in all_csv_rows:
                lines = block.strip().splitlines()
                cf.write("\n".join(lines[1:]) + "\n")
        print(f"  {C.GREEN}✓ CSV  saved → {C.BOLD}{csv_path}{C.RESET}")

    if args.format in ("html", "all"):
        render_html(all_html_data, tenancy_id, html_path)
        print(f"  {C.GREEN}✓ HTML saved → {C.BOLD}{html_path}{C.RESET}")

    _elapsed = datetime.utcnow() - _start_time
    _mins, _secs = divmod(int(_elapsed.total_seconds()), 60)
    _elapsed_str = f"{_mins}m {_secs}s" if _mins else f"{_secs}s"

    print(f"\n{C.DIM}  ✓ KAMI scan complete.{C.RESET}")
    print(f"  {C.DIM}⏱  Execution time : {C.RESET}{C.YELLOW}{_elapsed_str}{C.RESET}")
    print(f"  {C.DIM}📄  HTML           : {C.RESET}{C.CYAN}{html_path}{C.RESET}" if args.format in ("html","all") else "", end="")
    print(f"  {C.DIM}📄  CSV            : {C.RESET}{C.CYAN}{csv_path}{C.RESET}\n" if args.format in ("csv","all") else "\n", end="")


if __name__ == "__main__":
    main()
