-- nutri_safe_ai（呷安心）0007：Taiwan Official Drug Ingredient Catalog（additive、idempotent）
--
-- 狀態：2026-10-06 已由使用者於 production SQL Editor 手動套用並驗證（RLS、anon/authenticated 無權限）。
-- 用途：台灣 TFDA 官方資料中，「未註銷藥品許可證涵蓋藥證數」最高的主成分名錄（約前 1000 個，並保留第 1000 名門檻的所有並列項目）。
--   * 這是「官方藥品成分 identity」名錄：成員資格 ≠ 交互作用證據，不建立任何 interaction，不影響 CONCLUSION_ELIGIBLE。
--   * 與 0006 的「已審核 Interaction DrugSubstance」分開；只有「正規化後名稱完全相同」的對應才寫入 mapped_drug_substance_id
--     （鹽類 / 形式變體不自動對應）。
--   * 不修改 0001–0006 的任何表 / 欄位；本 migration 不寫入資料列（只有 marker）；資料由 scripts/import_taiwan_drug_catalog.py 在核准後匯入。
-- 前置：0001 → 0006。整份在同一個 transaction，可重複執行。

begin;

do $$
begin
    if not exists (select 1 from app_schema_meta where key = 'project' and value = 'nutri_safe_ai') then
        raise exception '0007: 找不到 nutri_safe_ai 的 app_schema_meta 標記，請確認連線的是本專案 DB';
    end if;
    if not exists (select 1 from app_schema_meta where key = 'migration_0006_interaction_corpus') then
        raise exception '0007: 找不到 migration_0006 標記，請先依序套用 0001 → 0006';
    end if;
end $$;

create table if not exists taiwan_drug_ingredients (
    id uuid primary key default gen_random_uuid(),
    ingredient_code text,                         -- TFDA 成分代碼（canonical identity；沒有代碼時為 NULL，以 normalized_exact_name 識別）
    official_name text not null,                  -- 官方名稱（原樣，不翻譯、不去鹽類）
    normalized_exact_name text not null,          -- NFKC / 空白折疊 / casefold；比對只用完全相同
    active_permit_count integer not null check (active_permit_count > 0),   -- COUNT(DISTINCT 許可證字號)，只算未註銷藥證
    rank integer not null check (rank > 0),
    source_id text not null,
    source_dataset text not null,                 -- 例如 9123（主成分略述）＋ 9121（成分代碼）＋ 9122（交叉檢查）
    source_url text,
    dataset_modified_at timestamptz,              -- 官方資料集 metadata 的更新時間
    snapshot_sha256 text,                         -- 下載檔雜湊（可追溯）
    interaction_mapping_status text not null default 'NOT_MAPPED'
        check (interaction_mapping_status in ('NOT_MAPPED', 'REVIEWED_EXACT_MAP')),
    mapped_drug_substance_id text references drug_substances(drug_substance_id),
    mapping_basis text,
    retrieved_at timestamptz not null,
    raw_data jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint taiwan_drug_mapped_has_target check (
        (interaction_mapping_status = 'REVIEWED_EXACT_MAP') = (mapped_drug_substance_id is not null))
);
create unique index if not exists uq_taiwan_drug_ingredient_code on taiwan_drug_ingredients (ingredient_code) where ingredient_code is not null;
create unique index if not exists uq_taiwan_drug_ingredient_name_when_no_code on taiwan_drug_ingredients (normalized_exact_name) where ingredient_code is null;
create index if not exists idx_taiwan_drug_ingredient_name on taiwan_drug_ingredients (normalized_exact_name);
create index if not exists idx_taiwan_drug_ingredient_rank on taiwan_drug_ingredients (rank);

-- 可追溯：每個名錄成分涵蓋的未註銷藥證字號（只有字號；不含商品資料）
create table if not exists taiwan_drug_ingredient_permits (
    ingredient_id uuid not null references taiwan_drug_ingredients(id) on delete cascade,
    permit_number text not null,
    primary key (ingredient_id, permit_number)
);
create index if not exists idx_taiwan_drug_ingredient_permit_number on taiwan_drug_ingredient_permits (permit_number);

do $$
declare t text;
begin
    foreach t in array array['taiwan_drug_ingredients', 'taiwan_drug_ingredient_permits'] loop
        execute format('alter table %I enable row level security', t);
        execute format('revoke all on table %I from anon, authenticated', t);
        execute format('grant select, insert, update, delete on table %I to service_role', t);
    end loop;
end $$;

insert into app_schema_meta (key, value) values ('migration_0007_taiwan_drug_ingredient_catalog', 'applied')
on conflict (key) do update set value = excluded.value, updated_at = now();

commit;
