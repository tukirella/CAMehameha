#!/usr/bin/env python3
"""
LoadBalancer Cleanse - Android16
Eliminate low-power and abandoned OCI Load Balancers across all compartments

Requires: oci-python-sdk (pip install oci)
Optional: oci-cli (only for your wider CAM toolbox, not required by this script)
"""

import argparse
import csv
import html
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    import oci
    from oci.config import from_file
except ImportError:
    print("❌ OCI Python SDK not found. Please install it:")
    print("   pip install oci")
    sys.exit(1)


# Color Styling
class Colors:
    HEADER = '\033[96m'
    SUCCESS = '\033[92m'
    WARNING = '\033[93m'
    ERROR = '\033[91m'
    INFO = '\033[94m'
    GHOST = '\033[95m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'


def print_colored(message: str, color: str = Colors.ENDC, end: str = '\n'):
    """Print colored output to console"""
    print(f"{color}{message}{Colors.ENDC}", end=end)


SUSPICIOUS_SCORE_THRESHOLD = 40
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")


def escape_html(value: Any) -> str:
    """Escape values before embedding them in the generated HTML report."""
    return html.escape("" if value is None else str(value), quote=True)


def sanitize_csv_value(value: Any) -> Any:
    """Prevent spreadsheet formula injection when CSVs are opened in Excel."""
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return value
    text = str(value)
    if text.startswith(CSV_FORMULA_PREFIXES):
        return f"'{text}"
    return text


def model_value(model: Any, attr_name: str, default: Any = None) -> Any:
    """Read an attribute from an OCI model or dict without assuming its shape."""
    if isinstance(model, dict):
        return model.get(attr_name, default)
    return getattr(model, attr_name, default)


def normalize_health_status(status: Any) -> str:
    """Normalize OCI health strings for scoring."""
    return str(status or "UNKNOWN").upper()


def show_banner():
    """Display banner"""
    print_colored("╔═══════════════════════════════════════════════════════════════════════════════╗", Colors.HEADER)
    print_colored("║                         🔍 LB Cleanse - Android 16 🔍                         ║", Colors.HEADER)
    print_colored("║                     Hunt down those forgotten Load Balancers!                 ║", Colors.HEADER)
    print_colored("╚═══════════════════════════════════════════════════════════════════════════════╝", Colors.HEADER)
    print()


