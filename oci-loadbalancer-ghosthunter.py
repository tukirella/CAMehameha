#!/usr/bin/env python3
"""
OCI LoadBalancer Ghost Hunter - CloudCostChefs Edition
Hunt down forgotten and unused OCI Load Balancers across all compartments

Author: CloudCostChefs
Version: 1.0
Requires: oci-cli, oci-python-sdk
"""

import argparse
import csv
import json
import logging
import os
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple

try:
    import oci
    from oci.config import from_file
    from oci.exceptions import ServiceError
except ImportError:
    print("❌ OCI Python SDK not found. Please install it:")
    print("   pip install oci")
    sys.exit(1)

# CloudCostChefs Color Styling
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

def show_banner():
    """Display the CloudCostChefs banner"""
    print_colored("╔═══════════════════════════════════════════════════════════════════════════════╗", Colors.HEADER)
    print_colored("║                         🔍 OCI LoadBalancer-GhostHunter 🔍                   ║", Colors.HEADER)
    print_colored("║                              CloudCostChefs Edition                            ║", Colors.HEADER)
    print_colored("║                        Hunt down those forgotten LBs!                        ║", Colors.HEADER)
    print_colored("╚═══════════════════════════════════════════════════════════════════════════════╝", Colors.HEADER)
    print()

