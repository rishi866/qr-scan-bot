export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

export interface Admin {
  id: number;
  username: string;
  totp_enabled: boolean;
  last_login_at: string | null;
  created_at: string;
}

export type Role = "seller" | "scanner";

export interface UserRow {
  user_id: number;
  telegram_id: number;
  username: string | null;
  name: string;
  role: Role;
  status: string;
  country: string | null;
  country_name: string | null;
  timezone: string | null;
  utc_offset: string | null;
  alias: string | null;
  reputation: number | null;
  bot_blocked: boolean;
  admin_note: string | null;
  approved_at: string | null;
  created_at: string;
  balance: string | null;
  pending: string | null;
  slot_count: number | null;
}

export interface SlotRow {
  id: number;
  user_id: number;
  alias: string | null;
  label: string;
  slot_start: number;
  slot_end: number;
  timezone: string;
  tz_label: string;
  utc_window: string;
  is_active: boolean;
  live_now: boolean;
  reputation: number;
  status?: string;
}

export interface ScannerRow extends UserRow {
  slots: SlotRow[];
  slot_labels: string[];
  active_now: boolean;
  completed_tasks: number;
  total_earned: string;
  last_assigned_at: string | null;
}

export interface UserDetail extends UserRow {
  wallet: {
    balance: string; pending: string; total_earned: string; total_spent: string; total_deposited: string; total_withdrawn: string;
    bep20_address: string | null; binance_address: string | null;
  } | null;
  stats: { completed_tasks: number; volume: string; commission: string };
  recent_sessions: { session_id: number; status: string; amount: string; commission: string; sent_at: string }[];
  slots: SlotRow[];
  deposits: number;
  withdrawals: number;
}

export interface Tx {
  id: number;
  session_id: number;
  seller_id: number;
  seller_name: string | null;
  seller_country: string | null;
  seller_timezone: string | null;
  scanner_id: number;
  scanner_name: string | null;
  scanner_country: string | null;
  scanner_timezone: string | null;
  slot: string | null;
  url: string;
  amount: string;
  commission: string;
  status: string;
  confirmed_at: string | null;
  created_at: string;
}

export interface SessionRow {
  session_id: number;
  status: string;
  seller: { user_id: number; name: string; telegram_id: number };
  scanner: { user_id: number; alias: string | null; name: string; telegram_id: number };
  url: string;
  amount: string;
  commission: string;
  slot: string | null;
  sent_at: string | null;
  accepted_at: string | null;
  done_at: string | null;
  confirmed_at: string | null;
  expires_at: string | null;
  deadline_at: string | null;
  closed_reason: string | null;
}

export interface PartyBrief {
  user_id: number;
  name: string;
  username: string | null;
  telegram_id: number;
  country: string | null;
  alias?: string | null;
}

export interface DisputeRow {
  id: number;
  session_id: number;
  status: string;
  resolution: string | null;
  reason: string | null;
  admin_notes: string | null;
  resolved_by: string | null;
  resolved_at: string | null;
  created_at: string;
  proof_deadline_at: string | null;
  has_proof: boolean;
  ai_status: string | null;
  ai_verdict: string | null;
  ai_confidence: number | null;
  ai_reason: string | null;
  amount: string;
  commission: string;
  session_status: string;
  url: string;
  seller: PartyBrief;
  scanner: PartyBrief;
  timeline: { sent_at: string | null; accepted_at: string | null; done_at: string | null; prompted_at: string | null };
}

export interface WalletRow {
  user_id: number;
  name: string;
  alias: string | null;
  role: Role;
  telegram_id: number;
  country: string | null;
  username: string | null;
  balance: string;
  pending: string;
  total_earned: string;
  total_spent: string;
  total_deposited: string;
  total_withdrawn: string;
  bep20_address: string | null;
  binance_address: string | null;
}

export interface LedgerRow {
  id: number;
  type: string;
  balance_delta: string;
  pending_delta: string;
  balance_after: string;
  pending_after: string;
  ref_type: string | null;
  ref_id: number | null;
  note: string | null;
  created_at: string;
}

