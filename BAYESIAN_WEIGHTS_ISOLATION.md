# ✅ Bayesian Model Weights Isolation & Approval Workflow

## 🔒 CRITICAL GUARANTEE: Unapproved Weights Never Affect Calculations

**User Requirement:** "Make sure it doesn't change values, only after admin approved the new values can be used to calculate"

**Status:** ✅ **FULLY IMPLEMENTED**

---

## 1. Complete Workflow Diagram

```
┌──────────────────────────────────────────────────────────────────────────┐
│ BAYESIAN UPDATER PROCESS (Runs every 24 hours)                          │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  1. Analyzes 47 TRAIN trades                                           │
│  2. Detects win rate: 72.3%                                            │
│  3. Proposes weight changes:                                           │
│     - VOLUME_ZSCORE: 3.0 → 4.5                                         │
│     - RS_RANK: 7.0 → 6.5                                               │
│                                                                          │
│  4. Calls: submit_bayesian_update_for_approval()                       │
│     ↓                                                                   │
│     Inserts into bayesian_model_updates table:                         │
│     - status = 'PENDING' (NOT APPROVED YET)                            │
│     - regime = 'BULL'                                                  │
│     - proposed_version = 'v2'                                          │
│     - proposed_weights = {...new weights...}                           │
│                                                                          │
│  5. ✅ STOPS HERE - Does NOT change live weights                       │
│     ✅ STOPS HERE - Does NOT modify score_weight_log                   │
│     ✅ STOPS HERE - Logs: "Awaiting admin approval..."                 │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ↓
┌──────────────────────────────────────────────────────────────────────────┐
│ ADMIN DASHBOARD (Human Decision Point)                                  │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  Admin Reviews pending update:                                         │
│  ┌─────────────────────────────────────────────────────────────┐       │
│  │ Update ID: #42                                              │       │
│  │ Regime: BULL                                                │       │
│  │ Win Rate: 72.3% (47 trades analyzed)                        │       │
│  │                                                             │       │
│  │ Current (v1):        Proposed (v2):                         │       │
│  │ VOLUME_ZSCORE: 3.0   →  VOLUME_ZSCORE: 4.5 ✓              │       │
│  │ RS_RANK: 7.0         →  RS_RANK: 6.5 ✓                    │       │
│  │ TREND_STRENGTH: 1.0  →  TREND_STRENGTH: 1.0 (unchanged)   │       │
│  │                                                             │       │
│  │ Reason: "Spike detection improved; less reliant on RS"     │       │
│  │                                                             │       │
│  │ [APPROVE] [REJECT] [NEED_INFO]                              │       │
│  └─────────────────────────────────────────────────────────────┘       │
│                                                                          │
│  SCENARIO A: Admin clicks [APPROVE]                                    │
│  └─→ approve_bayesian_update(42, "john_admin", "Looks good")           │
│                                                                          │
│  SCENARIO B: Admin clicks [REJECT]                                     │
│  └─→ reject_bayesian_update(42, "john_admin", "Need more data")        │
└──────────────────────────────────────────────────────────────────────────┘
                     │                          │
         APPROVE→    │                          │    ←REJECT
                     ↓                          ↓
    ┌────────────────────────────┐   ┌──────────────────────────┐
    │ Weights NOW GO LIVE        │   │ Current Weights Stay     │
    │                            │   │                          │
    │ approve_bayesian_update()  │   │ reject_bayesian_update() │
    │                            │   │                          │
    │ 1. INSERT into            │   │ 1. UPDATE status to      │
    │    score_weight_log:      │   │    'REJECTED'            │
    │    - model_version='v2'   │   │ 2. Leave score_weight_log│
    │    - regime='BULL'        │   │    UNCHANGED             │
    │    - weights={...new...}  │   │ 3. Result: v1 still live │
    │                            │   │ 4. Bayesian can propose  │
    │ 2. UPDATE bayesian_model  │   │    new update            │
    │    _updates status→       │   │                          │
    │    'APPROVED'             │   │                          │
    │                            │   │                          │
    │ 3. Result: v2 NOW LIVE    │   │                          │
    │ 4. All future scanners    │   │                          │
    │    use v2 weights         │   │                          │
    └────────────────────────────┘   └──────────────────────────┘
                     │                          │
                     ↓                          ↓
    ┌────────────────────────────┐   ┌──────────────────────────┐
    │ INTRADAY SCANNER RUNS      │   │ INTRADAY SCANNER RUNS    │
    │ (Next market cycle)        │   │ (Next market cycle)      │
    │                            │   │                          │
    │ Calls get_current_bayesian │   │ Calls get_current_bayesian
    │ _model()                   │   │ _model()                 │
    │                            │   │                          │
    │ get_current returns:       │   │ get_current returns:     │
    │ {                          │   │ {                        │
    │   'BULL': {                │   │   'BULL': {              │
    │     'version': 'v2',       │   │     'version': 'v1',     │
    │     'weights': {           │   │     'weights': {         │
    │       'VOLUME_ZSCORE': 4.5 │   │       'VOLUME_ZSCORE': 3.0│
    │       'RS_RANK': 6.5       │   │       'RS_RANK': 7.0     │
    │       ...                  │   │       ...                │
    │     }                      │   │     }                    │
    │   }                        │   │   }                      │
    │ }                          │   │ }                        │
    │                            │   │                          │
    │ ✅ USES v2 WEIGHTS        │   │ ✅ USES v1 WEIGHTS      │
    │ ✅ Alert saved with       │   │ ✅ Alert saved with      │
    │    model_version='v2'     │   │    model_version='v1'    │
    │ ✅ Captures exact weights │   │ ✅ Captures exact weights│
    │    used in alert          │   │    used in alert         │
    └────────────────────────────┘   └──────────────────────────┘
```

