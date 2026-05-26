<img width="480" height="270" alt="image" src="https://github.com/user-attachments/assets/da8367a3-756a-4923-8f15-872abae67ccb" />

## CAMehameha

CAMehameha empowers Cloud Adoption Managers with battle-tested scripts, tools, and engagement frameworks to deliver stronger trusted-advisor conversations, accelerate adoption, and maximize customer value.
Here you will be able to find our Cloud Shell–ready scripts, tools, and small frameworks aimed at improving cost efficiency, governance, security hygiene, and also identifying opportunities to increase and optimize OCI resource consumption.




<img width="90" height="45" alt="image" src="https://github.com/user-attachments/assets/050bf7bb-872b-47cd-bdda-a4998e4e9f3a" /> **🐉LoadBalancer Cleanse (Jan-2026')**

Helps to optimization utility that scans all compartments to detect abandoned, underutilized, or misconfigured Classic and Network Load Balancers.
**Inspired by one of Dragon-Ball Z characters named "Android 16"**, a calm and precise nature, the tool analyzes listeners, backend sets, lifecycle state, age, and configuration health to calculate a “Ghost Score.” Results are delivered via detailed CSV and interactive HTML reports, helping teams eliminate waste, reduce cost, and restore balance across OCI environments.

What it does?

- 🔍 **Scans all OCI compartments** to detect abandoned, underutilized, or misconfigured Classic & Network LB. 
- ⚙️ **Analyzes** listeners, backend sets, health status, lifecycle state, age, and configuration drift.
- 📊 **Assigns a per-LB Ghost Score** to quantify risk, waste, and cleanup priority.
- 📄 **Generates a detailed CSV export** for offline analysis and governance.
- 🌐 **Produces an interactive HTML report** with scores, findings, and configuration details per Load Balancer.
- 🧹 **Helps teams eliminate waste**, reduce cost, and restore balance across OCI environments.



______________________________________________________________________________________________________________________________________________________________________________________________




<img width="93" height="52" alt="image" src="https://github.com/user-attachments/assets/fa459be1-d8b8-4a6e-9179-304211c552a2" /> **🐉 AD-to-AD Compute Migration (Feb-2026')**


BULMA helps teams safely migrate OCI compute instances from one Availability Domain (AD) to another using a backup & restore approach — without deleting or modifying the original server (beyond a controlled shutdown). **Inspired by Bulma from Dragon Ball-Z, a brilliant Corp engineer** who builds practical tools and makes complex operations feel manageable—the script guides the user through an interactive flow in OCI Cloud Shell, then executes the migration with step-by-step visibility and progress (0%–100%) so operators always know exactly what’s happening during a critical cutover.

What it does?

- 🧭 **Interactive selection** - prompts the operator to choose which server(s) to migrate and select the destination AD.
- 🛑 **Controlled shutdown** - safely stops the selected instance(s) to ensure consistent backups.
- 💾 **Backup & Restore migration** - creates backups for the boot volume + all block volumes, then restores them into the selected AD.
- 🚀 **New instance creation** - launches new instance from restored boot volume in the target AD, keeping the original server intact.
- 🌐 **Network rebuild** - recreates VNIC attachments using same subnets and NSGs as the source, including secondary VNICs.
- 📦 **Storage re-attachment** - reattaches restored block volumes to the new instance, preserving attachment type where possible.
- ⚖️ **Load Balancer awareness** (optional) - detects if the source instance is registered as a backend in an OCI classic Load Balancer and restores backend membership by adding the new instance
- 📊 **Maximum operator visibility** - prints real-time progress for each step with overall % + current step %, including “what is happening now” messages so it’s safe to run under pressure.



______________________________________________________________________________________________________________________________________________________________________________________________




<img width="90" height="47" alt="image" src="https://github.com/user-attachments/assets/2f6da761-7d2a-4812-a853-1412c2ceae59" /> **🐉 Shape Upgrade Advisor (March-2026')**


KING KAI helps teams modernize and optimize OCI compute by scanning all compartments to identify workloads still running on legacy AMD (E2/E3/E4) and legacy Intel (Standard2) shapes, and then validating whether the recommended next-gen upgrade targets are actually available for the tenancy/region. **Inspired by Dragon Ball Z’s King Kai—the wise mentor** who spots inefficiencies and guides upgrades with clarity—the tool summarizes risk, sizing (oCPU/Memory), estimated baseline monthly cost, and upgrade feasibility into clean CSV + HTML reports, helping reduce waste and unblock modernization.

What it does?

- 🔍 **Scans all OCI compartments to locate instances running on legacy shapes** (AMD E2/E3/E4 + Intel Standard2).
- 🧠 **Classifies findings by vendor family** (AMD vs Intel) and assigns Risk Level (HIGH for AMD E2 + E3, MEDIUM for others).
- 🧾 **Captures instance sizing**: oCPU + Memory [GB], plus lifecycle state for quick triage.
- ✅ **Validates upgrade feasibility** by checking if target shapes are available in the active region/AD and not blocked by hard quota signals.
- 📈 **Adds baseline monthly cost estimates** for current shapes and potential monthly delta add-on if upgraded.
- 📄 **Generates a CSV report** (includes OCID + compartment details) for deeper governance, automation, and follow-up actions.
- 🌐 **Produces a clean HTML report** split into two sections (AMD / Intel) for fast executive visibility and upgrade planning.



______________________________________________________________________________________________________________________________________________________________________________________________




<img width="90" height="56" alt="image" src="https://github.com/user-attachments/assets/937de710-9368-4120-b285-8bd978eee0ae" /> **🐉 Capacity Limit Observer - KAMI (April-2026')**



KAMI helps teams gain clear, multi-region visibility into OCI service limits, current usage, and capacity-risk signals by scanning OCI limits across regions and presenting them in a clean, searchable HTML dashboard. Instead of manually drilling through the OCI Console region by region and service by service, KAMI centralizes the view into one practical report that highlights where usage is approaching thresholds and where proactive action may be required.

Inspired by Dragon Ball’s Kami — the guardian who watches from above — the tool acts as a visibility layer for OCI limits and capacity awareness. It helps CAMs, cloud teams, and customer-facing stakeholders quickly identify limit pressure, prepare service-limit discussions, reduce deployment delays, and support better capacity planning across OCI regions.

- 🔍 **Scans OCI limits and usage** across multiple regions to provide centralized visibility.
- 🌍 **Organizes results** by region, service, and limit name for easier investigation and follow-up.
- 📊 **Shows current usage vs approved limits**, helping teams understand remaining headroom.
- 🚦**Highlights threshold** risk signals, such as limits approaching 80% / 90% utilization.
- 🧭 **Helps identify** where service-limit requests may be needed before deployments are blocked.
- 🔎 **Includes searchable and filterable views** to quickly locate specific services, regions, or limits.
- 📄 **Generates structured outputs** that can support governance, reporting, escalation, and customer conversations.
- 🌐 **Produces a clean HTML dashboard** for fast executive and operational visibility.
