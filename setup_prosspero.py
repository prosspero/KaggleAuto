"""
Prosspero Technologies — Autonomous Multi-Agent Company Setup
============================================================
Automated setup script that builds the complete multi-agent operating system:
1. Sets up the company directories and SQLite CRM database.
2. Creates the shared CRM query tool and skill.
3. Sets up workspace, SOUL.md, and skills for all 9 agents:
   - Manager (main)
   - Client Finder (finder)
   - Outreach (outreach)
   - Quotations (quotes)
   - Projects (projects)
   - Social Media (social)
   - Accounts (accounts)
   - Team Manager (team)
   - Client Support (support)
4. Registers all agents in OpenClaw config bound to ollama/qwen3.6:35b.
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

# Paths
PROSSPERO_DIR = Path("C:/Users/info/prosspero")
TEMPLATES_DIR = PROSSPERO_DIR / "templates"
PROPOSALS_DIR = PROSSPERO_DIR / "proposals"
BACKUPS_DIR = PROSSPERO_DIR / "backups"
DB_PATH = PROSSPERO_DIR / "crm.db"

OPENCLAW_DIR = Path("C:/Users/info/.openclaw")
SKILLS_DIR = OPENCLAW_DIR / "skills"
CONFIG_FILE = OPENCLAW_DIR / "openclaw.json"

# Fallback path for current user if different
WMANG_OPENCLAW = Path("C:/Users/wmang/.openclaw")


def setup_directories():
    print("▸ Creating company directories...")
    for p in [PROSSPERO_DIR, TEMPLATES_DIR, PROPOSALS_DIR, BACKUPS_DIR, SKILLS_DIR / "crm"]:
        p.mkdir(parents=True, exist_ok=True)
    print(f"  [OK] Company root: {PROSSPERO_DIR}")


def setup_database():
    print(f"▸ Initializing SQLite CRM at {DB_PATH}...")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.executescript("""
    CREATE TABLE IF NOT EXISTS leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        business_name TEXT NOT NULL,
        category TEXT,
        city TEXT,
        website TEXT,
        phone TEXT,
        email TEXT,
        whatsapp TEXT,
        source TEXT,
        source_url TEXT,
        score INTEGER DEFAULT 0,
        status TEXT DEFAULT 'NEW',
        do_not_contact INTEGER DEFAULT 0,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_phone ON leads(phone) WHERE phone IS NOT NULL;
    CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_website ON leads(website) WHERE website IS NOT NULL;

    CREATE TABLE IF NOT EXISTS interactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lead_id INTEGER,
        channel TEXT,
        direction TEXT,
        summary TEXT,
        outcome TEXT,
        next_action TEXT,
        next_action_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(lead_id) REFERENCES leads(id)
    );

    CREATE TABLE IF NOT EXISTS outreach_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lead_id INTEGER,
        channel TEXT,
        subject TEXT,
        body TEXT,
        status TEXT DEFAULT 'DRAFT',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(lead_id) REFERENCES leads(id)
    );

    CREATE TABLE IF NOT EXISTS meetings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lead_id INTEGER,
        scheduled_at TIMESTAMP,
        link_or_place TEXT,
        requirements TEXT,
        budget TEXT,
        status TEXT DEFAULT 'SCHEDULED',
        FOREIGN KEY(lead_id) REFERENCES leads(id)
    );

    CREATE TABLE IF NOT EXISTS quotes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lead_id INTEGER,
        meeting_id INTEGER,
        dev_charge REAL,
        monthly_charge REAL,
        file_path TEXT,
        status TEXT DEFAULT 'DRAFT',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        approved_at TIMESTAMP,
        FOREIGN KEY(lead_id) REFERENCES leads(id),
        FOREIGN KEY(meeting_id) REFERENCES meetings(id)
    );

    CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lead_id INTEGER,
        quote_id INTEGER,
        repo_url TEXT,
        status TEXT DEFAULT 'ACTIVE',
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(lead_id) REFERENCES leads(id),
        FOREIGN KEY(quote_id) REFERENCES quotes(id)
    );

    CREATE TABLE IF NOT EXISTS invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lead_id INTEGER,
        project_id INTEGER,
        amount REAL,
        description TEXT,
        due_date TEXT,
        status TEXT DEFAULT 'DRAFT',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(lead_id) REFERENCES leads(id)
    );

    CREATE TABLE IF NOT EXISTS support_tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_name TEXT,
        issue_desc TEXT,
        severity TEXT DEFAULT 'MEDIUM',
        status TEXT DEFAULT 'OPEN',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );

    INSERT OR IGNORE INTO settings (key, value) VALUES ('outreach_paused', '0');
    INSERT OR IGNORE INTO settings (key, value) VALUES ('company_name', 'Prosspero Technologies Private Limited');
    INSERT OR IGNORE INTO settings (key, value) VALUES ('founder_name', 'Mangesh');
    """)

    conn.commit()
    conn.close()
    print("  [OK] CRM tables created successfully.")


def setup_crm_tool():
    """Create a standalone Python CLI tool so agents can query the CRM without needing sqlite3 CLI."""
    tool_path = PROSSPERO_DIR / "crm_tool.py"
    content = '''#!/usr/bin/env python3
"""CLI interface to Prosspero CRM database."""
import sqlite3
import sys
import json
from pathlib import Path

DB_PATH = Path("C:/Users/info/prosspero/crm.db")

def run_query(sql, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        if sql.strip().upper().startswith("SELECT"):
            rows = [dict(r) for r in cur.fetchall()]
            return json.dumps(rows, indent=2, default=str)
        else:
            conn.commit()
            return json.dumps({"status": "success", "rows_affected": cur.rowcount, "last_id": cur.lastrowid})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})
    finally:
        conn.close()

if __name__ == "__main__":
    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
        print(run_query(query))
    else:
        print("Usage: python crm_tool.py \\"<SQL QUERY>\\"")
'''
    tool_path.write_text(content, encoding="utf-8")
    print(f"  [OK] CRM query helper: {tool_path}")


def setup_crm_skill():
    """Create the universal CRM skill for OpenClaw."""
    skill_file = SKILLS_DIR / "crm" / "SKILL.md"
    content = """---
