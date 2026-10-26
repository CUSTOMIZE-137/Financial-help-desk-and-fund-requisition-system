# 💳 Financial Requisition & Help Desk System

An enterprise financial help desk and fund requisition system designed to streamline, track, and audit organizational fund requests. The system routes requisitions through a strict 5-stage sequential approval chain while maintaining data integrity, accountability loops, and audit trails.

---

## 🔄 Approval Workflow Strategy

Fund requisitions progress through five distinct sequential stages:

### Workflow Summary Diagram

```text
[1. Employee / Officer]
│ Submits request & TORs
▼
[2. Supervisor]
│ Departmental review & budget alignment
▼
[3. Monitoring & Evaluation (M&E)]
│ Strategic verification & project logframe alignment
▼
[4. Program Director]
│ Final strategic sign-off & authorization
▼
[5. Finance]
│ Pre-audit validation, payment processing & disbursement
▼
[Awaiting Accountability]
│ Requester uploads liquidation receipts & activity report
▼
[Completed]
```

---

## Stage-by-Stage Breakdown

### Phase 1: Initiation & Strategy Alignment

#### 1. Employee / Officer (Initiation)
* **Primary Role:** Requisitioner
* **Key Actions:**
  * Drafts the formal funding request.
  * Develops detailed **Terms of Reference (TORs)** outlining project scope, deliverables, timeline, and estimated itemized budget.
* **Output:** Initial requisition packet submitted for departmental review.

#### 2. Supervisor (Departmental Review)
* **Primary Role:** Departmental Approver
* **Key Actions:**
  * Reviews operational necessity and feasibility of the requested activity.
  * Verifies budget line availability within the departmental allocation.
  * Ensures resource planning aligns with quarterly department deliverables.
* **Output:** Departmental endorsement / First-level management approval.

#### 3. Monitoring & Evaluation - M&E (Strategic Verification)
* **Primary Role:** Quality & Impact Assurance
* **Key Actions:**
  * Cross-checks TOR activities against the project **Logical Framework (Logframe)**.
  * Validates planned key performance indicators (KPIs), targets, and expected output metrics.
  * Ensures compliance with donor guidelines and organizational strategic objectives.
* **Output:** M&E strategic compliance clearance.

#### 4. Program Director (Executive Authorization)
* **Primary Role:** Final Operational Approver
* **Key Actions:**
  * Evaluates overall strategic impact and project priorities.
  * Grants final executive sign-off for commit funds.
* **Output:** Authorized requisition ready for financial processing.

---

### Phase 2: Disbursement & Accountability

#### 5. Finance (Pre-Audit & Disbursement)
* **Primary Role:** Financial Control & Treasury
* **Key Actions:**
  * Conducts pre-audit compliance check (verifies General Ledger coding, tax implications, and vendor procurement compliance).
  * Processes payment or cash advance disbursement.
  * Records obligation in the accounting ledger.
* **Output:** Fund disbursement to vendor/requester; transition to open advance status.

#### 6. Awaiting Accountability (Liquidation & Reporting)
* **Primary Role:** Requester / Finance Control
* **Key Actions:**
  * Requester executes the planned activity per approved TORs.
  * Requester uploads original expenditure proof (receipts, tax invoices, attendance sheets, contracts).
  * Requester submits a concise Activity/Outcome Report summarizing deliverables achieved.
* **Output:** Submitted liquidation packet.

#### 7. Completed (Reconciliation & Closeout)
* **Primary Role:** Finance / Internal Audit
* **Key Actions:**
  * Finance reviews and reconciles submitted receipts against disbursed cash advance.
  * Processes recovery of unspent balances or reimbursement for authorized overruns.
  * Formally clears the open advance in the ERP/financial system and archives records.
* **Output:** Fully reconciled and closed requisition file.
---

## ✨ Key Features

* **5-Stage Sequential Approval Chain:** Strict status transitions ensuring requests cannot skip approval levels.
* **Revision & Clarification Loops:** Approvers can flag requests with `REVISION_REQUESTED` to send them back to the requester for adjustments without destroying the record.
* **Post-Disbursement Accountability (Liquidation):** Automated transition to `AWAITING_ACCOUNTABILITY` upon payment, requiring upload of final receipts and activity reports before closing.
* **Role-Based Access Control (RBAC):** Tailored views for `EMPLOYEE`, `SUPERVISOR`, `ME_OFFICER`, `PROGRAM_DIRECTOR`, `FINANCE`, and `ADMIN`.
* **Delegation of Authority:** Temporary delegate assignment mechanism to bypass bottlenecks when approvers are out of office.
* **Immutable Audit Trail:** Log entries (`requisition_logs`) capturing actor IDs, timestamps, status shifts, and comments for full compliance and auditing.

---

## 🏗️ Core Architecture & Data Model

The backend is built around a deterministic state machine managing the following key entities:

* **`Users`**: Profile management, roles, and delegation settings.
* **`Projects` & `BudgetLines`**: Financial tracking per grant or organizational department.
* **`Requisitions`**: Core requisition records containing requested amounts, justification, and state.
* **`AccountabilitySubmissions`**: Post-disbursement receipts, invoices, and completion reports.
* **`RequisitionLogs`**: Append-only log table for auditability.

---

## 🛠️ Technology Stack

* **Backend:** Python (Django / Flask) or Node.js (Express)
* **Database:** PostgreSQL / MySQL
* **Frontend:** Web Dashboard (HTML5 / Tailwind CSS / JS framework)
* **Authentication:** JWT / Session-based Role Auth

---

## 🚀 Quickstart & Setup

### Prerequisites
* Python 3.10+ / Node.js 18+
* PostgreSQL or SQLite (for local development)

### Installation Steps

1. **Clone the Repository:**
   ```bash
   git clone [https://github.com/CUSTOMIZE-137/financial-help-desk-and-fund-requisition-system.git](https://github.com/CUSTOMIZE-137/financial-help-desk-and-fund-requisition-system.git)
   cd financial-help-desk-and-fund-requisition-system