class OCILoadBalancerGhostHunter:
    def __init__(self, config_file: Optional[str] = None, profile: str = "DEFAULT"):
        """Initialize the OCI Ghost Hunter"""
        self.config = from_file(config_file or "~/.oci/config", profile)
        self.identity_client = oci.identity.IdentityClient(self.config)
        self.load_balancer_client = oci.load_balancer.LoadBalancerClient(self.config)
        self.network_load_balancer_client = oci.network_load_balancer.NetworkLoadBalancerClient(self.config)
        
        # Get tenancy details
        self.tenancy_id = self.config["tenancy"]
        try:
            tenancy = self.identity_client.get_tenancy(self.tenancy_id)
            self.tenancy_name = tenancy.data.name
        except Exception as e:
            print_colored(f"⚠️ Could not get tenancy name: {e}", Colors.WARNING)
            self.tenancy_name = "Unknown"
        
        self.all_load_balancers = []
        self.suspicious_load_balancers = []

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
                # Get root compartment
                root_comp = self.identity_client.get_compartment(self.tenancy_id)
                compartments.append({
                    'id': root_comp.data.id,
                    'name': root_comp.data.name,
                    'description': root_comp.data.description or "Root compartment"
                })
                
                # Get all child compartments
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
            
            # Safely get properties
            backend_sets = lb_data.get('backend_sets', {})
            listeners = lb_data.get('listeners', {})
            certificates = lb_data.get('certificates', {})
            
            # Check backend sets
            if not backend_sets:
                ghost_score += 50
                ghost_reasons.append("No backend sets configured")
            else:
                empty_backend_count = 0
                total_backends = 0
                
                for bs_name, bs_data in backend_sets.items():
                    backends = bs_data.get('backends', [])
                    if not backends:
                        empty_backend_count += 1
                    else:
                        # Check for healthy backends
                        healthy_backends = [b for b in backends if not b.get('offline', True)]
                        if not healthy_backends:
                            empty_backend_count += 1
                    total_backends += len(backends)
                
                if empty_backend_count == len(backend_sets):
                    ghost_score += 45
                    ghost_reasons.append("All backend sets are empty or offline")
                elif empty_backend_count > 0:
                    ghost_score += 25
                    ghost_reasons.append(f"Some backend sets are empty ({empty_backend_count}/{len(backend_sets)})")
            
            # Check listeners
            if not listeners:
                ghost_score += 40
                ghost_reasons.append("No listeners configured")
            else:
                # Check for listeners without backend sets
                listeners_without_backends = 0
                for listener_name, listener_data in listeners.items():
                    default_backend = listener_data.get('default_backend_set_name')
                    if not default_backend or default_backend not in backend_sets:
                        listeners_without_backends += 1
                
                if listeners_without_backends == len(listeners):
                    ghost_score += 35
                    ghost_reasons.append("All listeners lack valid backend sets")
                elif listeners_without_backends > 0:
                    ghost_score += 20
                    ghost_reasons.append(f"Some listeners lack backend sets ({listeners_without_backends}/{len(listeners)})")
            
            # Check SSL certificates (for Classic LBs)
            if lb_type == "Classic" and not certificates:
                # Only add score if we have HTTPS listeners
                https_listeners = [l for l in listeners.values() if l.get('protocol', '').upper() in ['HTTPS', 'SSL']]
                if https_listeners:
                    ghost_score += 15
                    ghost_reasons.append("HTTPS listeners without SSL certificates")
            
            # Check lifecycle state
            lifecycle_state = lb_data.get('lifecycle_state', 'UNKNOWN')
            if lifecycle_state not in ['ACTIVE', 'CREATING']:
                ghost_score += 30
                ghost_reasons.append(f"Load balancer in {lifecycle_state} state")
            
            # Check for recent activity (creation vs current time)
            time_created = lb_data.get('time_created')
            if time_created:
                from datetime import datetime, timezone
                try:
                    created_date = datetime.fromisoformat(time_created.replace('Z', '+00:00'))
                    days_old = (datetime.now(timezone.utc) - created_date).days
                    if days_old > 30 and ghost_score > 40:  # Old and already suspicious
                        ghost_score += 10
                        ghost_reasons.append(f"Created {days_old} days ago with issues")
                except:
                    pass
            
            # Determine ghost status
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
            
            # Collect detailed information
            backend_details = []
            for bs_name, bs_data in backend_sets.items():
                backends = bs_data.get('backends', [])
                healthy_count = len([b for b in backends if not b.get('offline', True)])
                backend_details.append(f"{bs_name}:{healthy_count}/{len(backends)} healthy")
            
            listener_details = []
            for listener_name, listener_data in listeners.items():
                protocol = listener_data.get('protocol', 'Unknown')
                port = listener_data.get('port', 'Unknown')
                backend_set = listener_data.get('default_backend_set_name', 'None')
                listener_details.append(f"{listener_name}:{protocol}:{port}->>{backend_set}")
            
            # Extract shape and other details
            shape = lb_data.get('shape_name', 'Unknown')
            if lb_type == "Network":
                shape = f"Network-{lb_data.get('bandwidth_in_mbps', 'Unknown')}Mbps"
            
            # Get tags
            freeform_tags = lb_data.get('freeform_tags', {})
            defined_tags = lb_data.get('defined_tags', {})
            all_tags = []
            for k, v in freeform_tags.items():
                all_tags.append(f"{k}={v}")
            for namespace, tags in defined_tags.items():
                for k, v in tags.items():
                    all_tags.append(f"{namespace}.{k}={v}")
            
            return {
                'LoadBalancerName': lb_name,
                'LoadBalancerType': lb_type,
                'Compartment': compartment_name,
                'Shape': shape,
                'LifecycleState': lifecycle_state,
                'GhostScore': ghost_score,
                'GhostStatus': ghost_status,
                'GhostReasons': "; ".join(ghost_reasons),
                'BackendSetCount': len(backend_sets),
                'ListenerCount': len(listeners),
                'CertificateCount': len(certificates),
                'TimeCreated': time_created or "Unknown",
                'LoadBalancerId': lb_id,
                'Tags': "; ".join(all_tags) if all_tags else "",
                # Detailed information
                'BackendSetDetails': "; ".join(backend_details),
                'ListenerDetails': "; ".join(listener_details),
            }
            
        except Exception as e:
            print_colored(f"         ⚠️ Error analyzing load balancer {lb_data.get('display_name', 'Unknown')}: {e}", Colors.WARNING)
            
            return {
                'LoadBalancerName': lb_data.get('display_name', 'Unknown'),
                'LoadBalancerType': lb_type,
                'Compartment': compartment_name,
                'Shape': 'Unknown',
                'LifecycleState': 'Unknown',
                'GhostScore': 0,
                'GhostStatus': 'ANALYSIS FAILED',
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

    def scan_load_balancers(self, compartment_ids: Optional[List[str]] = None):
        """Scan all load balancers in specified compartments"""
        show_banner()
        
        print_colored(f"👤 Connected to OCI Tenancy: {self.tenancy_name}", Colors.INFO)
        print()
        
        compartments = self.get_all_compartments(compartment_ids)
        if not compartments:
            print_colored("❌ No compartments found!", Colors.ERROR)
            return
        
        print_colored(f"🔍 Found {len(compartments)} compartment(s) to scan", Colors.SUCCESS)
        print()
        
        total_load_balancers = 0
        total_ghosts = 0
        
        for compartment in compartments:
            print_colored(f"🔄 Scanning compartment: {compartment['name']}", Colors.INFO)
            
            try:
                # Scan Classic Load Balancers
                classic_lbs = []
                try:
                    classic_lbs = oci.pagination.list_call_get_all_results(
                        self.load_balancer_client.list_load_balancers,
                        compartment_id=compartment['id']
                    ).data
                except Exception as e:
                    print_colored(f"   ⚠️ Could not list classic load balancers: {e}", Colors.WARNING)
                
                # Scan Network Load Balancers
                network_lbs = []
                try:
                    network_lbs = oci.pagination.list_call_get_all_results(
                        self.network_load_balancer_client.list_network_load_balancers,
                        compartment_id=compartment['id']
                    ).data
                except Exception as e:
                    print_colored(f"   ⚠️ Could not list network load balancers: {e}", Colors.WARNING)
                
                compartment_lb_count = len(classic_lbs) + len(network_lbs)
                total_load_balancers += compartment_lb_count
                
                if compartment_lb_count == 0:
                    print_colored("   ℹ️ No load balancers found in this compartment", Colors.INFO)
                    continue
                
                print_colored(f"   📊 Found {len(classic_lbs)} classic + {len(network_lbs)} network load balancer(s)", Colors.INFO)
                
                # Analyze Classic Load Balancers
                for lb in classic_lbs:
                    try:
                        print_colored(f"      🔍 Analyzing Classic LB: {lb.display_name}", Colors.INFO)
                        
                        # Get full load balancer details
                        lb_details = self.load_balancer_client.get_load_balancer(lb.id).data
                        lb_dict = {
                            'id': lb_details.id,
                            'display_name': lb_details.display_name,
                            'lifecycle_state': lb_details.lifecycle_state,
                            'time_created': str(lb_details.time_created) if lb_details.time_created else None,
                            'shape_name': lb_details.shape_name,
                            'backend_sets': {bs.name: {
                                'backends': [{'ip_address': b.ip_address, 'port': b.port, 'offline': b.offline} for b in bs.backends]
                            } for bs in lb_details.backend_sets.values()} if lb_details.backend_sets else {},
                            'listeners': {l.name: {
                                'protocol': l.protocol,
                                'port': l.port,
                                'default_backend_set_name': l.default_backend_set_name
                            } for l in lb_details.listeners.values()} if lb_details.listeners else {},
                            'certificates': lb_details.certificates or {},
                            'freeform_tags': lb_details.freeform_tags or {},
                            'defined_tags': lb_details.defined_tags or {}
                        }
                        
                        analysis = self.analyze_load_balancer_health(lb_dict, compartment['name'], "Classic")
                        
                        # Display result
                        if analysis['GhostScore'] >= 40:
                            total_ghosts += 1
                            display_status = self._get_display_status(analysis['GhostScore'])
                            print_colored(f"         {display_status} - Score: {analysis['GhostScore']}", Colors.GHOST)
                        else:
                            display_status = self._get_display_status(analysis['GhostScore'])
                            print_colored(f"         {display_status} - Score: {analysis['GhostScore']}", Colors.SUCCESS)
                        
                        self.all_load_balancers.append(analysis)
                        
                    except Exception as e:
                        print_colored(f"         ❌ Failed to analyze {lb.display_name}: {e}", Colors.ERROR)
                        self._add_failed_analysis(lb.display_name, compartment['name'], "Classic", str(e), lb.id)
                
                # Analyze Network Load Balancers
                for nlb in network_lbs:
                    try:
                        print_colored(f"      🔍 Analyzing Network LB: {nlb.display_name}", Colors.INFO)
                        
                        # Get full network load balancer details
                        nlb_details = self.network_load_balancer_client.get_network_load_balancer(nlb.id).data
                        nlb_dict = {
                            'id': nlb_details.id,
                            'display_name': nlb_details.display_name,
                            'lifecycle_state': nlb_details.lifecycle_state,
                            'time_created': str(nlb_details.time_created) if nlb_details.time_created else None,
                            'bandwidth_in_mbps': nlb_details.bandwidth_in_mbps,
                            'backend_sets': {},
                            'listeners': {},
                            'certificates': {},
                            'freeform_tags': nlb_details.freeform_tags or {},
                            'defined_tags': nlb_details.defined_tags or {}
                        }
                        
                        # Get backend sets for Network LB
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
                                
                                nlb_dict['backend_sets'][bs.name] = {
                                    'backends': [{'ip_address': b.ip_address, 'port': b.port, 'offline': False} for b in backends]
                                }
                        except Exception as e:
                            print_colored(f"           ⚠️ Could not get backend sets: {e}", Colors.WARNING)
                        
                        # Get listeners for Network LB
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
                            print_colored(f"           ⚠️ Could not get listeners: {e}", Colors.WARNING)
                        
                        analysis = self.analyze_load_balancer_health(nlb_dict, compartment['name'], "Network")
                        
                        # Display result
                        if analysis['GhostScore'] >= 40:
                            total_ghosts += 1
                            display_status = self._get_display_status(analysis['GhostScore'])
                            print_colored(f"         {display_status} - Score: {analysis['GhostScore']}", Colors.GHOST)
                        else:
                            display_status = self._get_display_status(analysis['GhostScore'])
                            print_colored(f"         {display_status} - Score: {analysis['GhostScore']}", Colors.SUCCESS)
                        
                        self.all_load_balancers.append(analysis)
                        
                    except Exception as e:
                        print_colored(f"         ❌ Failed to analyze {nlb.display_name}: {e}", Colors.ERROR)
                        self._add_failed_analysis(nlb.display_name, compartment['name'], "Network", str(e), nlb.id)
                
            except Exception as e:
                print_colored(f"   ❌ Error scanning compartment: {e}", Colors.ERROR)
            
            print()
        
        # Display summary
        self._display_summary(total_load_balancers, total_ghosts)
        
        # Filter suspicious load balancers
        self.suspicious_load_balancers = [lb for lb in self.all_load_balancers if lb['GhostScore'] >= 40]

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

    def _add_failed_analysis(self, lb_name: str, compartment_name: str, lb_type: str, error_msg: str, lb_id: str):
        """Add a failed analysis entry"""
        failed_analysis = {
            'LoadBalancerName': lb_name,
            'LoadBalancerType': lb_type,
            'Compartment': compartment_name,
            'Shape': 'Unknown',
            'LifecycleState': 'Unknown',
            'GhostScore': 0,
            'GhostStatus': 'ANALYSIS FAILED',
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

    def _display_summary(self, total_load_balancers: int, total_ghosts: int):
        """Display the hunt summary"""
        print_colored("╔═══════════════════════════════════════════════════════════════════════════════╗", Colors.HEADER)
        print_colored("║                                   📊 HUNT SUMMARY                             ║", Colors.HEADER)
        print_colored("╚═══════════════════════════════════════════════════════════════════════════════╝", Colors.HEADER)
        
        print_colored(f"📊 Total Load Balancers Scanned: {total_load_balancers}", Colors.INFO)
        print_colored(f"👻 Potential Ghost Load Balancers: {total_ghosts}", Colors.GHOST)
        print()
        
        # Show detailed results for suspicious load balancers
        if self.suspicious_load_balancers:
            print_colored("🔍 DETAILED GHOST ANALYSIS:", Colors.GHOST)
            print_colored("═══════════════════════════════════════════════════════════════════════════════", Colors.HEADER)
            
            for ghost in sorted(self.suspicious_load_balancers, key=lambda x: x['GhostScore'], reverse=True):
                display_status = self._get_display_status(ghost['GhostScore'])
                print_colored(f"👻 {ghost['LoadBalancerName']} ({display_status})", Colors.GHOST)
                print_colored(f"   📍 Location: {ghost['Compartment']} / {ghost['LoadBalancerType']}", Colors.INFO)
                print_colored(f"   📊 Ghost Score: {ghost['GhostScore']}/100", Colors.WARNING)
                print_colored(f"   🔍 Issues: {ghost['GhostReasons']}", Colors.ERROR)
                print_colored(f"   🏷️ Shape: {ghost['Shape']}", Colors.INFO)
                
                if ghost['Tags']:
                    print_colored(f"   🏷️ Tags: {ghost['Tags']}", Colors.INFO)
                
                print()

    def export_to_csv(self, csv_path: str):
        """Export suspicious load balancers to CSV"""
        if not self.suspicious_load_balancers:
            print_colored("🎉 No suspicious load balancers found - no CSV export needed!", Colors.SUCCESS)
            return
        
        try:
            with open(csv_path, 'w', newline='', encoding='utf-8') as csvfile:
                fieldnames = [
                    'LoadBalancerName', 'LoadBalancerType', 'Compartment', 'Shape', 'LifecycleState',
                    'GhostScore', 'GhostStatus', 'GhostReasons', 'BackendSetCount', 'ListenerCount',
                    'CertificateCount', 'TimeCreated', 'LoadBalancerId', 'Tags',
                    'BackendSetDetails', 'ListenerDetails'
                ]
                
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(self.suspicious_load_balancers)
            
            print_colored(f"📄 Suspicious load balancers exported to: {csv_path}", Colors.SUCCESS)
            print_colored(f"📊 Exported {len(self.suspicious_load_balancers)} suspicious load balancers (Ghost Score ≥ 40)", Colors.WARNING)
            print_colored("💡 CSV includes full configuration details for analysis", Colors.INFO)
            
            # Verify file was created
            if os.path.exists(csv_path):
                file_size = os.path.getsize(csv_path)
                print_colored(f"✅ CSV file created successfully ({file_size} bytes)", Colors.SUCCESS)
            
        except Exception as e:
            print_colored(f"❌ Failed to export CSV: {e}", Colors.ERROR)

    def generate_html_report(self, html_path: str):
        """Generate HTML report"""
        print_colored("📄 Generating HTML report...", Colors.INFO)
        
        try:
            report_date = datetime.now().strftime("%B %d, %Y at %H:%M")
            compartment_list = ", ".join(set([lb['Compartment'] for lb in self.all_load_balancers]))
            
            total_scanned = len(self.all_load_balancers)
            total_ghosts = len(self.suspicious_load_balancers)
            definite_ghosts = len([lb for lb in self.suspicious_load_balancers if lb['GhostScore'] >= 80])
            
            html_content = self._generate_html_content(
                report_date, compartment_list, total_scanned, total_ghosts, definite_ghosts
            )
            
            with open(html_path, 'w', encoding='utf-8') as f:
                f.write(html_content)
            
            print_colored(f"📄 HTML report generated: {html_path}", Colors.SUCCESS)
            
            if os.path.exists(html_path):
                file_size = os.path.getsize(html_path)
                print_colored(f"✅ HTML report created successfully ({file_size} bytes)", Colors.SUCCESS)
                print_colored("🌐 Open the HTML file in your browser to view the interactive report", Colors.INFO)
            
        except Exception as e:
            print_colored(f"❌ Failed to generate HTML report: {e}", Colors.ERROR)

    def _generate_html_content(self, report_date: str, compartment_list: str, 
                              total_scanned: int, total_ghosts: int, definite_ghosts: int) -> str:
        """Generate the HTML content for the report"""
        
        # Generate table rows for suspicious load balancers
        table_rows = ""
        if self.suspicious_load_balancers:
            for ghost in sorted(self.suspicious_load_balancers, key=lambda x: x['GhostScore'], reverse=True):
                score_class = "score-definite" if ghost['GhostScore'] >= 80 else "score-likely" if ghost['GhostScore'] >= 60 else "score-suspicious"
                status_class = "status-definite" if ghost['GhostScore'] >= 80 else "status-likely" if ghost['GhostScore'] >= 60 else "status-suspicious"
                
                config_details = []
                if ghost['BackendSetDetails']:
                    config_details.append(f"Backend Sets: {ghost['BackendSetDetails']}")
                if ghost['ListenerDetails']:
                    config_details.append(f"Listeners: {ghost['ListenerDetails']}")
                
                config_text = "<br>".join(config_details) if config_details else "No configuration details available"
                
                table_rows += f"""
                        <tr>
                            <td><strong>{ghost['LoadBalancerName']}</strong></td>
                            <td>{ghost['LoadBalancerType']}</td>
                            <td>{ghost['Compartment']}</td>
                            <td>{ghost['Shape']}</td>
                            <td>{ghost['LifecycleState']}</td>
                            <td><span class="ghost-score {score_class}">{ghost['GhostScore']}</span></td>
                            <td><span class="ghost-status {status_class}">{ghost['GhostStatus']}</span></td>
                            <td class="reasons">{ghost['GhostReasons']}</td>
                            <td class="details">{config_text}</td>
                        </tr>"""
        
        no_ghosts_section = """
            <div class="no-ghosts">
                <h2>🎉 Congratulations!</h2>
                <p>No suspicious load balancers were found in your OCI environment.</p>
                <p>All load balancers appear to be properly configured and in use.</p>
            </div>""" if not self.suspicious_load_balancers else f"""
            <div class="section">
                <h2>👻 Suspicious Load Balancers Detected</h2>
                <table class="ghost-table">
                    <thead>
                        <tr>
                            <th>Load Balancer</th>
                            <th>Type</th>
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
{table_rows}
                    </tbody>
                </table>
            </div>"""

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🔍 OCI LoadBalancer Ghost Hunter Report</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }}
        
        .container {{
            max-width: 1400px;
            margin: 0 auto;
            background: white;
            border-radius: 15px;
            box-shadow: 0 20px 40px rgba(0,0,0,0.1);
            overflow: hidden;
        }}
        
        .header {{
            background: linear-gradient(135deg, #2c3e50 0%, #34495e 100%);
            color: white;
            padding: 30px;
            text-align: center;
        }}
        
        .header h1 {{
            font-size: 2.5em;
            margin-bottom: 10px;
            text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
        }}
        
        .header .subtitle {{
            font-size: 1.2em;
            opacity: 0.9;
        }}
        
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            padding: 30px;
            background: #f8f9fa;
        }}
        
        .stat-card {{
            background: white;
            padding: 25px;
            border-radius: 10px;
            box-shadow: 0 4px 8px rgba(0,0,0,0.1);
            text-align: center;
            transition: transform 0.3s ease;
        }}
        
        .stat-card:hover {{
            transform: translateY(-5px);
        }}
        
        .stat-number {{
            font-size: 2.5em;
            font-weight: bold;
            margin-bottom: 10px;
        }}
        
        .stat-label {{
            color: #666;
            font-size: 1.1em;
        }}
        
        .ghost {{ color: #e74c3c; }}
        .suspicious {{ color: #f39c12; }}
        .total {{ color: #3498db; }}
        .clean {{ color: #27ae60; }}
        
        .content {{
            padding: 30px;
        }}
        
        .section {{
            margin-bottom: 40px;
        }}
        
        .section h2 {{
            color: #2c3e50;
            margin-bottom: 20px;
            padding-bottom: 10px;
            border-bottom: 3px solid #3498db;
            font-size: 1.8em;
        }}
        
        .ghost-table {{
            width: 100%;
            border-collapse: collapse;
            background: white;
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 4px 8px rgba(0,0,0,0.1);
        }}
        
        .ghost-table th {{
            background: #34495e;
            color: white;
            padding: 15px;
            text-align: left;
            font-weight: 600;
        }}
        
        .ghost-table td {{
            padding: 12px 15px;
            border-bottom: 1px solid #eee;
            vertical-align: top;
        }}
        
        .ghost-table tr:hover {{
            background: #f8f9fa;
        }}
        
        .ghost-score {{
            font-weight: bold;
            padding: 5px 10px;
            border-radius: 20px;
            color: white;
            text-align: center;
            min-width: 60px;
        }}
        
        .score-definite {{ background: #e74c3c; }}
        .score-likely {{ background: #e67e22; }}
        .score-suspicious {{ background: #f39c12; }}
        
        .ghost-status {{
            font-weight: bold;
            padding: 5px 12px;
            border-radius: 15px;
            font-size: 0.9em;
        }}
        
        .status-definite {{ background: #ffebee; color: #c62828; }}
        .status-likely {{ background: #fff3e0; color: #ef6c00; }}
        .status-suspicious {{ background: #fffbf0; color: #f57c00; }}
        
        .reasons {{
            max-width: 300px;
            word-wrap: break-word;
        }}
        
        .details {{
            font-size: 0.9em;
            color: #666;
            max-width: 250px;
            word-wrap: break-word;
        }}
        
        .footer {{
            background: #ecf0f1;
            padding: 20px;
            text-align: center;
            color: #7f8c8d;
            border-top: 1px solid #bdc3c7;
        }}
        
        .no-ghosts {{
            text-align: center;
            padding: 60px;
            color: #27ae60;
            font-size: 1.5em;
        }}
        
        .metadata {{
            background: #f8f9fa;
            padding: 20px;
            border-left: 4px solid #3498db;
            margin-bottom: 30px;
            border-radius: 0 8px 8px 0;
        }}
        
        .metadata h3 {{
            color: #2c3e50;
            margin-bottom: 10px;
        }}
        
        .metadata p {{
            margin: 5px 0;
            color: #666;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🔍 OCI LoadBalancer Ghost Hunter</h1>
            <div class="subtitle">CloudCostChefs Edition - Hunt Report</div>
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
                <div class="stat-number clean">{total_scanned - total_ghosts}</div>
                <div class="stat-label">Healthy Load Balancers</div>
            </div>
        </div>
        
        <div class="content">
            <div class="metadata">
                <h3>📊 Scan Details</h3>
                <p><strong>Report Generated:</strong> {report_date}</p>
                <p><strong>OCI Tenancy:</strong> {self.tenancy_name}</p>
                <p><strong>Compartments Scanned:</strong> {compartment_list}</p>
                <p><strong>Analysis Criteria:</strong> Load balancers with Ghost Score ≥ 40 are considered suspicious</p>
            </div>
{no_ghosts_section}
        </div>
        
        <div class="footer">
            <p>Generated by OCI LoadBalancer Ghost Hunter - CloudCostChefs Edition</p>
            <p>Report created on {report_date}</p>
        </div>
    </div>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(
        description="OCI LoadBalancer Ghost Hunter - Hunt down forgotten and unused OCI Load Balancers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic ghost hunt across all compartments
  python oci-loadbalancer-ghosthunter.py
  
  # Hunt specific compartments only
  python oci-loadbalancer-ghosthunter.py --compartments ocid1.compartment.oc1..aaa...
  
  # Custom file paths
  python oci-loadbalancer-ghosthunter.py --csv-path /tmp/ghosts.csv --html-path /tmp/report.html
  
  # Use specific OCI config profile
  python oci-loadbalancer-ghosthunter.py --profile PROD --config-file ~/.oci/config
        """
    )
    
    parser.add_argument(
        '--config-file', 
        help='Path to OCI config file (default: ~/.oci/config)'
    )
    parser.add_argument(
        '--profile', 
        default='DEFAULT',
        help='OCI config profile to use (default: DEFAULT)'
    )
    parser.add_argument(
        '--compartments', 
        nargs='+',
        help='Specific compartment OCIDs to scan (default: all compartments)'
    )
    parser.add_argument(
        '--csv-path',
        default=f"oci_ghost_loadbalancers_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        help='Path for CSV export'
    )
    parser.add_argument(
        '--html-path',
        default=f"oci_ghost_loadbalancers_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html",
        help='Path for HTML report'
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Enable verbose logging'
    )
    
    args = parser.parse_args()
    
    # Setup logging
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.WARNING)
    
    try:
        # Initialize the ghost hunter
        hunter = OCILoadBalancerGhostHunter(args.config_file, args.profile)
        
        # Scan load balancers
        hunter.scan_load_balancers(args.compartments)
        
        print()
        
        # Export results
        hunter.export_to_csv(args.csv_path)
        print()
        hunter.generate_html_report(args.html_path)
        
        print()
        print_colored("🎉 Ghost hunt complete!", Colors.SUCCESS)
        
    except Exception as e:
        print_colored(f"❌ Ghost hunt failed: {e}", Colors.ERROR)
        if args.verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