class OCILoadBalancerGhostHunter:
    def __init__(self, config_file: Optional[str] = None, profile: str = "DEFAULT"):
        """Initialize the LoadBalancer Cleanse"""
        self.config_file = config_file or "~/.oci/config"
        self.profile = profile
        self.config = from_file(self.config_file, profile)
        self.current_region = self.config.get("region", "Unknown")
        self._configure_clients(self.current_region)

        self.tenancy_id = self.config["tenancy"]
        try:
            tenancy = self.identity_client.get_tenancy(self.tenancy_id)
            self.tenancy_name = tenancy.data.name
        except Exception as e:
            print_colored(f"⚠️ Could not get tenancy name: {e}", Colors.WARNING)
            self.tenancy_name = "Unknown"

        self.all_load_balancers: List[Dict] = []
        self.suspicious_load_balancers: List[Dict] = []
        self.failed_load_balancers: List[Dict] = []

    def _configure_clients(self, region: str):
        """Configure OCI clients for a specific region."""
        self.current_region = region
        region_config = dict(self.config)
        if region:
            region_config["region"] = region
        self.identity_client = oci.identity.IdentityClient(region_config)
        self.load_balancer_client = oci.load_balancer.LoadBalancerClient(region_config)
        self.network_load_balancer_client = oci.network_load_balancer.NetworkLoadBalancerClient(region_config)

    def get_scan_regions(self, regions: Optional[List[str]] = None, all_regions: bool = False) -> List[str]:
        """Return the region names to scan."""
        if regions:
            return regions

        if not all_regions:
            return [self.current_region]

        try:
            region_subscriptions = self.identity_client.list_region_subscriptions(self.tenancy_id).data
            ready_regions = []
            for subscription in region_subscriptions:
                status = normalize_health_status(model_value(subscription, "status", "READY"))
                region_name = model_value(subscription, "region_name")
                if region_name and status == "READY":
                    ready_regions.append(region_name)

            if ready_regions:
                return sorted(set(ready_regions))

            print_colored("No READY subscribed regions were returned; using configured region only", Colors.WARNING)
        except Exception as e:
            print_colored(f"Could not list subscribed regions: {e}", Colors.WARNING)

        return [self.current_region]

    def _get_health_status(self, client: Any, method_name: str, **kwargs) -> Optional[str]:
        """Best-effort health lookup. Missing methods or permissions should not stop the scan."""
        method = getattr(client, method_name, None)
        if not method:
            return None

        try:
            health_data = method(**kwargs).data
        except Exception as e:
            logging.debug("Could not fetch %s health: %s", method_name, e)
            return None

        for attr_name in ("status", "health_status", "lifecycle_state"):
            value = model_value(health_data, attr_name)
            if value:
                return str(value)

        return None

    def _backend_to_dict(self, backend: Any, default_offline: bool = False) -> Dict:
        """Convert an OCI backend model into the normalized shape used by scoring."""
        offline = model_value(backend, "offline")
        if offline is None:
            offline = model_value(backend, "is_offline", default_offline)

        return {
            'name': model_value(backend, "name", ""),
            'ip_address': model_value(backend, "ip_address", "Unknown"),
            'port': model_value(backend, "port", "Unknown"),
            'offline': bool(offline),
            'health_status': model_value(backend, "health_status", "")
        }

    def get_all_compartments(self, compartment_ids: Optional[List[str]] = None) -> List[Dict]:
        """Get all compartments in the tenancy or specific compartment IDs"""
        compartments = []

        if compartment_ids:
            print_colored(f"🔍 Using specified compartment IDs: {len(compartment_ids)}", Colors.INFO)
            for comp_id in compartment_ids:
                try:
                    comp = self.identity_client.get_compartment(comp_id)
                    if comp.data.lifecycle_state == "ACTIVE":
                        compartments.append({
                            'id': comp.data.id,
                            'name': comp.data.name,
                            'description': comp.data.description or "No description"
                        })
                except Exception as e:
                    print_colored(f"⚠️ Could not access compartment {comp_id}: {e}", Colors.WARNING)
        else:
            print_colored("🔍 Discovering all compartments in tenancy...", Colors.INFO)
            try:
                # Root compartment (tenancy OCID works as root compartment OCID)
                root_comp = self.identity_client.get_compartment(self.tenancy_id)
                compartments.append({
                    'id': root_comp.data.id,
                    'name': root_comp.data.name,
                    'description': root_comp.data.description or "Root compartment"
                })

                # All child compartments
                all_compartments = oci.pagination.list_call_get_all_results(
                    self.identity_client.list_compartments,
                    compartment_id=self.tenancy_id,
                    compartment_id_in_subtree=True
                ).data

                for comp in all_compartments:
                    if comp.lifecycle_state == "ACTIVE":
                        compartments.append({
                            'id': comp.id,
                            'name': comp.name,
                            'description': comp.description or "No description"
                        })

            except Exception as e:
                print_colored(f"❌ Error getting compartments: {e}", Colors.ERROR)
                return []

        return compartments

    def analyze_load_balancer_health(self, lb_data: Dict, compartment_name: str, lb_type: str) -> Dict:
        """Analyze a load balancer for ghost characteristics"""
        try:
            ghost_score = 0
            ghost_reasons = []

            lb_name = lb_data.get('display_name', 'Unknown')
            lb_id = lb_data.get('id', 'Unknown')
            region = lb_data.get('region', self.current_region)

            backend_sets = lb_data.get('backend_sets', {}) or {}
            listeners = lb_data.get('listeners', {}) or {}
            certificates = lb_data.get('certificates', {}) or {}

            # Backend sets
            if not backend_sets:
                ghost_score += 50
                ghost_reasons.append("No backend sets configured")
            else:
                empty_backend_count = 0
                unhealthy_backend_count = 0
                for _, bs_data in backend_sets.items():
                    backends = bs_data.get('backends', []) or []
                    backend_set_health = bs_data.get('health_status')
                    if not backends:
                        empty_backend_count += 1
                    else:
                        available_backends = [b for b in backends if not b.get('offline', True)]
                        if not available_backends:
                            empty_backend_count += 1

                    if backend_set_health and normalize_health_status(backend_set_health) in ["CRITICAL", "UNKNOWN"]:
                        unhealthy_backend_count += 1

                if empty_backend_count == len(backend_sets):
                    ghost_score += 45
                    ghost_reasons.append("All backend sets are empty or offline")
                elif empty_backend_count > 0:
                    ghost_score += 25
                    ghost_reasons.append(f"Some backend sets are empty ({empty_backend_count}/{len(backend_sets)})")

                if unhealthy_backend_count == len(backend_sets):
                    ghost_score += 35
                    ghost_reasons.append("All backend sets report unhealthy health status")
                elif unhealthy_backend_count > 0:
                    ghost_score += 20
                    ghost_reasons.append(f"Some backend sets report unhealthy health status ({unhealthy_backend_count}/{len(backend_sets)})")

            # Listeners
            if not listeners:
                ghost_score += 40
                ghost_reasons.append("No listeners configured")
            else:
                listeners_without_backends = 0
                for _, listener_data in listeners.items():
                    default_backend = listener_data.get('default_backend_set_name')
                    if not default_backend or default_backend not in backend_sets:
                        listeners_without_backends += 1

                if listeners_without_backends == len(listeners):
                    ghost_score += 35
                    ghost_reasons.append("All listeners lack valid backend sets")
                elif listeners_without_backends > 0:
                    ghost_score += 20
                    ghost_reasons.append(f"Some listeners lack backend sets ({listeners_without_backends}/{len(listeners)})")

            # SSL certificates check (Classic only)
            if lb_type == "Classic" and not certificates:
                https_listeners = [l for l in listeners.values() if l.get('protocol', '').upper() in ['HTTPS', 'SSL']]
                if https_listeners:
                    ghost_score += 15
                    ghost_reasons.append("HTTPS listeners without SSL certificates")

            # Lifecycle state
            lifecycle_state = lb_data.get('lifecycle_state', 'UNKNOWN')
            if lifecycle_state not in ['ACTIVE', 'CREATING']:
                ghost_score += 30
                ghost_reasons.append(f"Load balancer in {lifecycle_state} state")

            # Age boost if already suspicious
            time_created = lb_data.get('time_created')
            if time_created:
                try:
                    created_date = datetime.fromisoformat(str(time_created).replace('Z', '+00:00'))
                    days_old = (datetime.now(timezone.utc) - created_date).days
                    if days_old > 30 and ghost_score >= 40:
                        ghost_score += 10
                        ghost_reasons.append(f"Created {days_old} days ago with issues")
                except Exception:
                    pass

            ghost_score = min(ghost_score, 100)

            # Status buckets
            if ghost_score >= 80:
                ghost_status = "DEFINITE GHOST"
            elif ghost_score >= 60:
                ghost_status = "LIKELY GHOST"
            elif ghost_score >= 40:
                ghost_status = "SUSPICIOUS"
            elif ghost_score >= 20:
                ghost_status = "REVIEW NEEDED"
            else:
                ghost_status = "ACTIVE"

            # Details
            backend_details = []
            for bs_name, bs_data in backend_sets.items():
                backends = bs_data.get('backends', []) or []
                available_count = len([b for b in backends if not b.get('offline', True)])
                health_status = bs_data.get('health_status')
                health_suffix = f" ({health_status})" if health_status else ""
                backend_details.append(f"{bs_name}:{available_count}/{len(backends)} available{health_suffix}")

            listener_details = []
            for listener_name, listener_data in listeners.items():
                protocol = listener_data.get('protocol', 'Unknown')
                port = listener_data.get('port', 'Unknown')
                backend_set = listener_data.get('default_backend_set_name', 'None')
                listener_details.append(f"{listener_name}:{protocol}:{port}->>{backend_set}")

            shape = lb_data.get('shape_name', 'Unknown')
            if lb_type == "Network":
                shape = f"Network-{lb_data.get('bandwidth_in_mbps', 'Unknown')}Mbps"

            freeform_tags = lb_data.get('freeform_tags', {}) or {}
            defined_tags = lb_data.get('defined_tags', {}) or {}
            all_tags = []
            for k, v in freeform_tags.items():
                all_tags.append(f"{k}={v}")
            for namespace, tags in defined_tags.items():
                for k, v in tags.items():
                    all_tags.append(f"{namespace}.{k}={v}")

            return {
                'LoadBalancerName': lb_name,
                'LoadBalancerType': lb_type,
                'Region': region,
                'Compartment': compartment_name,
                'Shape': shape,
                'LifecycleState': lifecycle_state,
                'GhostScore': ghost_score,
                'GhostStatus': ghost_status,
                'AnalysisStatus': 'OK',
                'GhostReasons': "; ".join(ghost_reasons),
                'BackendSetCount': len(backend_sets),
                'ListenerCount': len(listeners),
                'CertificateCount': len(certificates),
                'TimeCreated': str(time_created) if time_created else "Unknown",
                'LoadBalancerId': lb_id,
                'Tags': "; ".join(all_tags) if all_tags else "",
                'BackendSetDetails': "; ".join(backend_details),
                'ListenerDetails': "; ".join(listener_details),
            }

        except Exception as e:
            print_colored(f"⚠️ Error analyzing load balancer {lb_data.get('display_name', 'Unknown')}: {e}", Colors.WARNING)
            return {
                'LoadBalancerName': lb_data.get('display_name', 'Unknown'),
                'LoadBalancerType': lb_type,
                'Region': lb_data.get('region', self.current_region),
                'Compartment': compartment_name,
                'Shape': 'Unknown',
                'LifecycleState': 'Unknown',
                'GhostScore': 0,
                'GhostStatus': 'ANALYSIS FAILED',
                'AnalysisStatus': 'FAILED',
                'GhostReasons': f"Error during analysis: {str(e)}",
                'BackendSetCount': 0,
                'ListenerCount': 0,
                'CertificateCount': 0,
                'TimeCreated': 'Unknown',
                'LoadBalancerId': lb_data.get('id', 'Unknown'),
                'Tags': '',
                'BackendSetDetails': '',
                'ListenerDetails': '',
            }

    def scan_load_balancers(
        self,
        compartment_ids: Optional[List[str]] = None,
        regions: Optional[List[str]] = None,
        all_regions: bool = False
    ):
        """Scan load balancers in specified compartments and regions."""
        show_banner()

        self.all_load_balancers = []
        self.suspicious_load_balancers = []
        self.failed_load_balancers = []

        scan_regions = self.get_scan_regions(regions, all_regions)
        print_colored(f"Connected to OCI Tenancy: {self.tenancy_name}", Colors.INFO)
        print_colored(f"Regions to scan: {', '.join(scan_regions)}", Colors.INFO)
        print()

        compartments = self.get_all_compartments(compartment_ids)
        if not compartments:
            print_colored("No compartments found!", Colors.ERROR)
            return

        print_colored(f"Found {len(compartments)} compartment(s) to scan", Colors.SUCCESS)
        print()

        total_load_balancers = 0

        for region in scan_regions:
            self._configure_clients(region)
            print_colored(f"Scanning region: {region}", Colors.HEADER)

            for compartment in compartments:
                print_colored(f"Scanning compartment: {compartment['name']}", Colors.INFO)

                try:
                    classic_lbs = []
                    try:
                        classic_lbs = oci.pagination.list_call_get_all_results(
                            self.load_balancer_client.list_load_balancers,
                            compartment_id=compartment['id']
                        ).data
                    except Exception as e:
                        print_colored(f"   Could not list classic load balancers: {e}", Colors.WARNING)

                    network_lbs = []
                    try:
                        network_lbs = oci.pagination.list_call_get_all_results(
                            self.network_load_balancer_client.list_network_load_balancers,
                            compartment_id=compartment['id']
                        ).data
                    except Exception as e:
                        print_colored(f"   Could not list network load balancers: {e}", Colors.WARNING)

                    compartment_lb_count = len(classic_lbs) + len(network_lbs)
                    total_load_balancers += compartment_lb_count

                    if compartment_lb_count == 0:
                        print_colored("   No load balancers found in this compartment", Colors.INFO)
                        print()
                        continue

                    print_colored(
                        f"   Found {len(classic_lbs)} classic + {len(network_lbs)} network load balancer(s)",
                        Colors.INFO
                    )

                    for lb in classic_lbs:
                        try:
                            print_colored(f"      Analyzing Classic LB: {lb.display_name}", Colors.INFO)
                            lb_details = self.load_balancer_client.get_load_balancer(lb.id).data
                            lb_dict = {
                                'id': lb_details.id,
                                'display_name': lb_details.display_name,
                                'region': region,
                                'lifecycle_state': lb_details.lifecycle_state,
                                'time_created': str(lb_details.time_created) if lb_details.time_created else None,
                                'shape_name': lb_details.shape_name,
                                'backend_sets': {},
                                'listeners': {l.name: {
                                    'protocol': l.protocol,
                                    'port': l.port,
                                    'default_backend_set_name': l.default_backend_set_name
                                } for l in lb_details.listeners.values()} if lb_details.listeners else {},
                                'certificates': lb_details.certificates or {},
                                'freeform_tags': lb_details.freeform_tags or {},
                                'defined_tags': lb_details.defined_tags or {}
                            }

                            if lb_details.backend_sets:
                                for bs in lb_details.backend_sets.values():
                                    backend_set_health = self._get_health_status(
                                        self.load_balancer_client,
                                        "get_backend_set_health",
                                        load_balancer_id=lb.id,
                                        backend_set_name=bs.name
                                    )
                                    lb_dict['backend_sets'][bs.name] = {
                                        'health_status': backend_set_health,
                                        'backends': [self._backend_to_dict(b, default_offline=True) for b in (bs.backends or [])]
                                    }

                            analysis = self.analyze_load_balancer_health(lb_dict, compartment['name'], "Classic")
                            display_status = self._get_display_status(analysis['GhostScore'])
                            status_color = Colors.GHOST if analysis['GhostScore'] >= SUSPICIOUS_SCORE_THRESHOLD else Colors.SUCCESS
                            print_colored(f"         {display_status} - Score: {analysis['GhostScore']}", status_color)
                            self.all_load_balancers.append(analysis)

                        except Exception as e:
                            print_colored(f"         Failed to analyze {lb.display_name}: {e}", Colors.ERROR)
                            self._add_failed_analysis(lb.display_name, compartment['name'], "Classic", str(e), lb.id, region)

                    for nlb in network_lbs:
                        try:
                            print_colored(f"      Analyzing Network LB: {nlb.display_name}", Colors.INFO)
                            nlb_details = self.network_load_balancer_client.get_network_load_balancer(nlb.id).data
                            nlb_dict = {
                                'id': nlb_details.id,
                                'display_name': nlb_details.display_name,
                                'region': region,
                                'lifecycle_state': nlb_details.lifecycle_state,
                                'time_created': str(nlb_details.time_created) if nlb_details.time_created else None,
                                'bandwidth_in_mbps': nlb_details.bandwidth_in_mbps,
                                'backend_sets': {},
                                'listeners': {},
                                'certificates': {},
                                'freeform_tags': nlb_details.freeform_tags or {},
                                'defined_tags': nlb_details.defined_tags or {}
                            }

                            try:
                                backend_sets = oci.pagination.list_call_get_all_results(
                                    self.network_load_balancer_client.list_backend_sets,
                                    network_load_balancer_id=nlb.id
                                ).data

                                for bs in backend_sets:
                                    backends = oci.pagination.list_call_get_all_results(
                                        self.network_load_balancer_client.list_backends,
                                        network_load_balancer_id=nlb.id,
                                        backend_set_name=bs.name
                                    ).data
                                    backend_set_health = self._get_health_status(
                                        self.network_load_balancer_client,
                                        "get_backend_set_health",
                                        network_load_balancer_id=nlb.id,
                                        backend_set_name=bs.name
                                    )

                                    nlb_dict['backend_sets'][bs.name] = {
                                        'health_status': backend_set_health,
                                        'backends': [self._backend_to_dict(b, default_offline=False) for b in (backends or [])]
                                    }
                            except Exception as e:
                                print_colored(f"           Could not get backend sets: {e}", Colors.WARNING)

                            try:
                                listeners = oci.pagination.list_call_get_all_results(
                                    self.network_load_balancer_client.list_listeners,
                                    network_load_balancer_id=nlb.id
                                ).data

                                for listener in listeners:
                                    nlb_dict['listeners'][listener.name] = {
                                        'protocol': listener.protocol,
                                        'port': listener.port,
                                        'default_backend_set_name': listener.default_backend_set_name
                                    }
                            except Exception as e:
                                print_colored(f"           Could not get listeners: {e}", Colors.WARNING)

                            analysis = self.analyze_load_balancer_health(nlb_dict, compartment['name'], "Network")
                            display_status = self._get_display_status(analysis['GhostScore'])
                            status_color = Colors.GHOST if analysis['GhostScore'] >= SUSPICIOUS_SCORE_THRESHOLD else Colors.SUCCESS
                            print_colored(f"         {display_status} - Score: {analysis['GhostScore']}", status_color)
                            self.all_load_balancers.append(analysis)

                        except Exception as e:
                            print_colored(f"         Failed to analyze {nlb.display_name}: {e}", Colors.ERROR)
                            self._add_failed_analysis(nlb.display_name, compartment['name'], "Network", str(e), nlb.id, region)

                except Exception as e:
                    print_colored(f"   Error scanning compartment: {e}", Colors.ERROR)

                print()

        self.failed_load_balancers = [
            lb for lb in self.all_load_balancers if lb.get('AnalysisStatus') == 'FAILED'
        ]
        self.suspicious_load_balancers = [
            lb for lb in self.all_load_balancers
            if lb.get('AnalysisStatus') == 'OK' and lb['GhostScore'] >= SUSPICIOUS_SCORE_THRESHOLD
        ]

        self._display_summary(
            total_load_balancers,
            len(self.suspicious_load_balancers),
            len(self.failed_load_balancers)
        )

    def _get_display_status(self, ghost_score: int) -> str:
        """Get display status with emojis for console output"""
        if ghost_score >= 80:
            return "👻 DEFINITE GHOST"
        elif ghost_score >= 60:
            return "🔍 LIKELY GHOST"
        elif ghost_score >= 40:
            return "⚠️ SUSPICIOUS"
        elif ghost_score >= 20:
            return "📊 REVIEW NEEDED"
        else:
            return "✅ ACTIVE"

    def _add_failed_analysis(
        self,
        lb_name: str,
        compartment_name: str,
        lb_type: str,
        error_msg: str,
        lb_id: str,
        region: Optional[str] = None
    ):
        """Add a failed analysis entry"""
        failed_analysis = {
            'LoadBalancerName': lb_name,
            'LoadBalancerType': lb_type,
            'Region': region or self.current_region,
            'Compartment': compartment_name,
            'Shape': 'Unknown',
            'LifecycleState': 'Unknown',
            'GhostScore': 0,
            'GhostStatus': 'ANALYSIS FAILED',
            'AnalysisStatus': 'FAILED',
            'GhostReasons': f"Failed to analyze: {error_msg}",
            'BackendSetCount': 0,
            'ListenerCount': 0,
            'CertificateCount': 0,
            'TimeCreated': 'Unknown',
            'LoadBalancerId': lb_id,
            'Tags': '',
            'BackendSetDetails': '',
            'ListenerDetails': '',
        }
        self.all_load_balancers.append(failed_analysis)

    def _display_summary(self, total_load_balancers: int, total_ghosts: int, total_failed: int = 0):
        """Display the hunt summary"""
        print_colored("╔═══════════════════════════════════════════════════════════════════════════════╗", Colors.HEADER)
        print_colored("║                                   📊 HUNT SUMMARY                              ║", Colors.HEADER)
        print_colored("╚═══════════════════════════════════════════════════════════════════════════════╝", Colors.HEADER)

        print_colored(f"📊 Total Load Balancers Scanned: {total_load_balancers}", Colors.INFO)
        print_colored(f"👻 Potential Ghost Load Balancers: {total_ghosts}", Colors.GHOST)
        print()

        if total_failed:
            print_colored(f"Analysis Failures: {total_failed}", Colors.WARNING)
        print()

        if self.suspicious_load_balancers:
            print_colored("🔍 DETAILED GHOST ANALYSIS:", Colors.GHOST)
            print_colored("═══════════════════════════════════════════════════════════════════════════════", Colors.HEADER)

            for ghost in sorted(self.suspicious_load_balancers, key=lambda x: x['GhostScore'], reverse=True):
                display_status = self._get_display_status(ghost['GhostScore'])
                print_colored(f"👻 {ghost['LoadBalancerName']} ({display_status})", Colors.GHOST)
                print_colored(f"   📍 Location: {ghost.get('Region', 'Unknown')} / {ghost['Compartment']} / {ghost['LoadBalancerType']}", Colors.INFO)
                print_colored(f"   📊 Ghost Score: {ghost['GhostScore']}/100", Colors.WARNING)
                print_colored(f"   🔍 Issues: {ghost['GhostReasons']}", Colors.ERROR)
                print_colored(f"   🏷️ Shape: {ghost['Shape']}", Colors.INFO)

                if ghost['Tags']:
                    print_colored(f"   🏷️ Tags: {ghost['Tags']}", Colors.INFO)

                print()

        if self.failed_load_balancers:
            print_colored("ANALYSIS FAILURES:", Colors.WARNING)
            for failed in self.failed_load_balancers:
                print_colored(
                    f"   {failed.get('Region', 'Unknown')} / {failed['Compartment']} / {failed['LoadBalancerName']}: {failed['GhostReasons']}",
                    Colors.WARNING
                )

    def export_to_csv(self, csv_path: str):
        """Export suspicious load balancers and analysis failures to CSV."""
        findings = self.suspicious_load_balancers + self.failed_load_balancers
        if not findings:
            print_colored("No suspicious load balancers or analysis failures found - no CSV export needed!", Colors.SUCCESS)
            return

        try:
            with open(csv_path, 'w', newline='', encoding='utf-8') as csvfile:
                fieldnames = [
                    'LoadBalancerName', 'LoadBalancerType', 'Region', 'Compartment', 'Shape', 'LifecycleState',
                    'GhostScore', 'GhostStatus', 'AnalysisStatus', 'GhostReasons', 'BackendSetCount', 'ListenerCount',
                    'CertificateCount', 'TimeCreated', 'LoadBalancerId', 'Tags',
                    'BackendSetDetails', 'ListenerDetails'
                ]

                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows([
                    {field: sanitize_csv_value(row.get(field, "")) for field in fieldnames}
                    for row in findings
                ])

            print_colored(f"Findings exported to: {csv_path}", Colors.SUCCESS)
            print_colored(
                f"Exported {len(self.suspicious_load_balancers)} suspicious load balancers and {len(self.failed_load_balancers)} analysis failures",
                Colors.WARNING
            )
            print_colored("CSV includes full configuration details for analysis", Colors.INFO)

            if os.path.exists(csv_path):
                file_size = os.path.getsize(csv_path)
                print_colored(f"CSV file created successfully ({file_size} bytes)", Colors.SUCCESS)

        except Exception as e:
            print_colored(f"Failed to export CSV: {e}", Colors.ERROR)

    def generate_html_report(self, html_path: str):
        """Generate an escaped HTML report."""
        print_colored("Generating HTML report...", Colors.INFO)

        try:
            report_date = datetime.now().strftime("%B %d, %Y at %H:%M")
            compartment_list = ", ".join(sorted({lb.get('Compartment', 'Unknown') for lb in self.all_load_balancers})) or "N/A"
            region_list = ", ".join(sorted({lb.get('Region', 'Unknown') for lb in self.all_load_balancers})) or self.current_region

            total_scanned = len(self.all_load_balancers)
            total_ghosts = len(self.suspicious_load_balancers)
            total_failed = len(self.failed_load_balancers)
            definite_ghosts = len([lb for lb in self.suspicious_load_balancers if lb['GhostScore'] >= 80])
            healthy_count = max(0, total_scanned - total_ghosts - total_failed)

            html_content = self._generate_html_content(
                report_date,
                compartment_list,
                region_list,
                total_scanned,
                total_ghosts,
                definite_ghosts,
                healthy_count,
                total_failed
            )

            with open(html_path, 'w', encoding='utf-8') as f:
                f.write(html_content)

            print_colored(f"HTML report generated: {html_path}", Colors.SUCCESS)

            if os.path.exists(html_path):
                file_size = os.path.getsize(html_path)
                print_colored(f"HTML report created successfully ({file_size} bytes)", Colors.SUCCESS)

        except Exception as e:
            print_colored(f"Failed to generate HTML report: {e}", Colors.ERROR)

    def _generate_html_content(
        self,
        report_date: str,
        compartment_list: str,
        region_list: str,
        total_scanned: int,
        total_ghosts: int,
        definite_ghosts: int,
        healthy_count: int,
        total_failed: int
    ) -> str:
        """Generate escaped HTML content for the report."""
        def table_cell(value: Any) -> str:
            return f"<td>{escape_html(value)}</td>"

        suspicious_rows = []
        for ghost in sorted(self.suspicious_load_balancers, key=lambda x: x['GhostScore'], reverse=True):
            score = ghost['GhostScore']
            score_class = "score-definite" if score >= 80 else "score-likely" if score >= 60 else "score-suspicious"
            status_class = "status-definite" if score >= 80 else "status-likely" if score >= 60 else "status-suspicious"
            config_details = []
            if ghost.get('BackendSetDetails'):
                config_details.append(f"Backend Sets: {ghost['BackendSetDetails']}")
            if ghost.get('ListenerDetails'):
                config_details.append(f"Listeners: {ghost['ListenerDetails']}")
            config_text = "<br>".join(escape_html(detail) for detail in config_details) or "No configuration details available"

            suspicious_rows.append(f"""
                        <tr>
                            {table_cell(ghost.get('LoadBalancerName', 'Unknown'))}
                            {table_cell(ghost.get('LoadBalancerType', 'Unknown'))}
                            {table_cell(ghost.get('Region', 'Unknown'))}
                            {table_cell(ghost.get('Compartment', 'Unknown'))}
                            {table_cell(ghost.get('Shape', 'Unknown'))}
                            {table_cell(ghost.get('LifecycleState', 'Unknown'))}
                            <td><span class="ghost-score {score_class}">{score}</span></td>
                            <td><span class="ghost-status {status_class}">{escape_html(ghost.get('GhostStatus', 'Unknown'))}</span></td>
                            <td class="reasons">{escape_html(ghost.get('GhostReasons', ''))}</td>
                            <td class="details">{config_text}</td>
                        </tr>""")

        failed_rows = []
        for failed in self.failed_load_balancers:
            failed_rows.append(f"""
                        <tr>
                            {table_cell(failed.get('LoadBalancerName', 'Unknown'))}
                            {table_cell(failed.get('LoadBalancerType', 'Unknown'))}
                            {table_cell(failed.get('Region', 'Unknown'))}
                            {table_cell(failed.get('Compartment', 'Unknown'))}
                            <td class="reasons">{escape_html(failed.get('GhostReasons', ''))}</td>
                            {table_cell(failed.get('LoadBalancerId', 'Unknown'))}
                        </tr>""")

        if suspicious_rows:
            suspicious_section = f"""
            <div class="section">
                <h2>Suspicious Load Balancers Detected</h2>
                <table class="ghost-table">
                    <thead>
                        <tr>
                            <th>Load Balancer</th>
                            <th>Type</th>
                            <th>Region</th>
                            <th>Compartment</th>
                            <th>Shape</th>
                            <th>State</th>
                            <th>Ghost Score</th>
                            <th>Status</th>
                            <th>Issues Found</th>
                            <th>Configuration Details</th>
                        </tr>
                    </thead>
                    <tbody>
{''.join(suspicious_rows)}
                    </tbody>
                </table>
            </div>"""
        else:
            suspicious_section = """
            <div class="no-ghosts">
                <h2>No suspicious load balancers found</h2>
                <p>No suspicious load balancers were found among successfully analyzed resources.</p>
            </div>"""

        failed_section = ""
        if failed_rows:
            failed_section = f"""
            <div class="section">
                <h2>Analysis Failures</h2>
                <table class="ghost-table">
                    <thead>
                        <tr>
                            <th>Load Balancer</th>
                            <th>Type</th>
                            <th>Region</th>
                            <th>Compartment</th>
                            <th>Error</th>
                            <th>OCID</th>
                        </tr>
                    </thead>
                    <tbody>
{''.join(failed_rows)}
                    </tbody>
                </table>
            </div>"""

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>LoadBalancer Cleanse - Android16 Report</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: #eef2f7;
            min-height: 100vh;
            padding: 20px;
            color: #1f2937;
        }}
        .container {{
            max-width: 1400px;
            margin: 0 auto;
            background: white;
            border: 1px solid #d8dee9;
            border-radius: 8px;
            overflow: hidden;
        }}
        .header {{
            background: #263445;
            color: white;
            padding: 28px 30px;
        }}
        .header h1 {{ font-size: 2em; margin-bottom: 8px; }}
        .header .subtitle {{ opacity: 0.9; }}
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 16px;
            padding: 24px;
            background: #f8fafc;
            border-bottom: 1px solid #e5e7eb;
        }}
        .stat-card {{
            background: white;
            padding: 20px;
            border: 1px solid #e5e7eb;
            border-radius: 8px;
            text-align: center;
        }}
        .stat-number {{ font-size: 2.1em; font-weight: bold; margin-bottom: 8px; }}
        .stat-label {{ color: #4b5563; }}
        .ghost {{ color: #dc2626; }}
        .total {{ color: #2563eb; }}
        .clean {{ color: #16a34a; }}
        .failed {{ color: #b45309; }}
        .content {{ padding: 28px; }}
        .section {{ margin-bottom: 36px; }}
        .section h2 {{
            margin-bottom: 16px;
            padding-bottom: 8px;
            border-bottom: 2px solid #2563eb;
            font-size: 1.5em;
        }}
        .ghost-table {{
            width: 100%;
            border-collapse: collapse;
            background: white;
            border: 1px solid #e5e7eb;
        }}
        .ghost-table th {{
            background: #334155;
            color: white;
            padding: 12px;
            text-align: left;
            font-weight: 600;
        }}
        .ghost-table td {{
            padding: 11px 12px;
            border-bottom: 1px solid #e5e7eb;
            vertical-align: top;
        }}
        .ghost-score {{
            display: inline-block;
            font-weight: bold;
            padding: 4px 10px;
            border-radius: 999px;
            color: white;
            min-width: 48px;
            text-align: center;
        }}
        .score-definite {{ background: #dc2626; }}
        .score-likely {{ background: #ea580c; }}
        .score-suspicious {{ background: #d97706; }}
        .ghost-status {{
            display: inline-block;
            font-weight: bold;
            padding: 4px 10px;
            border-radius: 999px;
            font-size: 0.9em;
        }}
        .status-definite {{ background: #fee2e2; color: #991b1b; }}
        .status-likely {{ background: #ffedd5; color: #9a3412; }}
        .status-suspicious {{ background: #fef3c7; color: #92400e; }}
        .reasons {{ max-width: 360px; word-break: break-word; }}
        .details {{ max-width: 320px; word-break: break-word; color: #4b5563; }}
        .metadata {{
            background: #f8fafc;
            padding: 18px;
            border-left: 4px solid #2563eb;
            margin-bottom: 28px;
            border-radius: 0 8px 8px 0;
        }}
        .metadata p {{ margin: 5px 0; color: #4b5563; }}
        .no-ghosts {{
            padding: 36px;
            border: 1px solid #bbf7d0;
            background: #f0fdf4;
            color: #166534;
            border-radius: 8px;
            margin-bottom: 36px;
        }}
        .footer {{
            background: #f8fafc;
            padding: 18px;
            text-align: center;
            color: #64748b;
            border-top: 1px solid #e5e7eb;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>LoadBalancer Cleanse - Android16</h1>
            <div class="subtitle">OCI Load Balancer configuration and ghost-score report</div>
        </div>

        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-number total">{total_scanned}</div>
                <div class="stat-label">Total Load Balancers</div>
            </div>
            <div class="stat-card">
                <div class="stat-number ghost">{total_ghosts}</div>
                <div class="stat-label">Suspicious Load Balancers</div>
            </div>
            <div class="stat-card">
                <div class="stat-number ghost">{definite_ghosts}</div>
                <div class="stat-label">Definite Ghosts</div>
            </div>
            <div class="stat-card">
                <div class="stat-number clean">{healthy_count}</div>
                <div class="stat-label">Healthy Load Balancers</div>
            </div>
            <div class="stat-card">
                <div class="stat-number failed">{total_failed}</div>
                <div class="stat-label">Analysis Failures</div>
            </div>
        </div>

        <div class="content">
            <div class="metadata">
                <p><strong>Report Generated:</strong> {escape_html(report_date)}</p>
                <p><strong>OCI Tenancy:</strong> {escape_html(self.tenancy_name)}</p>
                <p><strong>Regions Scanned:</strong> {escape_html(region_list)}</p>
                <p><strong>Compartments Scanned:</strong> {escape_html(compartment_list)}</p>
                <p><strong>Analysis Criteria:</strong> load balancers with Ghost Score &gt;= {SUSPICIOUS_SCORE_THRESHOLD} are considered suspicious.</p>
            </div>

{suspicious_section}
{failed_section}
        </div>

        <div class="footer">
            <p>Generated by OCI CAM-West CAMehameha Repository</p>
            <p>https://github.com/tukirella/CAMehameha</p>
        </div>
    </div>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(
        description="LoadBalancer Cleanse - Android16 (Ghost Score KPI scan for OCI Load Balancers)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic cleanse hunt across all compartments
  python3 LB-Cleanse-Android16.py

  # Scan specific compartments only
  python3 LB-Cleanse-Android16.py --compartments ocid1.compartment.oc1..aaa...

  # Use specific OCI config profile
  python3 LB-Cleanse-Android16.py --profile PROD --config-file ~/.oci/config

  # Scan every READY subscribed OCI region
  python3 LB-Cleanse-Android16.py --all-regions
        """
    )

    parser.add_argument('--config-file', help='Path to OCI config file (default: ~/.oci/config)')
    parser.add_argument('--profile', default='DEFAULT', help='OCI config profile to use (default: DEFAULT)')
    parser.add_argument('--compartments', nargs='+', help='Specific compartment OCIDs to scan (default: all compartments)')
    parser.add_argument('--regions', nargs='+', help='Specific OCI region names to scan (default: configured profile region)')
    parser.add_argument('--all-regions', action='store_true', help='Scan all READY subscribed OCI regions')

    parser.add_argument(
        '--csv-path',
        default=f"oci_lbcleanse_android16_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        help='Path for CSV export'
    )
    parser.add_argument(
        '--html-path',
        default=f"LB-Cleanse-Android16_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html",
        help='Path for HTML report'
    )
    parser.add_argument('--verbose', '-v', action='store_true', help='Enable verbose logging')

    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)

    try:
        hunter = OCILoadBalancerGhostHunter(args.config_file, args.profile)
        hunter.scan_load_balancers(args.compartments, args.regions, args.all_regions)

        print()
        hunter.export_to_csv(args.csv_path)
        print()
        hunter.generate_html_report(args.html_path)

        print()
        print_colored("🎉 LB Cleanse complete!", Colors.SUCCESS)

    except Exception as e:
        print_colored(f"❌ LB Cleanse failed: {e}", Colors.ERROR)
        if args.verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
