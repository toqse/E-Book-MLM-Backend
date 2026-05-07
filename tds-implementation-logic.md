# TDS Implementation Logic — Commission Platform
## For: Private Limited Company | Section 194H | FY 2025–26

---

## Overview

This document defines the backend logic for handling TDS (Tax Deducted at Source)
on referral commissions as per Section 194H of the Income Tax Act.

**Key Rules:**
- TDS Rate: 2% (if PAN provided) | 20% (if PAN not provided)
- Annual Threshold: ₹20,000 per member per financial year
- Financial Year: April 1 – March 31
- TDS is tracked cumulatively per member, NOT per transaction

---

## Database Schema

### 1. `users` table (add these fields if not present)
```sql
ALTER TABLE users ADD COLUMN pan_number VARCHAR(10);
ALTER TABLE users ADD COLUMN kyc_verified BOOLEAN DEFAULT FALSE;
ALTER TABLE users ADD COLUMN kyc_verified_at TIMESTAMP;
```

### 2. `tds_ledger` table (create this new table)
```sql
CREATE TABLE tds_ledger (
  id               BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id          BIGINT NOT NULL,
  financial_year   VARCHAR(7) NOT NULL,   -- e.g. "2025-26"
  total_earned     DECIMAL(10,2) DEFAULT 0.00,   -- cumulative earnings this FY
  total_tds        DECIMAL(10,2) DEFAULT 0.00,   -- cumulative TDS deducted this FY
  tds_triggered    BOOLEAN DEFAULT FALSE,         -- has 20k threshold been crossed?
  tds_triggered_at TIMESTAMP NULL,
  created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

  UNIQUE KEY unique_user_fy (user_id, financial_year),
  FOREIGN KEY (user_id) REFERENCES users(id)
);
```

### 3. `commission_transactions` table (add TDS fields)
```sql
ALTER TABLE commission_transactions ADD COLUMN gross_amount    DECIMAL(10,2);
ALTER TABLE commission_transactions ADD COLUMN tds_amount      DECIMAL(10,2) DEFAULT 0.00;
ALTER TABLE commission_transactions ADD COLUMN net_amount      DECIMAL(10,2);
ALTER TABLE commission_transactions ADD COLUMN tds_rate        DECIMAL(5,2) DEFAULT 0.00;
ALTER TABLE commission_transactions ADD COLUMN tds_applicable  BOOLEAN DEFAULT FALSE;
ALTER TABLE commission_transactions ADD COLUMN financial_year  VARCHAR(7);
```

---

## Core Helper Functions

### Get Current Financial Year
```javascript
function getCurrentFinancialYear() {
  const now = new Date();
  const month = now.getMonth() + 1; // 1–12
  const year = now.getFullYear();

  // FY starts April 1
  if (month >= 4) {
    return `${year}-${String(year + 1).slice(-2)}`; // e.g. "2025-26"
  } else {
    return `${year - 1}-${String(year).slice(-2)}`; // e.g. "2024-25"
  }
}
```

### Get TDS Rate for User
```javascript
function getTDSRate(user) {
  if (!user.kyc_verified) {
    throw new Error("Commission cannot be paid without KYC verification.");
  }
  if (!user.pan_number || user.pan_number.trim() === "") {
    return 0.20; // 20% if no PAN
  }
  return 0.02; // 2% if PAN provided
}
```

---

## Main TDS Calculation Function

