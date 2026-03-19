<img width="1920" height="1080" alt="image" src="https://github.com/user-attachments/assets/da8367a3-756a-4923-8f15-872abae67ccb" />

## CAMehameha

CAMehameha empowers Cloud Adoption Managers with battle-tested scripts, tools, and engagement frameworks to deliver stronger trusted-advisor conversations, accelerate adoption, and maximize customer value.
Here you will be able to find our Cloud Shell–ready scripts, tools, and small frameworks aimed at improving cost efficiency, governance, security hygiene, and also identifying opportunities to increase and optimize OCI resource consumption.




### 1. 🐉 LoadBalancer Cleanse – Android16
<img width="180" height="90" alt="image" src="https://github.com/user-attachments/assets/050bf7bb-872b-47cd-bdda-a4998e4e9f3a" />

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




### 2. 🐉 AD-to-AD Compute Migration Toolkit - BULMA  
<img width="186" height="105" alt="image" src="https://github.com/user-attachments/assets/fa459be1-d8b8-4a6e-9179-304211c552a2" />

BULMA helps teams safely migrate OCI compute instances from one Availability Domain (AD) to another using a backup & restore approach — without deleting or modifying the original server (beyond a controlled shutdown). **Inspired by Bulma from Dragon Ball-Z, a brilliant Corp engineer** who builds practical tools and makes complex operations feel manageable—the script guides the user through an interactive flow in OCI Cloud Shell, then executes the migration with step-by-step visibility and progress (0%–100%) so operators always know exactly what’s happening during a critical cutover.

What it does?

- 🧭 Interactive selection - prompts the operator to choose which server(s) to migrate and select the destination AD.
- 🛑 Controlled shutdown - safely stops the selected instance(s) to ensure consistent backups.
- 💾 Backup & Restore migration - creates backups for the boot volume + all attached block volumes, then restores them into the destination AD.
- 🚀 New instance creation - launches new compute instance from the restored boot volume in the target AD, keeping the original server intact.
- 🌐 Network rebuild - recreates VNIC attachments using the same subnets and NSGs as the source, including secondary VNICs when present.
- 📦 Storage re-attachment - reattaches restored block volumes to the new instance, preserving attachment type where possible.
- ⚖️ Load Balancer awareness (optional) - detects if the source instance is registered as a backend in an OCI classic Load Balancer and restores backend membership by adding the new instance
- 📊 Maximum operator visibility - prints real-time progress for each step with overall % + current step %, including “what is happening now” messages so it’s safe to run under pressure.



______________________________________________________________________________________________________________________________________________________________________________________________




### 3. 🐉 Shape Upgrade Advisor - KING KAI (Rollout MAR-2026'📅)
<img width="180" height="95" alt="image" src="https://github.com/user-attachments/assets/2f6da761-7d2a-4812-a853-1412c2ceae59" />

KING KAI helps teams modernize and optimize OCI compute by scanning all compartments to identify workloads still running on legacy AMD (E2/E3/E4) and legacy Intel (Standard2) shapes, and then validating whether the recommended next-gen upgrade targets are actually available for the tenancy/region. **Inspired by Dragon Ball Z’s King Kai—the wise mentor** who spots inefficiencies and guides upgrades with clarity—the tool summarizes risk, sizing (oCPU/Memory), estimated baseline monthly cost, and upgrade feasibility into clean CSV + HTML reports, helping reduce waste and unblock modernization.

What it does?

- 🔍 **Scans all OCI compartments to locate instances running on legacy shapes** (AMD E2/E3/E4 + Intel Standard2).
- 🧠 **Classifies findings by vendor family** (AMD vs Intel) and assigns Risk Level (HIGH for AMD E2 + E3, MEDIUM for others).
- 🧾 **Captures instance sizing**: oCPU + Memory [GB], plus lifecycle state for quick triage.
- ✅ **Validates upgrade feasibility** by checking if target shapes are available in the active region/AD and not blocked by hard quota signals.
- 📈 **Adds baseline monthly cost estimates** for current shapes and potential monthly delta add-on if upgraded.
- 📄 **Generates a CSV report** (includes OCID + compartment details) for deeper governance, automation, and follow-up actions.
- 🌐 **Produces a clean HTML report** split into two sections (AMD / Intel) for fast executive visibility and upgrade planning.