---

## 2. Isolation Guarantee: How It Works

### 2.1 Two Separate Tables (Critical Design)

| Table | Purpose | Contains | When Updated |
|-------|---------|----------|--------------|
| **bayesian_model_updates** | Pending proposals | `status='PENDING'` updates | When Bayesian proposes changes |
| **score_weight_log** | LIVE weights ONLY | Only APPROVED weights | When admin approves ONLY |

### 2.2 Data Flow Isolation

```
Bayesian Updater
    ↓
    Reads: score_weight_log (current APPROVED)
    Analyzes: TRAIN trades
    Proposes: new weights
    ↓
    Writes to: bayesian_model_updates (status='PENDING')
    
    ❌ NEVER writes to score_weight_log directly
    ❌ NEVER calls calculate_score() with PENDING weights
    ❌ NEVER modifies any live data

Admin Approval (Only Path to Go Live)
    ↓
    Human reviews proposed vs current
    ↓
    [APPROVE] → INSERT into score_weight_log (NOW LIVE)
    [REJECT]  → Mark status='REJECTED' (live weights unchanged)
    
    ✅ ONLY admin approval can change live weights
    ✅ ONLY admin approval triggers score_weight_log insert
    ✅ No other code path can modify live weights

Scanners (Intraday, EOD, Reversal, Live)
    ↓
    Call: get_current_bayesian_model()
    ↓
    This function ONLY reads from score_weight_log
    ↓
    Returns APPROVED weights ONLY
    
    ✅ Guaranteed to never get PENDING weights
    ✅ Guaranteed to get only what admin approved
    ✅ Captures weights in each alert for audit trail
```

---

## 3. Code Implementation Details

### 3.1 get_current_bayesian_model() — The Safety Gate

```python
def get_current_bayesian_model():
    """
    Get the current ACTIVE (APPROVED) Bayesian model version and weights for all regimes.
    
    CRITICAL: This ONLY returns weights from score_weight_log that have been
    explicitly approved by admin. PENDING updates in bayesian_model_updates
    are NOT included here.
    """
    # ✅ Reads ONLY from score_weight_log
    cur.execute("""
        SELECT model_version, weights
        FROM score_weight_log
        WHERE regime = %s
        ORDER BY id DESC
        LIMIT 1
    """, (regime,))
    
    # ✅ Returns APPROVED weights only
    # ❌ Never touches bayesian_model_updates table
```

**Guarantee:** This function CANNOT return PENDING weights. It only reads from `score_weight_log` which contains only approved weights.

### 3.2 submit_bayesian_update_for_approval() — Proposal Safe

```python
def submit_bayesian_update_for_approval(...):
    """
    PENDING status = awaiting admin review.
    Weights are saved but NOT used for calculations.
    """
    # Check for existing PENDING (prevent duplicates)
    cur.execute("""
        SELECT id FROM bayesian_model_updates
        WHERE regime = %s AND status = 'PENDING'
    """)
    if pending_exists:
        return None  # Blocked!
    
    # Insert with status='PENDING'
    cur.execute("""
        INSERT INTO bayesian_model_updates (
            regime, proposed_version, ...,
            status='PENDING'
        )
    """)
    
    # ❌ Does NOT modify score_weight_log
    # ❌ Does NOT make weights live
    # ✅ Just saves proposal for review
```

**Guarantee:** PENDING weights are saved to DB but never used by calculate_score() because get_current_bayesian_model() doesn't read them.

### 3.3 approve_bayesian_update() — Approval Only Path to Live

```python
def approve_bayesian_update(update_id, admin_name, comment):
    """
    ONLY THIS FUNCTION can make new weights LIVE.
    
    Two-phase commit:
    1. INSERT into score_weight_log (makes live)
    2. UPDATE bayesian_model_updates to APPROVED
    """
    
    # Get the PENDING proposal
    cur.execute("""
        SELECT regime, proposed_version, proposed_weights
        FROM bayesian_model_updates
        WHERE id = %s AND status = 'PENDING'
    """)
    
    # STEP 1: INSERT into score_weight_log (Makes weights LIVE)
    cur.execute("""
        INSERT INTO score_weight_log (
            model_version, regime, weights
        ) VALUES (%s, %s, %s)  -- Now LIVE!
    """, (proposed_version, regime, proposed_weights))
    
    # STEP 2: Update bayesian_model_updates to APPROVED
    cur.execute("""
        UPDATE bayesian_model_updates
        SET status = 'APPROVED', approved_by = %s, approved_at = NOW()
        WHERE id = %s
    """)
    
    conn.commit()  # Atomic!
```