```javascript
/**
 * Calculate TDS for a commission credit
 *
 * @param {object} user          - User object with pan_number, kyc_verified
 * @param {number} grossAmount   - Commission amount being credited (e.g. 30 or 10)
 * @param {object} db            - Database connection
 * @returns {object}             - { grossAmount, tdsAmount, netAmount, tdsRate, tdsApplicable }
 */
async function calculateCommissionTDS(user, grossAmount, db) {

  // Step 1: Block unverified KYC
  if (!user.kyc_verified) {
    throw new Error("Cannot credit commission: KYC not verified.");
  }

  const financialYear = getCurrentFinancialYear();
  const tdsRate = getTDSRate(user);
  const TDS_THRESHOLD = 20000;

  // Step 2: Get or create TDS ledger entry for this user + FY
  let ledger = await db.query(
    `SELECT * FROM tds_ledger WHERE user_id = ? AND financial_year = ?`,
    [user.id, financialYear]
  );

  if (!ledger) {
    await db.query(
      `INSERT INTO tds_ledger (user_id, financial_year, total_earned, total_tds, tds_triggered)
       VALUES (?, ?, 0, 0, FALSE)`,
      [user.id, financialYear]
    );
    ledger = { total_earned: 0, total_tds: 0, tds_triggered: false };
  }

  const previousTotal = parseFloat(ledger.total_earned);
  const newTotal = previousTotal + grossAmount;

  let tdsAmount = 0;
  let tdsApplicable = false;

  // Step 3: Check if threshold already crossed before this transaction
  if (ledger.tds_triggered) {
    // Threshold was already crossed — deduct TDS on full gross amount
    tdsAmount = parseFloat((grossAmount * tdsRate).toFixed(2));
    tdsApplicable = true;

  } else if (newTotal > TDS_THRESHOLD) {
    // Threshold crossed WITH this transaction
    // Deduct TDS on the ENTIRE cumulative amount (including previous unpaid TDS)
    const totalTDSRequired = parseFloat((newTotal * tdsRate).toFixed(2));
    const tdsAlreadyDeducted = parseFloat(ledger.total_tds);
    tdsAmount = parseFloat((totalTDSRequired - tdsAlreadyDeducted).toFixed(2));
    tdsApplicable = true;

    // Mark threshold as triggered
    await db.query(
      `UPDATE tds_ledger
       SET tds_triggered = TRUE, tds_triggered_at = NOW()
       WHERE user_id = ? AND financial_year = ?`,
      [user.id, financialYear]
    );

  } else {
    // Below threshold — no TDS
    tdsAmount = 0;
    tdsApplicable = false;
  }

  const netAmount = parseFloat((grossAmount - tdsAmount).toFixed(2));

  // Step 4: Update ledger
  await db.query(
    `UPDATE tds_ledger
     SET total_earned = total_earned + ?,
         total_tds = total_tds + ?,
         updated_at = NOW()
     WHERE user_id = ? AND financial_year = ?`,
    [grossAmount, tdsAmount, user.id, financialYear]
  );

  return {
    grossAmount,
    tdsAmount,
    netAmount,
    tdsRate: tdsRate * 100,   // as percentage, e.g. 2 or 20
    tdsApplicable,
    financialYear
  };
}
```

---

## Withdrawal Flow

```javascript
/**
 * Process a withdrawal request
 * Minimum withdrawal: ₹200 (platform rule)
 * TDS is already handled at commission credit time — do NOT deduct again here
 */
async function processWithdrawal(user, withdrawalAmount, db) {

  const MIN_WITHDRAWAL = 200;

  // Step 1: Check minimum withdrawal
  if (withdrawalAmount < MIN_WITHDRAWAL) {
    throw new Error(`Minimum withdrawal amount is ₹${MIN_WITHDRAWAL}`);
  }

  // Step 2: Check wallet balance
  const wallet = await db.query(
    `SELECT balance FROM wallets WHERE user_id = ?`,
    [user.id]
  );

  if (wallet.balance < withdrawalAmount) {
    throw new Error("Insufficient wallet balance.");
  }

  // Step 3: TDS is NOT deducted again at withdrawal
  // It was already deducted when commission was credited to wallet
  // Simply process the withdrawal of net amount

  await db.query(
    `UPDATE wallets SET balance = balance - ? WHERE user_id = ?`,
    [withdrawalAmount, user.id]
  );

  await db.query(
    `INSERT INTO withdrawals (user_id, amount, status, created_at)
     VALUES (?, ?, 'pending', NOW())`,
    [user.id, withdrawalAmount]
  );

  return {
    success: true,
    withdrawalAmount,
    message: "Withdrawal initiated successfully."
  };
}
```

---

## Commission Credit Flow (Full)

