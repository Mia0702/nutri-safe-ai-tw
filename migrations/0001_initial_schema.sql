-- nutri_safe_ai（呷安心）Phase 1 初始 schema
-- 目標：完全空白的新 Supabase project（不得套用在含其他專案資料表的 DB 上）
-- 執行方式：Supabase Dashboard → SQL Editor 貼上整份執行，或 supabase db push
-- 本檔可重複執行（idempotent）：create ... if not exists / create or replace / drop trigger if exists
--
-- 設計原則
-- * Supabase 是 canonical source of truth；Neo4j 只存關係，並與這裡共用 canonical UUID。
-- * canonical UUID 由應用程式以 uuid5(自然鍵) 產生（app/normalization.py stable_id），
--   因此重複匯入同一筆官方資料一定得到同一個 id；另以自然鍵 unique constraint 雙重保護。
-- * 每筆從官方來源匯入的資料保留 source_id / source_record_key / source_url / retrieved_at / raw_record_id。
-- * 官方核准功效（approved_claims / product_claims）、使用者或廣告命題（observed_claims）、
--   違規廣告證據（illegal_ad_cases）分表儲存，不混在同一個欄位。
-- * 列舉型欄位以 CHECK constraint 限定，值與 app/schemas/enums.py 一致（tests/unit/test_migration_schema.py 會比對）。
-- * 所有 updated_at 由通用 trigger 在 UPDATE 時自動更新。
-- * 所有表開啟 RLS 且不建立 policy，並撤銷 anon / authenticated 權限：只有 service role（後端）可以存取。

create extension if not exists vector with schema extensions;

-- ---------------------------------------------------------------------------
-- 通用 updated_at trigger function
-- ---------------------------------------------------------------------------
create or replace function set_updated_at()
returns trigger
language plpgsql
set search_path = public
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

-- ---------------------------------------------------------------------------
-- schema marker：程式啟動時確認連到的是本專案 DB，避免誤寫入其他專案
-- 必須與 app/services/supabase_service.py 的 PROJECT_MARKER 相同
-- ---------------------------------------------------------------------------
create table if not exists app_schema_meta (
    key text primary key,
    value text not null,
    updated_at timestamptz not null default now()
);
insert into app_schema_meta (key, value) values
    ('project', 'nutri_safe_ai'),
    ('schema_version', '1')
on conflict (key) do update set value = excluded.value;