export interface DepositRow {
  id: number;
  user_id: number;
  name: string;
  alias: string | null;
  role: Role;
  telegram_id: number;
  country: string | null;
  amount: string;
  method: string;
  tx_hash: string;
  address: string | null;
  status: string;
  note: string | null;
  block_number: number | null;
  created_at: string;
  credited_at: string | null;
}

export interface WithdrawalRow {
  id: number;
  user_id: number;
  name: string;
  alias: string | null;
  telegram_id: number;
  country: string | null;
  amount: string;
  fee: string;
  net: string;
  method: string;
  address: string;
  tx_hash: string | null;
  status: string;
  admin_note: string | null;
  processed_by: string | null;
  processed_at: string | null;
  created_at: string;
  auto_payout: boolean;
}

export interface Dashboard {
  users: { total: number; sellers: number; scanners: number; approved: number; pending: number; suspended: number };
  pending_approvals: number;
  scanners_needing_name: number;
  today: { transactions: number; volume: string; commission: string };
  commission_total: string;
  disputes: { open: number; awaiting_proof: number; pending_review: number };
  withdrawals: { to_handle: number; amount: string };
  generated_at?: string;
  deposits_to_review: number;
  live_sessions: number;
  active_scanners_now: { user_id: number; alias: string; slot: string; country: string | null; reputation: number }[];
  series: { date: string; transactions: number; volume: string; commission: string }[];
  ledger: { ok: boolean; problems: string[]; user_total: string; platform_balance: string };
  system: {
    worker_online: boolean; worker_last_seen: string | null; ai_configured: boolean; deposits_configured: boolean;
    auto_payout: boolean; binance_configured: boolean; telegram_admins: number;
  };
}

export interface Badges {
  pending_users: number;
  open_disputes: number;
  withdrawals_to_handle: number;
  deposits_to_review: number;
  scanners_needing_name: number;
}

export interface SettingDef {
  key: string;
  type: "decimal" | "int" | "bool" | "str" | "list";
  default: unknown;
  label: string;
  help: string;
  group: string;
  min: number | null;
  max: number | null;
  choices: number[] | null;
}

export interface BroadcastRow {
  id: number;
  message: string;
  audience: string;
  total: number;
  created_by: string | null;
  created_at: string;
  sent: number;
  failed: number;
  queued: number;
}

export interface AuditRow {
  id: number;
  admin: string;
  action: string;
  target_type: string | null;
  target_id: string | null;
  details: Record<string, unknown> | null;
  ip: string | null;
  created_at: string;
}

export interface ChainStatus {
  token: { symbol: string; contract: string; decimals: number };
  confirmations: number;
  auto_payout: { enabled: boolean; max_amount: string };
  binance_configured: boolean;
  ai_configured: boolean;
  deposits: { configured: boolean; mode?: string; xpub?: string; error?: string };
  hot_wallet: { configured: boolean; address?: string; bnb?: string; token?: string };
  rpc: { ok: boolean; chain_id?: number; latest_block?: number; error?: string };
  last_scanned_block: number | null;
  worker_heartbeat: string | null;
}

export interface SettingsResponse {
  definitions: SettingDef[];
  values: Record<string, unknown>;
  changed?: string[];
}

export interface CoverageResponse {
  date: string;
  current_hour: number;
  hours: { hour: number; scanners: { user_id: number; alias: string | null; slot: string; timezone: string }[] }[];
}

export interface LedgerReport {
  ok: boolean;
  problems: string[];
  ledger_total: string;
  expected_total: string;
  platform_balance: string;
  user_total: string;
}

export interface WalletsSummary {
  by_role: Record<string, { balance: string; pending: string }>;
  platform_commission: string;
  deposits_credited: string;
  withdrawals_paid: string;
  ledger: LedgerReport;
}

export interface WalletDetail {
  wallet: WalletRow & { updated_at: string | null };
  ledger: Page<LedgerRow>;
}