```javascript
/**
 * Credit commission to a user's wallet
 * Called when:
 *   - Direct referral happens (₹30)
 *   - Tree passive income triggers (₹10)
 *   - Milestone bonus unlocks (₹300, ₹600, ₹1000, ₹1350, ₹1600)
 */
async function creditCommission(userId, grossAmount, commissionType, db) {

  const user = await db.query(
    `SELECT * FROM users WHERE id = ?`,
    [userId]
  );

  // Block if KYC not done
  if (!user.kyc_verified) {
    console.log(`Skipping commission for user ${userId}: KYC not verified.`);
    return { skipped: true, reason: "KYC not verified" };
  }

  // Calculate TDS
  const tdsResult = await calculateCommissionTDS(user, grossAmount, db);

  // Credit NET amount to wallet
  await db.query(
    `UPDATE wallets SET balance = balance + ? WHERE user_id = ?`,
    [tdsResult.netAmount, userId]
  );

  // Record the transaction
  await db.query(
    `INSERT INTO commission_transactions
     (user_id, commission_type, gross_amount, tds_amount, net_amount,
      tds_rate, tds_applicable, financial_year, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, NOW())`,
    [
      userId,
      commissionType,           // 'direct_referral' | 'tree_passive' | 'milestone'
      tdsResult.grossAmount,
      tdsResult.tdsAmount,
      tdsResult.netAmount,
      tdsResult.tdsRate,
      tdsResult.tdsApplicable,
      tdsResult.financialYear
    ]
  );

  // If TDS was deducted, log it separately for government deposit tracking
  if (tdsResult.tdsApplicable && tdsResult.tdsAmount > 0) {
    await db.query(
      `INSERT INTO tds_deposits_pending
       (user_id, pan_number, tds_amount, financial_year, commission_transaction_id, deposit_by_date)
       VALUES (?, ?, ?, ?, LAST_INSERT_ID(), DATE_ADD(LAST_DAY(NOW()), INTERVAL 7 DAY))`,
      [userId, user.pan_number, tdsResult.tdsAmount, tdsResult.financialYear]
    );
  }

  return tdsResult;
}
```

---

## Financial Year Reset

```javascript
/**
 * This does NOT need a cron job.
 * The financial year is derived dynamically from getCurrentFinancialYear().
 * A new row is automatically created in tds_ledger for each new FY
 * the first time a commission is credited after April 1.
 *
 * No manual reset needed. ✅
 */
```

---

## Usage Examples

```javascript
// When someone's referral purchases the course → Credit ₹30 to referrer
await creditCommission(referrerId, 30, 'direct_referral', db);

// When tree passive income triggers → Credit ₹10
await creditCommission(userId, 10, 'tree_passive', db);

// When milestone of 10 referrals is hit → Credit ₹300
await creditCommission(userId, 300, 'milestone', db);

// When user requests withdrawal of ₹500
await processWithdrawal(user, 500, db);
```

---

## TDS Deposit to Government (Your Finance Team)

| Action | Deadline |
|---|---|
| Deposit TDS deducted in a month | By 7th of next month |
| File Form 26Q — Q1 (Apr–Jun) | By 31st July |
| File Form 26Q — Q2 (Jul–Sep) | By 31st October |
| File Form 26Q — Q3 (Oct–Dec) | By 31st January |
| File Form 26Q — Q4 (Jan–Mar) | By 31st May |

Query to get monthly TDS pending deposit:
```sql
SELECT
  u.pan_number,
  u.full_name,
  SUM(t.tds_amount) AS total_tds,
  t.financial_year
FROM tds_deposits_pending t
JOIN users u ON u.id = t.user_id
WHERE
  MONTH(t.created_at) = MONTH(NOW() - INTERVAL 1 MONTH)
  AND t.deposited = FALSE
GROUP BY u.id, t.financial_year;
```

---

## Summary of Rules (Quick Reference for Cursor)

| Rule | Value |
|---|---|
| TDS Section | 194H |
| Threshold per user per FY | ₹20,000 |
| TDS Rate (PAN provided) | 2% |
| TDS Rate (no PAN) | 20% |
| Commission allowed without KYC? | NO |
| TDS deducted per transaction? | NO — only after ₹20,000 cumulative |
| TDS deducted again at withdrawal? | NO — only at credit time |
| Financial Year | April 1 – March 31 |
| FY resets automatically? | YES — new ledger row per FY |
