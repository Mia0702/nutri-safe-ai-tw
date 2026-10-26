-- nutri_safe_ai（呷安心）0008：LINE 持久化狀態 schema（additive、idempotent）
--
-- 狀態：尚未套用到任何環境（Phase C2a，僅 schema）。
-- 用途：為日後重啟不遺失的 LINE 狀態預留 3 張表（runtime 尚未接線，現有 in-memory 行為不變）：
--   * line_request_records ：request 範圍紀錄，供 postback / 舊卡片使用（TTL 24 小時）。
--   * line_task_contexts   ：每位使用者目前的 TaskContext（TTL 30 分鐘；version 供日後 compare-and-set）。
--   * line_event_claims    ：一次性 atomic claim（webhook 去重、ASR 選擇/忽略單次使用；TTL 24 小時）。
-- 隱私：不存原始 LINE user id；只存 owner_key（runtime 以 HMAC 產生，本 migration 不含 secret 或產生邏輯）。
--       不存圖片 / 音訊 bytes、secret、多餘的 OpenAI payload。
-- 保留：本 migration 不建立 pg_cron / purge job；只提供 expires_at 與其 index，供日後清除。
-- 不修改 0001–0007 的任何表 / 欄位 / policy；本 migration 不寫入資料列（只有 marker）。
-- 前置：0001 → 0007。整份在同一個 transaction，可重複執行。

begin;

do $$
begin
    if not exists (select 1 from app_schema_meta where key = 'project' and value = 'nutri_safe_ai') then
        raise exception '0008: 找不到 nutri_safe_ai 的 app_schema_meta 標記，請確認連線的是本專案 DB';
    end if;
    if not exists (select 1 from app_schema_meta where key = 'migration_0007_taiwan_drug_ingredient_catalog') then
        raise exception '0008: 找不到 migration_0007 標記，請先依序套用 0001 → 0007';
    end if;
end $$;

-- request 範圍紀錄：first-write-wins 由日後 INSERT ... ON CONFLICT DO NOTHING 達成；本 schema 不提供自動覆寫。
create table if not exists line_request_records (
    request_id uuid primary key,
    owner_key text not null,                                  -- HMAC 後的擁有者鍵；不是 LINE user id
    record jsonb not null,
    query_label text,
    received_at timestamptz not null default now(),
    created_at timestamptz not null default now(),
    expires_at timestamptz not null default (now() + interval '24 hours')   -- request TTL = 24h
);
create index if not exists idx_line_request_records_expires_at on line_request_records (expires_at);

-- 目前 TaskContext：每位使用者一列；context 內含既有 TaskContext 語意（含 pending clarification），不在此重新設計。
create table if not exists line_task_contexts (
    owner_key text primary key,
    session_id text not null,
    context jsonb not null,
    version integer not null default 1 check (version >= 1),  -- 日後 compare-and-set：UPDATE ... WHERE version = :expected
    updated_at timestamptz not null default now(),
    expires_at timestamptz not null default (now() + interval '30 minutes') -- context TTL = 30m
);
create index if not exists idx_line_task_contexts_expires_at on line_task_contexts (expires_at);

-- 一次性 claim：INSERT ... ON CONFLICT (kind, key) DO NOTHING，成功插入者取得 claim。
create table if not exists line_event_claims (
    kind text not null,
    key text not null,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null default (now() + interval '24 hours'),  -- event claim TTL = 24h
    primary key (kind, key)
);
create index if not exists idx_line_event_claims_expires_at on line_event_claims (expires_at);

do $$
declare t text;
begin
    foreach t in array array['line_request_records', 'line_task_contexts', 'line_event_claims'] loop
        execute format('alter table %I enable row level security', t);
        execute format('revoke all on table %I from anon, authenticated', t);
        execute format('grant select, insert, update, delete on table %I to service_role', t);
    end loop;
end $$;

insert into app_schema_meta (key, value) values ('migration_0008_line_persistent_state', 'applied')
on conflict (key) do update set value = excluded.value, updated_at = now();

commit;
