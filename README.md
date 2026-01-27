<img width="1920" height="1080" alt="image" src="https://github.com/user-attachments/assets/da8367a3-756a-4923-8f15-872abae67ccb" />

## CAMehameha

CAMehameha empowers Cloud Adoption Managers with battle-tested scripts, tools, and engagement frameworks to deliver stronger trusted-advisor conversations, accelerate adoption, and maximize customer value.
Here you will be able to find our Cloud Shell–ready scripts, tools, and small frameworks aimed at improving cost efficiency, governance, security hygiene, and also identifying opportunities to increase and optimize OCI resource consumption.




### 1. 🐉 LoadBalancer Cleanse – Android16
<img width="180" height="90" alt="image" src="https://github.com/user-attachments/assets/050bf7bb-872b-47cd-bdda-a4998e4e9f3a" />

Our first script which helps to optimization utility that scans all compartments to detect abandoned, underutilized, or misconfigured Classic and Network Load Balancers.
**Inspired by one of Dragon-Ball Z characters named "Android 16"**, a calm and precise nature, the tool analyzes listeners, backend sets, lifecycle state, age, and configuration health to calculate a “Ghost Score.” Results are delivered via detailed CSV and interactive HTML reports, helping teams eliminate waste, reduce cost, and restore balance across OCI environments.

What it does?

- 🔍 **Scans all OCI compartments** to detect abandoned, underutilized, or misconfigured Classic & Network LB. 
- ⚙️ **Analyzes** listeners, backend sets, health status, lifecycle state, age, and configuration drift.
- 📊 **Assigns a per-LB Ghost Score** to quantify risk, waste, and cleanup priority.
- 📄 **Generates a detailed CSV export** for offline analysis and governance.
- 🌐 **Produces an interactive HTML report** with scores, findings, and configuration details per Load Balancer.
- 🧹 **Helps teams eliminate waste**, reduce cost, and restore balance across OCI environments.
<img width="700" height="452" alt="Screenshot 2026-01-20 173012" src="https://github.com/user-attachments/assets/9b8e5253-6172-4a90-beec-6d12729ea8f7" />



______________________________________________________________________________________________________________________________________________________________________________________________



### 2. 🐉 Shape Upgrade Advisor - KING KAI 
<img width="180" height="95" alt="image" src="https://github.com/user-attachments/assets/2f6da761-7d2a-4812-a853-1412c2ceae59" />

Sniffs out forgotten cloud resources in your OCI tenancy—no manual sleuthing required.

Scans every compartment (including root) and flags:
  • Orphaned Block Volumes (no attachments)
  • Unattached Reserved Public IPs (REGION + AD scopes)
  • Empty Network Security Groups (NSGs with zero VNICs)
  • Load Balancers with no backends (validated via list_backends)
  • Old-gen Compute instances (shape matches regex)
  • Resources with absolutely NO tags
  • Resources with sketchy names (test|temp|demo|old|backup|poc)

PLUS (KING KAI Upgrade Advisor):
  • When old shapes are found, shows:
      - counts (AMD E2/E3/E4 + Intel Standard1/2, including zeros)
      - recommended upgrade targets:
          AMD  -> VM.Standard.E5.Flex and/or VM.Standard.E6.Flex
          Intel-> VM.Standard3.Flex and VM.Optimized3.Flex
      - per-AD shape catalog availability (✅/❌) and E5/E6 series in catalog
      - tenancy limits snapshot (including explicit 0) + available/used when supported
  • Included in the HTML report (very important), and also printed to console.