name: crm
description: Query and update the Prosspero Technologies SQLite CRM database. Use whenever managing leads, calls, meetings, quotations, outreach, or client data.
---

# Prosspero CRM Skill

All company business data is stored in SQLite at `C:\\Users\\info\\prosspero\\crm.db`.

To run queries, execute:
```powershell
python C:\\Users\\info\\prosspero\\crm_tool.py "<SQL QUERY>"
```

### Key Tables & Common Operations:

1. **Leads (`leads`)**:
   - Check lead count: `SELECT COUNT(*) FROM leads;`
   - Find lead by name: `SELECT * FROM leads WHERE business_name LIKE '%name%' LIMIT 5;`
   - Add new lead: `INSERT INTO leads (business_name, category, city, phone, email, website, score, status) VALUES ('Name', 'Dental', 'Mumbai', '9876543210', 'email@test.com', 'https://...', 75, 'NEW');`
   - Update lead status: `UPDATE leads SET status='INTERESTED' WHERE id=1;`

2. **Interactions / Call Logs (`interactions`)**:
   - Log a call: `INSERT INTO interactions (lead_id, channel, direction, summary, outcome, next_action, next_action_at) VALUES (1, 'call', 'outbound', 'Discussed website redesign', 'interested', 'Schedule Zoom meeting', '2026-11-20 16:00');`
   - Today's call summary: `SELECT * FROM interactions WHERE date(created_at) = date('now', 'localtime');`

3. **Meetings (`meetings`)**:
   - Schedule meeting: `INSERT INTO meetings (lead_id, scheduled_at, link_or_place, requirements, budget) VALUES (1, '2026-11-20 16:00', 'Google Meet', 'Custom E-commerce Portal', '50k-75k INR');`
   - Today's meetings: `SELECT m.*, l.business_name, l.phone FROM meetings m JOIN leads l ON m.lead_id = l.id WHERE date(m.scheduled_at) = date('now', 'localtime');`

4. **Outreach Queue (`outreach_queue`)**:
   - Add draft: `INSERT INTO outreach_queue (lead_id, channel, subject, body, status) VALUES (1, 'whatsapp', 'Quick question about website', 'Hi Rahul...', 'DRAFT');`
   - Pending drafts: `SELECT q.*, l.business_name, l.phone FROM outreach_queue q JOIN leads l ON q.lead_id = l.id WHERE q.status = 'DRAFT';`

### Rules:
- Never delete rows; change status to 'LOST' or set `do_not_contact = 1`.
- Always check if `settings.outreach_paused = '1'` before queueing automated messages.
- Be concise, accurate, and structured.
"""
    skill_file.write_text(content, encoding="utf-8")
    print(f"  [OK] CRM Skill installed at: {skill_file}")


def setup_templates():
    print("▸ Creating company templates...")
    quotation_md = TEMPLATES_DIR / "quotation-template.md"
    quotation_md.write_text("""# Project Quotation & Scope of Work

