-- nutri_safe_ai（呷安心）0003：TFDA Claim Policy Corpus data layer（additive）
--
-- 用途：保存已驗證的 TFDA 宣稱法規 corpus（來源、規則、例示、保健功效目錄、匯入批次）。
--   * 只新增資料表，不修改 0001 / 0002 既有表、函式與權限。
--   * RAG 沿用既有 rag_documents / rag_chunks（不建立第二套 vector schema）；
--     corpus 來源同時登記到 source_registry，rag_documents.source_id 指向它。
--   * 違規廣告案例不在本檔範圍：illegal_ad_cases 本輪不填資料；open-data inventory 只存來源 metadata。
-- 前置：0001 → 0002。本檔可重複執行（idempotent），整份在同一個 transaction。
--
-- 識別：
--   * 每張表的 id 由應用程式以 uuid5（app/normalization.py stable_id）從 corpus 原始 ID 產生，
--     corpus 原始 ID（source_id / rule_id / example_id / function_id）另以 unique 欄位保存。
--   * content_hash = 資料欄位的 sha256；importer 以它判斷 insert / update / unchanged。
--   * corpus_version = 該筆內容最後一次變更時的 corpus 版本（research_reference_date）。
--
-- Quarantine（evidence_use，值與 app/ingestion/claim_policy_corpus.py EvidenceUse 一致）：
--   DECISION_ELIGIBLE        可作 deterministic 判斷依據
--   CORROBORATION_REQUIRED   可檢索，但不得作唯一決策證據（例如 OCR 未逐字校對）
--   CONTEXT_ONLY             可保存與檢索，不得作 deterministic final legal conclusion（NEEDS_REVIEW）
--   EXCLUDED_SUPERSEDED      已被取代：保存但預設不參與 current decision / current retrieval
--   跨表條件（來源被取代 / 需複核時規則跟著降級）由 importer 計算；本檔以 CHECK 保證同列狀態不矛盾。

begin;

do $$
begin
    if to_regclass('public.app_schema_meta') is null then
        raise exception '0003: 找不到 app_schema_meta，請先套用 0001';
    end if;
    if not exists (select 1 from app_schema_meta where key = 'project' and value = 'nutri_safe_ai') then
        raise exception '0003: 找不到 nutri_safe_ai 的 app_schema_meta 標記，請確認連線的是本專案 DB 並已套用 0001';
    end if;
    if to_regclass('public.source_registry') is null or to_regclass('public.rag_documents') is null then
        raise exception '0003: 找不到 source_registry / rag_documents，請先套用 0001';
    end if;
end $$;