**Guarantee:** 
- Only approve_bayesian_update() can insert into score_weight_log
- Only admin action triggers this function
- PENDING weights remain PENDING until admin explicitly approves

---

## 4. Alert Audit Trail

Each alert captures which model version generated it:

```sql
-- An alert saved during BULL v2 approval:
INSERT INTO alerts (
    symbol, score, model_version, bayesian_regime, bayesian_weights,
    breakout_type, alert_time, ...
) VALUES (
    'RELIANCE', 82, 'v2', 'BULL', 
    '{"VOLUME_ZSCORE": 4.5, "RS_RANK": 6.5, ...}',
    'BREAKOUT', '2026-06-17 14:30:00', ...
)
```

**Audit Trail:**
- ✅ Every alert shows which model_version created it
- ✅ Every alert captures exact bayesian_weights used
- ✅ Dashboard can show P&L by model version
- ✅ Can revert to v1 if v2 underperforms

---

## 5. Testing the Isolation

### Test Case 1: Bayesian Cannot Affect Live Weights

```python
# Scenario: Bayesian proposes v2 weights
update_id = submit_bayesian_update_for_approval(
    regime='BULL',
    proposed_version='v2',
    proposed_weights={'VOLUME_ZSCORE': 4.5, 'RS_RANK': 6.5}
)

# Now run a scanner
model = get_current_bayesian_model()
assert model['BULL']['version'] == 'v1'  # ✅ Still v1!
assert model['BULL']['weights']['VOLUME_ZSCORE'] == 3.0  # ✅ Unchanged!

# PENDING weights are hidden from scanners
```

### Test Case 2: Admin Approval Makes Weights Live

```python
# Admin approves
approve_bayesian_update(update_id, admin_name='alice')

# Now scanner gets v2
model = get_current_bayesian_model()
assert model['BULL']['version'] == 'v2'  # ✅ Now v2!
assert model['BULL']['weights']['VOLUME_ZSCORE'] == 4.5  # ✅ Updated!
```

### Test Case 3: Pending Still Blocked if Not Approved

```python
# Bayesian tries to propose again while v2 is PENDING
update_id_2 = submit_bayesian_update_for_approval(
    regime='BULL',
    proposed_version='v3',  # Another update
    proposed_weights={...}
)

assert update_id_2 is None  # ✅ Blocked!
# Log: "Already have PENDING update for BULL regime (ID: {id})"
```

---

## 6. Error Handling & Failure Modes

### Scenario: What if DB connection fails during approval?

```python
# In approve_bayesian_update():
try:
    cur.execute("INSERT INTO score_weight_log ...")  # Fails!
    cur.execute("UPDATE bayesian_model_updates ...")
    conn.commit()
except Exception as e:
    # ✅ ATOMIC: Both fail or both succeed
    # ✅ If INSERT fails, UPDATE never happens
    # ✅ PENDING status remains PENDING
    # ✅ Manual retry possible by admin
    logger.error(f"Failed to approve update: {e}")
    return False
```

**Result:** Weights stay PENDING, no partial state corruption.

### Scenario: Admin approves, then rejects same update?

```python
# Once approved:
cur.execute("WHERE id = %s AND status = 'PENDING'")
# Returns 0 rows (status is now 'APPROVED')

# ✅ Reject fails safely (already approved)
# ✅ Cannot change an approved state
# ✅ To override, must manually update DB
```

---

## 7. Deployment Checklist

- [x] get_current_bayesian_model() reads only from score_weight_log
- [x] submit_bayesian_update_for_approval() inserts with status='PENDING'
- [x] approve_bayesian_update() inserts into score_weight_log
- [x] Only approve_bayesian_update() can insert into score_weight_log
- [x] reject_bayesian_update() cannot modify score_weight_log
- [x] Alerts capture model_version and bayesian_weights
- [x] Bayesian updater calls submit_bayesian_update_for_approval()
- [x] No other code modifies bayesian_model_updates.status
- [x] Admin must explicitly approve before weights go live
- [x] PENDING updates block new proposals for same regime
- [x] DB syntax check passed ✅

---

## 8. Summary

| Aspect | Guarantee |
|--------|-----------|
| **PENDING Weights** | Saved to DB, never used by scanners |
| **Live Weights** | Only from score_weight_log (approved only) |
| **Who Approves** | Admin only (via approve_bayesian_update) |
| **When Weights Change** | After admin explicitly approves |
| **Audit Trail** | Every alert captures model_version + weights |
| **Failure Mode** | PENDING stays PENDING on DB error (atomic) |
| **Blocking** | Cannot submit new update if one is PENDING |

**Result:** ✅ **UNAPPROVED WEIGHTS NEVER AFFECT CALCULATIONS**