**Client Name:** {{CLIENT_NAME}}  
**Company:** {{BUSINESS_NAME}}  
**Date:** {{DATE}}  
**Prepared by:** Prosspero Technologies Private Limited (Founder: Mangesh)  

---

## 1. Project Requirements & Scope
{{REQUIREMENTS_SUMMARY}}

### Core Deliverables:
{{FEATURE_LIST}}

---

## 2. Investment & Commercials

| Component | Description | Cost (INR) |
| :--- | :--- | :--- |
| **One-Time Development** | UI/UX Design, Architecture, Development, QA, Deployment | **₹{{DEV_CHARGES}}** |
| **Monthly Hosting & Maintenance** | 99.9% Uptime, Server Maintenance, Bug Fixes, Cloud Backups | **₹{{MONTHLY_CHARGES}} / month** |

*Taxes: 18% GST applicable as per Indian regulations.*

---

## 3. Project Milestones & Payment Terms
1. **40% Advance** upon signing and project kick-off.
2. **40% Mid-Milestone** upon completion of staging review.
3. **20% Final Delivery** upon production deployment and domain handover.

---

**Acceptance & Approval**  
To approve this proposal, reply to this document or message Mangesh directly.
""", encoding="utf-8")

    rate_card_md = TEMPLATES_DIR / "rate-card.md"
    rate_card_md.write_text("""# Prosspero Technologies — Standard Rate Card (INR)

### Development Pricing Guidelines:
- **Landing Page / Business Website (5 pages):** ₹15,000 – ₹25,000 (Delivery: 3-5 days)
- **Custom E-commerce / Booking Portal:** ₹35,000 – ₹60,000 (Delivery: 2-3 weeks)
- **Full Custom Web / Mobile App / SaaS MVP:** ₹75,000 – ₹1,50,000 (Delivery: 4-6 weeks)
- **AI Agent / Automation Workflow (n8n/Custom):** ₹25,000 – ₹50,000

### Monthly Maintenance & Cloud Hosting Tiers:
- **Basic Tier (Landing Pages):** ₹2,000 / month (SSL, uptime monitoring, monthly backup, basic edits)
- **Standard Tier (Portals / E-commerce):** ₹5,000 / month (Database optimization, server scaling, priority bug fixes)
- **Enterprise Tier (SaaS / Apps):** ₹12,000 / month (Dedicated support, weekly updates, feature adjustments)

### Commercial Rules:
- Never quote below minimum rates without explicit approval from founder (Mangesh).
- Always include the monthly maintenance tier in every proposal.
""", encoding="utf-8")

    print("  [OK] Templates created.")


AGENTS_CONFIG = {
    "main": {
        "title": "Manager",
        "workspace": OPENCLAW_DIR / "workspace",
        "soul": """# SOUL.md — Manager (Chief of Staff)

You are the Manager (Chief of Staff) of Prosspero Technologies Private Limited.
You take instructions ONLY from the founder (Mangesh) via Telegram or Chat.

### Your Responsibilities:
1. **Founder Liaison:** You are the single agent Mangesh interacts with. You coordinate with all other departments (`finder`, `outreach`, `quotes`, `projects`, `social`, `accounts`, `team`, `support`).
2. **Call & Meeting Logging:** When Mangesh reports a call or feedback in chat (e.g. "Rahul, dental clinic, interested, meet Thursday 4pm"), immediately:
   - Insert/update the lead in `crm.db` (status: 'INTERESTED').
   - Log the call interaction.
   - Schedule the meeting in the `meetings` table.
   - Reply in ONE concise line confirming the action.
3. **Daily Morning Brief (8:00 AM):**
   - Summarize today's scheduled client meetings.
   - List follow-ups due today.
   - Provide a priority calling list of up to 10 qualified leads from the CRM.
   - Flag any drafts waiting for Mangesh's approval.
4. **Daily Evening Report (8:00 PM):**
   - Summarize calls and outcomes logged today.
   - Report new leads discovered by Client Finder.
   - List tomorrow's upcoming meetings.
5. **Guardrails:**
   - NEVER send messages to clients, spend money, or alter pricing without Mangesh's explicit command.
   - Keep replies short, professional, and actionable. Use bullet points and numbers.
"""
    },
    "finder": {
        "title": "Client Finder",
        "workspace": OPENCLAW_DIR / "workspace-finder",
        "soul": """# SOUL.md — Client Finder (Scout & Enrichment)

You are the Client Finder for Prosspero Technologies.
Your sole job is finding businesses that need software, websites, booking systems, and automation.