-- ---------------------------------------------------------------------------
-- 匯入批次：同一份 corpus（檔案 sha256 相同）只有一列，重跑只更新 run_count / 結果
-- ---------------------------------------------------------------------------
create table if not exists claim_policy_import_batches (
    id uuid primary key,
    batch_key text not null unique,                 -- corpus 檔案 sha256 組合
    corpus_name text not null,
    corpus_version date not null,
    validation_checked_at timestamptz,
    validation_passed boolean not null,
    file_hashes jsonb not null default '{}'::jsonb,
    expected_counts jsonb not null default '{}'::jsonb,
    result_counts jsonb not null default '{}'::jsonb,
    status text not null check (status in ('RUNNING', 'COMPLETED', 'FAILED')),
    run_count int not null default 1 check (run_count >= 1),
    error_message text,
    started_at timestamptz not null default now(),
    finished_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- 官方來源（法規、公告、評估方法、QA、資料集 metadata）
-- ---------------------------------------------------------------------------
create table if not exists claim_policy_sources (
    id uuid primary key,
    source_id text not null unique,                 -- corpus 原始 ID，例如 SRC_xxx
    source_registry_id uuid not null unique references source_registry(id),
    country_code text not null default 'TW',
    authority text not null,
    document_title text not null,
    document_type text not null,                    -- corpus 原始分類（中文）
    document_number text,
    source_url text not null,
    official_domain text not null,
    published_date date,
    effective_date date,
    last_amended_date date,
    retrieved_at timestamptz not null,
    metadata_updated_at text,
    status text not null check (status in ('CURRENT', 'SUPERSEDED', 'NEEDS_REVIEW')),
    version_note text,
    date_review_status text not null check (date_review_status in ('VERIFIED', 'NEEDS_REVIEW', 'NOT_APPLICABLE_DATASET')),
    original_download_urls jsonb not null default '[]'::jsonb,
    raw_files jsonb not null default '[]'::jsonb,
    raw_manifest jsonb not null default '[]'::jsonb,        -- 原始檔 sha256 / 下載網址 / 擷取方式
    open_data_inventory jsonb not null default '[]'::jsonb, -- 只存資料集 metadata，不含案例
    is_official boolean not null default true,
    evidence_use text not null
        check (evidence_use in ('DECISION_ELIGIBLE', 'CORROBORATION_REQUIRED', 'CONTEXT_ONLY', 'EXCLUDED_SUPERSEDED')),
    quarantine_reasons text[] not null default '{}',
    corpus_name text not null,
    corpus_version date not null,
    content_hash text not null,
    record_version int not null default 1 check (record_version >= 1),
    first_import_batch_id uuid not null references claim_policy_import_batches(id),
    last_import_batch_id uuid not null references claim_policy_import_batches(id),
    raw_data jsonb not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint claim_policy_sources_superseded_excluded
        check (status <> 'SUPERSEDED' or evidence_use = 'EXCLUDED_SUPERSEDED'),
    constraint claim_policy_sources_review_not_decision
        check (status <> 'NEEDS_REVIEW' or evidence_use <> 'DECISION_ELIGIBLE')
);
create index if not exists idx_claim_policy_sources_status on claim_policy_sources (status, evidence_use);

-- ---------------------------------------------------------------------------
-- 規則（每列一條官方條文 / 附件項目的適用判斷）
-- ---------------------------------------------------------------------------
create table if not exists claim_policy_rules (
    id uuid primary key,
    rule_id text not null unique,                   -- corpus 原始 ID，例如 RULE_xxxx
    claim_policy_source_id uuid not null references claim_policy_sources(id),
    rule_category text not null,
    policy_effect text not null check (policy_effect in (
        'PERMITTED', 'RESTRICTED', 'PROHIBITED', 'REQUIRES_EVIDENCE', 'PRODUCT_SPECIFIC_ONLY', 'NEEDS_REVIEW'
    )),
    claim_category text not null,
    scope text,
    rule_summary_zh text,
    official_text text not null check (length(official_text) > 0),
    conditions text,
    exceptions text,
    legal_basis text,
    article_or_attachment text not null,
    effective_date date,
    status text not null check (status in ('CURRENT', 'SUPERSEDED', 'NEEDS_REVIEW')),
    review_status text not null check (review_status in ('SOURCE_TEXT_VERIFIED', 'SOURCE_VERIFIED_CONTEXT_REQUIRED', 'NEEDS_REVIEW')),
    evidence_use text not null
        check (evidence_use in ('DECISION_ELIGIBLE', 'CORROBORATION_REQUIRED', 'CONTEXT_ONLY', 'EXCLUDED_SUPERSEDED')),
    quarantine_reasons text[] not null default '{}',
    corpus_name text not null,
    corpus_version date not null,
    content_hash text not null,
    record_version int not null default 1 check (record_version >= 1),
    first_import_batch_id uuid not null references claim_policy_import_batches(id),
    last_import_batch_id uuid not null references claim_policy_import_batches(id),
    raw_data jsonb not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint claim_policy_rules_superseded_excluded
        check (status <> 'SUPERSEDED' or evidence_use = 'EXCLUDED_SUPERSEDED'),
    constraint claim_policy_rules_review_not_decision check (
        (status <> 'NEEDS_REVIEW' and review_status <> 'NEEDS_REVIEW' and policy_effect <> 'NEEDS_REVIEW')
        or evidence_use in ('CONTEXT_ONLY', 'EXCLUDED_SUPERSEDED')
    )
);
create index if not exists idx_claim_policy_rules_source on claim_policy_rules (claim_policy_source_id);
create index if not exists idx_claim_policy_rules_effect on claim_policy_rules (policy_effect, evidence_use);

-- ---------------------------------------------------------------------------
-- 官方例示詞句（屬於某條規則；例示來源可與規則來源不同）
-- ---------------------------------------------------------------------------
create table if not exists claim_policy_examples (
    id uuid primary key,
    example_id text not null unique,                -- corpus 原始 ID，例如 EX_xxxx
    claim_policy_rule_id uuid not null references claim_policy_rules(id),
    claim_policy_source_id uuid not null references claim_policy_sources(id),
    example_text text not null check (length(example_text) > 0),
    example_type text not null check (example_type in (
        'PERMITTED_EXAMPLE', 'GENERAL_FUNCTION_EXAMPLE', 'CONDITIONAL_EXAMPLE', 'MEDICAL_EFFICACY_EXAMPLE', 'PROHIBITED_EXAMPLE'
    )),
    claim_category text not null,
    conditions text,
    notes text,
    article_or_attachment text,
    evidence_use text not null
        check (evidence_use in ('DECISION_ELIGIBLE', 'CORROBORATION_REQUIRED', 'CONTEXT_ONLY', 'EXCLUDED_SUPERSEDED')),
    quarantine_reasons text[] not null default '{}',
    corpus_name text not null,
    corpus_version date not null,
    content_hash text not null,
    record_version int not null default 1 check (record_version >= 1),
    first_import_batch_id uuid not null references claim_policy_import_batches(id),
    last_import_batch_id uuid not null references claim_policy_import_batches(id),
    raw_data jsonb not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create index if not exists idx_claim_policy_examples_rule on claim_policy_examples (claim_policy_rule_id);
create index if not exists idx_claim_policy_examples_source on claim_policy_examples (claim_policy_source_id);

-- ---------------------------------------------------------------------------
-- 健康食品保健功效目錄（公告的功效項目＋現行評估方法）
-- ---------------------------------------------------------------------------
create table if not exists health_function_catalog (
    id uuid primary key,
    function_id text not null unique,               -- corpus 原始 ID，例如 HF_xx
    official_function_name text not null unique,
    official_definition text,                       -- 評估方法「適用範圍」原文；官方未提供時為 null，不自行補寫
    evaluation_method_title text,
    legal_basis text,
    catalog_source_id uuid not null references claim_policy_sources(id),
    evaluation_source_id uuid not null references claim_policy_sources(id),
    effective_date date,
    evaluation_effective_date date,
    catalog_effective_date date,
    status text not null check (status in ('CURRENT', 'SUPERSEDED', 'NEEDS_REVIEW')),
    review_status text not null check (review_status in ('SOURCE_TEXT_VERIFIED', 'SOURCE_VERIFIED_CONTEXT_REQUIRED', 'NEEDS_REVIEW')),
    notes text,
    evidence_use text not null
        check (evidence_use in ('DECISION_ELIGIBLE', 'CORROBORATION_REQUIRED', 'CONTEXT_ONLY', 'EXCLUDED_SUPERSEDED')),
    quarantine_reasons text[] not null default '{}',
    corpus_name text not null,
    corpus_version date not null,
    content_hash text not null,
    record_version int not null default 1 check (record_version >= 1),
    first_import_batch_id uuid not null references claim_policy_import_batches(id),
    last_import_batch_id uuid not null references claim_policy_import_batches(id),
    raw_data jsonb not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint health_function_catalog_superseded_excluded
        check (status <> 'SUPERSEDED' or evidence_use = 'EXCLUDED_SUPERSEDED'),
    constraint health_function_catalog_review_not_decision check (
        (status <> 'NEEDS_REVIEW' and review_status <> 'NEEDS_REVIEW')
        or evidence_use in ('CONTEXT_ONLY', 'EXCLUDED_SUPERSEDED')
    )
);

-- 功效項目的歷史（已被取代）評估方法：陣列無法建 FK，改以關聯表保存
create table if not exists health_function_historical_sources (
    health_function_id uuid not null references health_function_catalog(id) on delete cascade,
    claim_policy_source_id uuid not null references claim_policy_sources(id),
    last_import_batch_id uuid not null references claim_policy_import_batches(id),
    created_at timestamptz not null default now(),
    primary key (health_function_id, claim_policy_source_id)
);

-- ---------------------------------------------------------------------------
-- updated_at trigger（沿用 0001 的 set_updated_at）
-- ---------------------------------------------------------------------------
do $$
declare t text;
begin
    foreach t in array array[
        'claim_policy_import_batches', 'claim_policy_sources', 'claim_policy_rules',
        'claim_policy_examples', 'health_function_catalog'
    ] loop
        execute format('drop trigger if exists %I on %I', 'trg_' || t || '_updated_at', t);
        execute format(
            'create trigger %I before update on %I for each row execute function set_updated_at()',
            'trg_' || t || '_updated_at', t
        );
    end loop;
end $$;

-- ---------------------------------------------------------------------------
-- 權限：延續 0001——RLS 全開不設 policy；撤銷 anon / authenticated；只授權 service_role
-- ---------------------------------------------------------------------------
do $$
declare t text;
begin
    foreach t in array array[
        'claim_policy_import_batches', 'claim_policy_sources', 'claim_policy_rules',
        'claim_policy_examples', 'health_function_catalog', 'health_function_historical_sources'
    ] loop
        execute format('alter table %I enable row level security', t);
        execute format('revoke all on table %I from anon, authenticated', t);
        execute format('grant select, insert, update, delete on table %I to service_role', t);
    end loop;
end $$;

insert into app_schema_meta (key, value) values ('migration_0003_claim_policy_corpus', 'applied')
on conflict (key) do nothing;

commit;