-- ---------------------------------------------------------------------------
-- 來源與原始資料
-- ---------------------------------------------------------------------------
create table if not exists source_registry (
    id uuid primary key default gen_random_uuid(),
    source_key text not null unique,              -- 例如 tw_fda_health_food_dataset
    source_name text not null,                    -- 顯示給使用者的來源名稱
    source_type text not null
        check (source_type in ('open_data', 'official_document', 'official_web', 'derived')),
    publisher text,
    base_url text,
    country_code text not null default 'TW',
    is_official boolean not null default true,
    description text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- 官方原始紀錄原樣保存；相同來源、相同紀錄、相同內容只會有一筆
create table if not exists raw_import_records (
    id uuid primary key default gen_random_uuid(),
    source_id uuid not null references source_registry(id),
    source_record_key text not null,
    source_url text,
    retrieved_at timestamptz not null,
    raw_data jsonb not null,
    raw_hash text not null,
    import_batch_id text,
    created_at timestamptz not null default now(),
    unique (source_id, source_record_key, raw_hash)
);
create index if not exists idx_raw_import_records_key on raw_import_records (source_id, source_record_key);

-- ---------------------------------------------------------------------------
-- 官方結構化資料
-- ---------------------------------------------------------------------------
create table if not exists companies (
    id uuid primary key,                          -- uuid5('company', country:name_normalized)
    country_code text not null default 'TW',
    name_zh text not null,
    name_en text,
    name_normalized text not null,
    source_id uuid references source_registry(id),
    source_record_key text,
    source_url text,
    retrieved_at timestamptz,
    raw_record_id uuid references raw_import_records(id),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (country_code, name_normalized)
);

create table if not exists permits (
    id uuid primary key,                          -- uuid5('permit', country:permit_number)
    country_code text not null default 'TW',
    permit_number text not null,                  -- 官方字號（去除「本證失效」等狀態後綴）
    permit_code text not null,                    -- 正規化代碼，例如 A00235
    permit_type text,                             -- 例如 健食字 / 健食規字
    approval_date date,
    status text not null default 'UNKNOWN'
        check (status in ('ACTIVE', 'EXPIRED', 'MERGED', 'CANCELLED', 'REVOKED', 'UNKNOWN')),
    status_raw text,
    source_id uuid references source_registry(id),
    source_record_key text,
    source_url text,
    retrieved_at timestamptz,
    raw_record_id uuid references raw_import_records(id),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (country_code, permit_number)
);
create index if not exists idx_permits_code on permits (permit_code);

-- 產品唯一性不依中文品名：
--   id = uuid5('product', country:permit_number)，並以 permit_id、(source_id, source_record_key) 雙重 unique 保護
create table if not exists products (
    id uuid primary key,
    country_code text not null default 'TW',
    product_name_zh text not null,
    product_name_en text,
    product_name_normalized text not null,
    product_category text not null default 'health_food',
    company_id uuid references companies(id),
    permit_id uuid references permits(id),
    status text not null default 'UNKNOWN'
        check (status in ('ACTIVE', 'EXPIRED', 'MERGED', 'CANCELLED', 'REVOKED', 'UNKNOWN')),
    source_id uuid references source_registry(id),
    source_record_key text,
    source_url text,
    retrieved_at timestamptz,
    raw_record_id uuid references raw_import_records(id),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint products_permit_unique unique (permit_id),
    constraint products_source_record_unique unique (source_id, source_record_key)
);
create index if not exists idx_products_name_norm on products (product_name_normalized);
create index if not exists idx_products_company on products (company_id);

create table if not exists ingredients (
    id uuid primary key,                          -- uuid5('ingredient', name_normalized)
    name_zh text not null,
    name_en text,
    name_normalized text not null unique,
    source_id uuid references source_registry(id),
    source_record_key text,
    retrieved_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists product_ingredients (
    product_id uuid not null references products(id) on delete cascade,
    ingredient_id uuid not null references ingredients(id),
    raw_text text,                                -- 官方原文（含含量）
    amount_text text,
    source_id uuid references source_registry(id),
    source_record_key text,
    source_url text,
    retrieved_at timestamptz,
    primary key (product_id, ingredient_id)
);
create index if not exists idx_product_ingredients_ingredient on product_ingredients (ingredient_id);

-- 官方核准的保健功效類別（例如「調節血脂」），跨產品共用
create table if not exists approved_claims (
    id uuid primary key,
    country_code text not null default 'TW',
    function_name text not null,
    function_key text not null,
    claim_kind text not null default 'HEALTH_FOOD_FUNCTION',
    source_id uuid references source_registry(id),
    created_at timestamptz not null default now(),
    unique (country_code, function_key)
);

-- 產品被核准的功效敘述原文
create table if not exists product_claims (
    id uuid primary key,
    product_id uuid not null references products(id) on delete cascade,
    approved_claim_id uuid not null references approved_claims(id),
    claim_type text not null default 'APPROVED_OFFICIAL' check (claim_type = 'APPROVED_OFFICIAL'),
    claim_text text not null,
    source_id uuid references source_registry(id),
    source_record_key text,
    source_url text,
    retrieved_at timestamptz,
    unique (product_id, approved_claim_id)
);

create table if not exists warnings (
    id uuid primary key,
    warning_type text not null check (warning_type in ('WARNING', 'PRECAUTION')),
    text text not null,
    text_hash text not null unique,
    source_id uuid references source_registry(id),
    created_at timestamptz not null default now()
);

create table if not exists product_warnings (
    product_id uuid not null references products(id) on delete cascade,
    warning_id uuid not null references warnings(id),
    source_id uuid references source_registry(id),
    source_record_key text,
    source_url text,
    retrieved_at timestamptz,
    primary key (product_id, warning_id)
);

create table if not exists illegal_ad_cases (
    id uuid primary key default gen_random_uuid(),
    country_code text not null default 'TW',
    case_key text not null unique,
    product_name text,
    product_name_normalized text,
    company_name text,
    company_name_normalized text,
    violation_content text not null,
    violation_type text,
    penalty_date date,
    published_date date,
    source_id uuid references source_registry(id),
    source_record_key text,
    source_url text,
    retrieved_at timestamptz,
    raw_record_id uuid references raw_import_records(id),
    created_at timestamptz not null default now()
);
create index if not exists idx_illegal_ad_product on illegal_ad_cases (product_name_normalized);
create index if not exists idx_illegal_ad_company on illegal_ad_cases (company_name_normalized);

-- ---------------------------------------------------------------------------
-- Brand 與 Entity Alias
-- 不得手動加入特定品牌特例；品牌與別名必須來自官方資料或可重現的推導規則。
--
-- evidence_status 區分事實的確認程度：
--   OFFICIAL_RECORD  官方資料原文直接記載（例如官方成分欄位的英文名）
--   INFERRED         系統依規則推測（例如由同公司產品名稱共同前綴推導的品牌），不是官方確認
--   MANUAL_REVIEWED  經人工審核
-- INFERRED 一律必須保存 inference_basis（推導依據）且 confidence < 1；is_inferred 由資料庫自動計算。
-- ---------------------------------------------------------------------------
create table if not exists brands (
    id uuid primary key,                          -- uuid5('brand', country:company_id:name_normalized)
    country_code text not null default 'TW',
    name text not null,
    name_normalized text not null,
    company_id uuid references companies(id),     -- 與品牌關聯的申請商；是否「擁有」見 company_relation
    derivation text not null
        check (derivation in ('OFFICIAL_RECORD', 'DERIVED_BRAND_PREFIX', 'MANUAL_REVIEWED')),
    evidence_status text not null
        check (evidence_status in ('OFFICIAL_RECORD', 'INFERRED', 'MANUAL_REVIEWED')),
    is_inferred boolean generated always as (evidence_status = 'INFERRED') stored,
    confidence numeric(4, 3) not null check (confidence between 0 and 1),
    inference_basis jsonb not null default '{}'::jsonb,  -- 推導依據（規則、共用前綴的產品數 / 比例）
    source_id uuid references source_registry(id),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint brands_name_company_unique unique nulls not distinct (country_code, name_normalized, company_id),
    constraint brands_derived_is_inferred check (derivation <> 'DERIVED_BRAND_PREFIX' or evidence_status = 'INFERRED'),
    constraint brands_inferred_has_basis check (
        evidence_status <> 'INFERRED' or (inference_basis <> '{}'::jsonb and confidence < 1)
    )
);
create index if not exists idx_brands_company on brands (company_id);

-- 公司與品牌的關係語意（可重複執行；既有 DB 也會補上欄位）：
--   OWNS_BRAND             擁有品牌：只限非推測（OFFICIAL_RECORD / MANUAL_REVIEWED）且 inference_basis 有 ownership_evidence
--   ASSOCIATED_WITH_BRAND  僅知有關聯（例如由產品名稱推導），不代表官方確認擁有
-- 一家公司可以有 0..N 個品牌（brands_name_company_unique 只限制同公司同名）。
alter table brands add column if not exists company_relation text not null default 'ASSOCIATED_WITH_BRAND';
alter table brands drop constraint if exists brands_company_relation_valid;
alter table brands add constraint brands_company_relation_valid
    check (company_relation in ('OWNS_BRAND', 'ASSOCIATED_WITH_BRAND'));
alter table brands drop constraint if exists brands_owns_requires_evidence;
alter table brands add constraint brands_owns_requires_evidence check (
    company_relation <> 'OWNS_BRAND'
    or (evidence_status <> 'INFERRED' and company_id is not null and inference_basis ? 'ownership_evidence')
);

-- 產品屬於哪個品牌（推導結果帶 confidence 與 inference_basis）
create table if not exists product_brands (
    product_id uuid not null references products(id) on delete cascade,
    brand_id uuid not null references brands(id) on delete cascade,
    derivation text not null
        check (derivation in ('OFFICIAL_RECORD', 'DERIVED_BRAND_PREFIX', 'MANUAL_REVIEWED')),
    evidence_status text not null
        check (evidence_status in ('OFFICIAL_RECORD', 'INFERRED', 'MANUAL_REVIEWED')),
    is_inferred boolean generated always as (evidence_status = 'INFERRED') stored,
    confidence numeric(4, 3) not null check (confidence between 0 and 1),
    inference_basis jsonb not null default '{}'::jsonb,
    source_id uuid references source_registry(id),
    created_at timestamptz not null default now(),
    primary key (product_id, brand_id),
    constraint product_brands_derived_is_inferred check (derivation <> 'DERIVED_BRAND_PREFIX' or evidence_status = 'INFERRED'),
    constraint product_brands_inferred_has_basis check (
        evidence_status <> 'INFERRED' or (inference_basis <> '{}'::jsonb and confidence < 1)
    )
);
create index if not exists idx_product_brands_brand on product_brands (brand_id);

-- 別名：每一列指向「恰好一個」canonical entity，並有 FK 保證實體存在
create table if not exists entity_aliases (
    id uuid primary key,                          -- uuid5('alias', entity_type:entity_id:alias_normalized)
    entity_type text not null check (entity_type in ('PRODUCT', 'BRAND', 'COMPANY', 'INGREDIENT')),
    product_id uuid references products(id) on delete cascade,
    brand_id uuid references brands(id) on delete cascade,
    company_id uuid references companies(id) on delete cascade,
    ingredient_id uuid references ingredients(id) on delete cascade,
    entity_id uuid generated always as (coalesce(product_id, brand_id, company_id, ingredient_id)) stored,
    alias text not null,
    alias_normalized text not null,
    alias_type text not null check (alias_type in (
        'OFFICIAL_NAME', 'ENGLISH_NAME', 'SHORT_NAME', 'BRAND_NAME',
        'DERIVED_BRAND_PREFIX', 'COMMON_NAME', 'FORMER_NAME'
    )),
    evidence_status text not null
        check (evidence_status in ('OFFICIAL_RECORD', 'INFERRED', 'MANUAL_REVIEWED')),
    is_inferred boolean generated always as (evidence_status = 'INFERRED') stored,
    source_id uuid references source_registry(id),
    source_record_key text,
    source_url text,
    confidence numeric(4, 3) not null check (confidence between 0 and 1),
    inference_basis jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint entity_aliases_one_target check (
        num_nonnulls(product_id, brand_id, company_id, ingredient_id) = 1
        and (
            (entity_type = 'PRODUCT' and product_id is not null)
            or (entity_type = 'BRAND' and brand_id is not null)
            or (entity_type = 'COMPANY' and company_id is not null)
            or (entity_type = 'INGREDIENT' and ingredient_id is not null)
        )
    ),
    constraint entity_aliases_derived_is_inferred check (alias_type <> 'DERIVED_BRAND_PREFIX' or evidence_status = 'INFERRED'),
    constraint entity_aliases_inferred_has_basis check (
        evidence_status <> 'INFERRED' or (inference_basis <> '{}'::jsonb and confidence < 1)
    ),
    constraint entity_aliases_unique unique (entity_type, entity_id, alias_normalized)
);
create index if not exists idx_entity_aliases_alias on entity_aliases (alias_normalized);

-- ---------------------------------------------------------------------------
-- RAG（pgvector）
-- 去重策略：
--   * content_hash（正規化全文的 sha256）全域唯一：相同內容不論網址是否為 null 都不會重複匯入。
--   * 同一官方網址（正規化後）只允許一個 is_current = true 的版本；內容更新時舊版改為 false。
-- ---------------------------------------------------------------------------
create table if not exists rag_documents (
    id uuid primary key default gen_random_uuid(),
    title text not null,
    source_url text,
    source_url_normalized text,
    source_name text not null,
    source_id uuid references source_registry(id),
    document_type text not null
        check (document_type in ('regulation', 'guideline', 'announcement', 'faq', 'other')),
    country_code text not null default 'TW',
    is_official boolean not null default true,
    is_current boolean not null default true,
    published_at date,
    retrieved_at timestamptz not null,
    content_hash text not null,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint rag_documents_content_hash_unique unique (content_hash),
    constraint rag_documents_url_or_source check (source_url is not null or source_id is not null)
);
create unique index if not exists rag_documents_current_url_unique
    on rag_documents (source_url_normalized) where is_current and source_url_normalized is not null;

create table if not exists rag_chunks (
    id uuid primary key default gen_random_uuid(),
    document_id uuid not null references rag_documents(id) on delete cascade,
    chunk_index int not null,
    content text not null,
    embedding extensions.vector(1536) not null,
    embedding_model text not null,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    unique (document_id, chunk_index)
);
create index if not exists idx_rag_chunks_embedding
    on rag_chunks using hnsw (embedding extensions.vector_cosine_ops);

-- ---------------------------------------------------------------------------
-- LINE 使用者與分析紀錄
-- line_users.id                 = 本系統內部 UUID
-- line_users.line_user_id       = LINE 平台的 userId（U 開頭字串）
-- analysis_requests.line_user_record_id → line_users.id（內部 UUID，不是 LINE userId）
-- ---------------------------------------------------------------------------
create table if not exists line_users (
    id uuid primary key default gen_random_uuid(),
    line_user_id text not null unique,
    follow_status text not null default 'following' check (follow_status in ('following', 'unfollowed')),
    followed_at timestamptz,
    unfollowed_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists analysis_requests (
    id uuid primary key,                          -- = request_id
    line_user_record_id uuid references line_users(id),
    channel text not null check (channel in ('debug_api', 'line')),
    input_type text not null check (input_type in ('text', 'image', 'audio', 'url', 'mixed')),
    intent text,
    normalized_request jsonb,
    product_verification text check (product_verification in (
        'VERIFIED', 'PARTIAL_MATCH', 'NOT_FOUND', 'CONFLICT', 'NOT_APPLICABLE'
    )),
    evidence_strength text check (evidence_strength in ('STRONG', 'MEDIUM', 'WEAK', 'NONE')),
    risk_level text check (risk_level in ('LOW', 'MEDIUM', 'HIGH')),
    risk jsonb,
    final_answer text,
    error_codes text[] not null default '{}',
    duration_ms int,
    created_at timestamptz not null default now()
);
create index if not exists idx_analysis_requests_line_user on analysis_requests (line_user_record_id);

create table if not exists evidence_logs (
    id uuid primary key default gen_random_uuid(),
    request_id uuid not null references analysis_requests(id) on delete cascade,
    evidence_kind text not null check (evidence_kind in (
        'product_match', 'approved_claim', 'warning', 'graph', 'rag', 'official_web',
        'illegal_ad', 'rejected_evidence', 'tool_call'
    )),
    evidence jsonb not null,
    source_name text,
    source_url text,
    is_official boolean,
    created_at timestamptz not null default now()
);
create index if not exists idx_evidence_logs_request on evidence_logs (request_id);

-- 使用者 / 廣告 / 轉傳中出現的命題（不是官方核准功效）。
-- origin = statement role；只有 PRODUCT_LABEL_CLAIM / ADVERTISEMENT_CLAIM 是產品方宣稱，
-- USER_QUESTION 是使用者提問，不能視為產品宣稱。
create table if not exists observed_claims (
    id uuid primary key default gen_random_uuid(),
    request_id uuid not null references analysis_requests(id) on delete cascade,
    claim_index int not null,
    claim_text text not null,
    origin text not null check (origin in (
        'USER_QUESTION', 'USER_ASSERTION', 'PRODUCT_LABEL_CLAIM',
        'ADVERTISEMENT_CLAIM', 'THIRD_PARTY_CLAIM', 'OFFICIAL_CLAIM'
    )),
    is_product_side_assertion boolean generated always as (
        origin in ('PRODUCT_LABEL_CLAIM', 'ADVERTISEMENT_CLAIM')
    ) stored,
    semantic_category text not null check (semantic_category in (
        'health_function', 'nutrient_supplement', 'medical_efficacy',
        'exaggerated_efficacy', 'general_wellness', 'other'
    )),
    assessment_status text check (assessment_status in (
        'CONSISTENT_WITH_OFFICIAL', 'UNSUPPORTED', 'POTENTIALLY_EXAGGERATED',
        'MEDICAL_TREATMENT_CLAIM', 'CONFLICT_WITH_OFFICIAL', 'UNKNOWN', 'NOT_APPLICABLE'
    )),
    created_at timestamptz not null default now(),
    unique (request_id, claim_index)
);

-- ---------------------------------------------------------------------------
-- updated_at trigger：所有含 updated_at 欄位的 public table 自動掛上
-- ---------------------------------------------------------------------------
do $$
declare t text;
begin
    for t in
        select c.table_name
        from information_schema.columns c
        join information_schema.tables tb
          on tb.table_schema = c.table_schema and tb.table_name = c.table_name
        where c.table_schema = 'public' and c.column_name = 'updated_at' and tb.table_type = 'BASE TABLE'
    loop
        execute format('drop trigger if exists trg_%1$s_updated_at on %1$I', t);
        execute format(
            'create trigger trg_%1$s_updated_at before update on %1$I for each row execute function set_updated_at()',
            t
        );
    end loop;
end $$;

-- ---------------------------------------------------------------------------
-- 查詢 view / 函式（名稱以「詞片段命中數」排序，最終比對分數由應用程式計算）
-- ---------------------------------------------------------------------------
create or replace view product_details
with (security_invoker = true)
as
select
    p.id as product_id,
    p.country_code,
    p.product_name_zh,
    p.product_name_en,
    p.product_name_normalized,
    p.status,
    p.company_id,
    c.name_zh as company_name,
    c.name_normalized as company_name_normalized,
    p.permit_id,
    pm.permit_number,
    pm.permit_code,
    pm.permit_type,
    pm.approval_date,
    pm.status as permit_status,
    p.source_record_key,
    p.source_url,
    p.retrieved_at,
    s.source_name,
    s.is_official
from products p
left join companies c on c.id = p.company_id
left join permits pm on pm.id = p.permit_id
left join source_registry s on s.id = p.source_id;

create or replace function search_products(p_terms text[], p_limit int default 20)
returns table (product jsonb, hit_count int)
language sql stable
set search_path = public
as $$
    select to_jsonb(d), h.hits
    from product_details d
    cross join lateral (
        select count(*)::int as hits
        from unnest(p_terms) t
        where length(t) > 0
          and (d.product_name_normalized like '%' || t || '%'
               or coalesce(d.company_name_normalized, '') like '%' || t || '%')
    ) h
    where h.hits > 0
    order by h.hits desc, length(d.product_name_normalized) asc
    limit p_limit;
$$;

create or replace function search_ingredients(p_terms text[], p_limit int default 20)
returns table (ingredient jsonb, hit_count int)
language sql stable
set search_path = public
as $$
    select to_jsonb(i), h.hits
    from ingredients i
    cross join lateral (
        select count(*)::int as hits
        from unnest(p_terms) t
        where length(t) > 0
          and (i.name_normalized like '%' || t || '%'
               or lower(coalesce(i.name_en, '')) like '%' || t || '%')
    ) h
    where h.hits > 0
    order by h.hits desc, length(i.name_normalized) asc
    limit p_limit;
$$;

create or replace function search_illegal_ad_cases(p_terms text[], p_limit int default 20)
returns table (ad_case jsonb, hit_count int)
language sql stable
set search_path = public
as $$
    select to_jsonb(a), h.hits
    from illegal_ad_cases a
    cross join lateral (
        select count(*)::int as hits
        from unnest(p_terms) t
        where length(t) > 0
          and (coalesce(a.product_name_normalized, '') like '%' || t || '%'
               or coalesce(a.company_name_normalized, '') like '%' || t || '%')
    ) h
    where h.hits > 0
    order by h.hits desc
    limit p_limit;
$$;

create or replace function search_entity_aliases(p_terms text[], p_entity_types text[], p_limit int default 20)
returns table (alias_row jsonb, hit_count int)
language sql stable
set search_path = public
as $$
    -- is_official 只代表「來源資料集」是否官方；別名本身是否為官方確認看 evidence_status / is_inferred
    select to_jsonb(a) || jsonb_build_object(
               'source_name', s.source_name,
               'source_is_official', s.is_official,
               'brand_company_id', b.company_id,
               'brand_company_relation', b.company_relation,
               'brand_is_inferred', b.is_inferred,
               'brand_inference_basis', b.inference_basis
           ),
           h.hits
    from entity_aliases a
    left join source_registry s on s.id = a.source_id
    left join brands b on b.id = a.brand_id
    cross join lateral (
        select count(*)::int as hits
        from unnest(p_terms) t
        where length(t) > 0
          and (a.alias_normalized like '%' || t || '%' or t like '%' || a.alias_normalized || '%')
    ) h
    where h.hits > 0 and a.entity_type = any(p_entity_types)
    order by h.hits desc, a.confidence desc
    limit p_limit;
$$;

create or replace function match_rag_chunks(
    query_embedding extensions.vector(1536),
    match_count int default 5,
    min_similarity float default 0.3,
    p_country_code text default 'TW'
)
returns table (
    chunk_id uuid,
    document_id uuid,
    chunk_index int,
    content text,
    chunk_metadata jsonb,
    similarity float,
    title text,
    source_url text,
    source_name text,
    document_type text,
    is_official boolean,
    retrieved_at timestamptz
)
language sql stable
set search_path = public, extensions
as $$
    select
        c.id, c.document_id, c.chunk_index, c.content, c.metadata,
        1 - (c.embedding <=> query_embedding) as similarity,
        d.title, d.source_url, d.source_name, d.document_type, d.is_official, d.retrieved_at
    from rag_chunks c
    join rag_documents d on d.id = c.document_id
    where d.country_code = p_country_code
      and d.is_current
      and 1 - (c.embedding <=> query_embedding) >= min_similarity
    order by c.embedding <=> query_embedding
    limit match_count;
$$;

-- ---------------------------------------------------------------------------
-- 權限：RLS 全開不設 policy；撤銷 anon / authenticated；只授權 service_role
-- ---------------------------------------------------------------------------
do $$
declare
    t text;
    tables text[] := array[
        'app_schema_meta', 'source_registry', 'raw_import_records', 'companies', 'permits',
        'products', 'ingredients', 'product_ingredients', 'approved_claims', 'product_claims',
        'warnings', 'product_warnings', 'illegal_ad_cases', 'brands', 'product_brands',
        'entity_aliases', 'rag_documents', 'rag_chunks', 'line_users', 'analysis_requests',
        'evidence_logs', 'observed_claims'
    ];
begin
    foreach t in array tables loop
        execute format('alter table %I enable row level security', t);
        execute format('revoke all on table %I from anon, authenticated', t);
        execute format('grant select, insert, update, delete on table %I to service_role', t);
    end loop;
    execute 'revoke all on table product_details from anon, authenticated';
    execute 'grant select on table product_details to service_role';
end $$;

do $$
declare f text;
begin
    foreach f in array array[
        'search_products(text[], integer)',
        'search_ingredients(text[], integer)',
        'search_illegal_ad_cases(text[], integer)',
        'search_entity_aliases(text[], text[], integer)',
        'match_rag_chunks(extensions.vector, integer, double precision, text)'
    ] loop
        execute format('revoke execute on function %s from public, anon, authenticated', f);
        execute format('grant execute on function %s to service_role', f);
    end loop;
end $$;