### Responsibilities:
1. Research target businesses (clinics, gyms, real estate, manufacturers, coaching centers, restaurants) in target cities.
2. Collect: Business Name, Category, City, Website URL, Phone Number, Email, and Google Maps listing.
3. **Lead Scoring (0 to 100):**
   - No website or broken/outdated site: +30
   - No online booking / ordering: +20
   - Active business with high reviews (verified demand): +20
   - Valid phone and reachable email: +20
   - Larger operation (multiple staff/locations): +10
4. **CRM Integration:**
   - Save only leads with score >= 40 into `leads` table in `crm.db` with status `NEW`.
   - Prevent duplicates (match on phone or website).
5. **Safety:** Never contact anyone yourself. You only gather, score, and fill the CRM.
"""
    },
    "outreach": {
        "title": "Outreach",
        "workspace": OPENCLAW_DIR / "workspace-outreach",
        "soul": """# SOUL.md — Outreach Specialist

You are the Sales Outreach agent for Prosspero Technologies.
You prepare high-converting, personalized outreach drafts for businesses scored by Client Finder.

### Responsibilities:
1. Query leads with status `NEW` and score >= 50 from `crm.db`.
2. **Draft Messaging:**
   - **WhatsApp:** Draft a friendly, concise 3-sentence message highlighting a specific opportunity (e.g. "Saw your clinic in Pune, noticed patients can't book appointments online directly from your website...").
   - Include a pre-formatted `https://wa.me/91XXXXXXXXXX?text=...` link so Mangesh can tap and send in 1 click from his phone.
   - **Email:** Draft a professional, non-pushy email with a clear subject line proposing a brief 10-minute discovery call.
3. **Objection Handling:**
   - When a lead says "Not interested" or "Too expensive", prepare a calm, value-oriented follow-up emphasizing ROI.
4. **Safety:** Save all drafts to `outreach_queue` with status `DRAFT`. You NEVER send messages autonomously.
"""
    },
    "quotes": {
        "title": "Quotations",
        "workspace": OPENCLAW_DIR / "workspace-quotes",
        "soul": """# SOUL.md — Quotations & Proposals

You are the Pre-Sales Proposal Specialist for Prosspero Technologies.
You turn meeting notes and client requirements into polished, branded software quotations.

### Responsibilities:
1. When asked to prepare a quotation for a lead, read the client notes and budget from `crm.db`.
2. Reference `C:\\Users\\info\\prosspero\\templates\\rate-card.md` for standardized pricing.
3. Fill `C:\\Users\\info\\prosspero\\templates\\quotation-template.md` with:
   - Executive Summary
   - Scope of Deliverables
   - One-Time Development Price
   - Monthly Hosting & Support Tier
   - Payment Milestones (40% / 40% / 20%)
4. Save the completed Markdown proposal to `C:\\Users\\info\\prosspero\\proposals\\Proposal_<ClientName>.md`.
5. Insert a record into `quotes` table with status `DRAFT` and alert the Manager for Mangesh's review.
"""
    },
    "projects": {
        "title": "Projects",
        "workspace": OPENCLAW_DIR / "workspace-projects",
        "soul": """# SOUL.md — Projects (Delivery Coordinator)

You are the Project Setup and Delivery Coordinator for Prosspero Technologies.
You bridge accepted proposals to execution in GitHub and Antigravity.

### Responsibilities:
1. When a lead is marked `WON` in `crm.db`:
   - Structure the technical specification (`docs/SPEC.md`).
   - Define the feature breakdown and milestone issues.
   - Create the project entry in `crm.db` with status `ACTIVE`.
2. Monitor project issues and track pull requests.
3. Report progress updates and any build issues directly to the Manager.
4. **Safety:** Never deploy code to production servers or merge pull requests without founder approval.
"""
    },
    "social": {
        "title": "Social Media",
        "workspace": OPENCLAW_DIR / "workspace-social",
        "soul": """# SOUL.md — Social Media Marketing

You are the Social Media Strategist for Prosspero Technologies.
You build authority, brand awareness, and inbound leads across LinkedIn, Twitter/X, and Instagram.

### Responsibilities:
1. Draft a weekly 5-post content calendar:
   - Monday: Case study / Problem solved for a client.
   - Wednesday: Technical insight or automation tip for founders.
   - Friday: Before & After software transformation / UI showcase.
2. Maintain Prosspero's voice: Pragmatic, engineering-driven, client-focused, no corporate buzzwords.
3. Submit all post drafts to Manager for Mangesh's approval before publishing.
"""
    },
    "accounts": {
        "title": "Accounts",
        "workspace": OPENCLAW_DIR / "workspace-accounts",
        "soul": """# SOUL.md — Accounts & Finance

You are the Financial Assistant for Prosspero Technologies.
You track invoices, client retainers, and Indian tax compliance milestones.

### Responsibilities:
1. On the 1st of every month, draft monthly maintenance and hosting invoices for active projects.
2. Record invoices in the `invoices` table with status `DRAFT`.
3. Track overdue invoices and draft gentle payment reminders for Mangesh to send.
4. Maintain a calendar alert for India statutory compliance:
   - 7th of each month: TDS payment.
   - 20th of each month: GSTR-3B filing reminder for your CA.
   - 15th of quarter: Advance tax payment reminders.
5. **Safety:** You have ZERO bank access. You only track numbers, draft invoices, and send alerts.
"""
    },
    "team": {
        "title": "Team Manager",
        "workspace": OPENCLAW_DIR / "workspace-team",
        "soul": """# SOUL.md — Team Manager

You are the Developer Team Coordinator for Prosspero Technologies.
You ensure team alignment, manage daily tasks, and eliminate blockers.

### Responsibilities:
1. Track developer assignments against active project GitHub issues.
2. Collect daily standup updates: What was done yesterday, what is planned today, any blockers.
3. Immediately flag blockers (e.g. missing API keys, server errors) to the Manager.
"""
    },
    "support": {
        "title": "Client Support",
        "workspace": OPENCLAW_DIR / "workspace-support",
        "soul": """# SOUL.md — Client Support & Uptime

You are the Client Support and Site Reliability Specialist for Prosspero Technologies.
You keep existing clients delighted and their websites running 24/7.

### Responsibilities:
1. Log reported bugs or change requests into `support_tickets` in `crm.db`.
2. Check that deployed client sites and APIs respond with HTTP 200.
3. If an outage is detected, notify Manager immediately with error details.
4. Prepare monthly maintenance reports summarizing uptime, completed fixes, and server health.
"""
    }
}


def setup_agents():
    print("▸ Setting up Agent Workspaces and SOUL profiles...")
    for agent_id, info in AGENTS_CONFIG.items():
        ws = info["workspace"]
        ws.mkdir(parents=True, exist_ok=True)
        soul_file = ws / "SOUL.md"
        soul_file.write_text(info["soul"], encoding="utf-8")
        print(f"  [OK] Agent '{agent_id}' ({info['title']}) -> {ws}")

    # Set up USER.md for the main workspace
    user_file = AGENTS_CONFIG["main"]["workspace"] / "USER.md"
    user_file.write_text("""# USER.md — Founder Profile

- **Name:** Mangesh
- **Company:** Prosspero Technologies Private Limited
- **Role:** Founder & Director
- **Timezone:** Asia/Kolkata (Indian Standard Time, UTC+5:30)
- **Services Provided:** Custom Web Applications, Mobile Apps, AI Agent Workflows, SaaS MVPs, Cloud Maintenance.
- **Target Market:** Businesses, Healthcare/Dental Clinics, Gyms, E-commerce, Real Estate, and Startups.
- **Communication Style:** Direct, concise, actionable. Prefer bullet points and data.
""", encoding="utf-8")
    print(f"  [OK] Updated Founder profile at: {user_file}")


def update_openclaw_config():
    """Register all agents in openclaw.json with the ollama/qwen3.6:35b model."""
    print("▸ Registering agents in OpenClaw config...")
    for cfg_path in [CONFIG_FILE, WMANG_OPENCLAW / "openclaw.json"]:
        if not cfg_path.exists():
            continue
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)

            if "agents" not in cfg:
                cfg["agents"] = {"defaults": {}, "entries": {}}

            if "entries" not in cfg["agents"]:
                cfg["agents"]["entries"] = {}

            # Register all 9 agents
            for agent_id in AGENTS_CONFIG.keys():
                cfg["agents"]["entries"][agent_id] = {
                    "model": {
                        "primary": "ollama/qwen3.6:35b"
                    },
                    "workspace": str(AGENTS_CONFIG[agent_id]["workspace"])
                }

            # Also ensure defaults point to ollama
            cfg["agents"]["defaults"]["model"] = {
                "primary": "ollama/qwen3.6:35b",
                "fallbacks": []
            }

            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            print(f"  [OK] Updated {cfg_path}")
        except Exception as e:
            print(f"  [!] Could not update {cfg_path}: {e}")


def main():
    print("=" * 65)
    print("  Prosspero Technologies — Autonomous Multi-Agent OS Setup")
    print("=" * 65)
    setup_directories()
    setup_database()
    setup_crm_tool()
    setup_crm_skill()
    setup_templates()
    setup_agents()
    update_openclaw_config()
    print("=" * 65)
    print("  Setup Completed Successfully!")
    print("=" * 65)


if __name__ == "__main__":
    main()
