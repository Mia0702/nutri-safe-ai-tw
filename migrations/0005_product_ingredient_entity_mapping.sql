-- nutri_safe_ai（呷安心）0005：Product Ingredient Mapping Bridge（additive、idempotent）
--
-- 狀態：PROPOSAL（rev 2，2026-10-04 加入 reviewed overlay），尚未套用到任何 DB（需使用者同意後於 SQL Editor 手動執行）。
-- 為什麼不能只用既有 ingredients / product_ingredients（0001）：
--   * ingredients.id = uuid5(name_normalized)，代表 TFDA 官方名稱的 canonical 成分；Interaction Corpus 的 entity
--     （N_* 營養素群 / F_* form）是另一套 identity level，放進 ingredients 會讓「鈣」與「碳酸鈣」、「魚油」與「EPA」
--     在同一張表互相碰撞 name_normalized，等於隱性 SAME_AS。
--   * product_ingredients 只有 (product_id, ingredient_id) 主鍵、raw_text、amount_text，無法保存
--     mapping_status / confidence / review_status / 候選 entity / amount basis / corpus_version，
--     也無法保存「只是候選、不是正式 CONTAINS」的列。
-- 本 migration 只新增表 / view，不修改既有欄位語意、不改 ingredients / product_ingredients 內容、不寫入資料列（只有 marker）。
-- 不建立 interaction / evidence 表（下一輪），也沒有任何 Product↔Product 關係、沒有 SAME_AS。
-- 前置：0001 → 0002 → 0003 → 0004。整份在同一個 transaction，可重複執行。

begin;

do $$
begin
    if not exists (select 1 from app_schema_meta where key = 'project' and value = 'nutri_safe_ai') then
        raise exception '0005: 找不到 nutri_safe_ai 的 app_schema_meta 標記，請確認連線的是本專案 DB';
    end if;
    if not exists (select 1 from app_schema_meta where key = 'migration_0004_tfda_product_coverage') then
        raise exception '0005: 找不到 migration_0004_tfda_product_coverage 標記，請先依序套用 0001 → 0004';
    end if;
end $$;

-- identity / alias 的來源（base corpus interaction_sources.csv＋overlay reviewed_alias_sources.csv）
create table if not exists interaction_corpus_sources (
    source_id text primary key,                    -- TFDA_ADDITIVES_61、ODS_Zinc…
    authority text,
    source_tier text,                              -- A / B / INPUT_ONLY
    title text,
    source_url text,
    source_type text,                              -- OFFICIAL_DATASET / OFFICIAL_IDENTITY_REFERENCE / BASELINE_NOT_IDENTITY_EVIDENCE…
    retrieved_at timestamptz,
    last_updated_date text,
    sha256 text,
    corpus_version text,
    overlay_version text,
    raw_data jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

-- Interaction Corpus 的 ingredient entity（只放 INGREDIENT；drug substance 下一輪）
create table if not exists interaction_corpus_entities (
    entity_id text primary key,                    -- corpus / overlay 原始 ID（N_CA、F_CA_CARBONATE、F_ZN_OXIDE…）
    entity_type text not null check (entity_type = 'INGREDIENT'),
    graph_label text not null check (graph_label in ('NutrientGroup', 'IngredientForm')),
    canonical_name_en text not null,
    display_name_zh text,                          -- 顯示用譯名，不是 identity 證據
    identity_level text not null check (identity_level in (
        -- base corpus
        'NUTRIENT_GROUP', 'CHEMICAL_SUBSTANCE', 'SALT', 'MIXTURE',
        -- reviewed overlay 新增 form 的審核 identity level
        'SALT_FAMILY_UNSPECIFIED_HYDRATION', 'CHEMICAL_FORM', 'DECLARED_FATTY_ACID', 'DECLARED_SALT_HYDRATE',
        'VITAMER_FAMILY_HOMOLOG_UNSPECIFIED', 'DECLARED_STEREO_FORM', 'DECLARED_STEREO_SALT'
    )),
    overlay_identity_level text,                   -- 既有 F_* 經 overlay 審核的細分 level（不覆蓋 base identity_level）
    parent_nutrient_entity_id text references interaction_corpus_entities(entity_id),  -- CONTRIBUTES_TO_NUTRIENT，不是 SAME_AS
    entity_origin text not null default 'BASE_CORPUS' check (entity_origin in ('BASE_CORPUS', 'REVIEWED_OVERLAY')),
    source_id text not null,
    source_url text,
    review_status text not null,
    corpus_version text not null,
    overlay_version text,
    raw_data jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint interaction_entity_group_has_no_parent check (
        identity_level <> 'NUTRIENT_GROUP' or parent_nutrient_entity_id is null
    ),
    constraint interaction_entity_form_has_parent check (
        identity_level = 'NUTRIENT_GROUP' or parent_nutrient_entity_id is not null
    ),
    constraint interaction_entity_label_matches_level check (
        (graph_label = 'NutrientGroup') = (identity_level = 'NUTRIENT_GROUP')
    ),
    constraint interaction_entity_overlay_has_version check (
        entity_origin <> 'REVIEWED_OVERLAY' or overlay_version is not null
    )
);

-- alias：base corpus（ALIAS_*）、人工審核（REVIEWED:*）、reviewed overlay（OV1_ALIAS_*）
-- 正式 alias 必有 entity_id；overlay 候選（AMBIGUOUS / NEEDS_REVIEW）entity_id 為 NULL，只有 candidate_entity_id。
create table if not exists interaction_corpus_entity_aliases (
    alias_id text primary key,
    entity_id text references interaction_corpus_entities(entity_id),
    candidate_entity_id text references interaction_corpus_entities(entity_id),
    alias_text text not null,
    alias_normalized text not null,
    language text not null,
    alias_origin text not null default 'BASE_CORPUS'
        check (alias_origin in ('BASE_CORPUS', 'MANUAL_REVIEWED', 'REVIEWED_OVERLAY')),
    alias_relation text,                           -- base corpus：CANONICAL_NAME / SOURCED_SYNONYM_OR_ABBREVIATION / DISPLAY_TRANSLATION
    alias_status text,                             -- overlay：VERIFIED_OFFICIAL / SUPPORTED_AUTHORITATIVE / AMBIGUOUS / NEEDS_REVIEW
    identity_level text,
    match_policy text not null check (match_policy in (
        'EXACT_AT_DECLARED_IDENTITY_LEVEL', 'DISPLAY_ONLY_NOT_AUTOMERGE', 'MANUAL_REVIEWED_EXACT',
        'EXACT_NFKC_CASEFOLD_WHITESPACE_AT_DECLARED_LEVEL', 'CANDIDATE_ONLY_NOT_AUTOMERGE'
    )),
    mapping_context text,                          -- overlay：OFFICIAL_PRODUCT_INGREDIENT_TOKEN
    precedence text check (precedence is null or precedence in (
        'EXACT_OVERLAY', 'PRODUCT_FORM_CONTEXT_OVER_BASE_NUTRIENT_ALIAS'
    )),
    source_id text,
    source_url text,
    source_locator text,
    retrieved_at timestamptz,
    review_status text not null,
    reviewer text,                                 -- 只有 MANUAL_REVIEWED_EXACT
    review_basis text,
    corpus_version text not null,
    overlay_version text,
    notes text,
    created_at timestamptz not null default now(),
    constraint interaction_alias_manual_has_reviewer check (
        match_policy <> 'MANUAL_REVIEWED_EXACT' or (reviewer is not null and review_basis is not null)
    ),
    constraint interaction_alias_overlay_status check (
        alias_origin <> 'REVIEWED_OVERLAY' or (
            alias_status in ('VERIFIED_OFFICIAL', 'SUPPORTED_AUTHORITATIVE', 'AMBIGUOUS', 'NEEDS_REVIEW')
            and overlay_version is not null and source_id is not null
        )
    ),
    -- overlay 正式 ⇔ 有 entity；候選不得有正式 entity、不得是 exact policy
    constraint interaction_alias_overlay_formal_has_entity check (
        alias_origin <> 'REVIEWED_OVERLAY'
        or (alias_status in ('VERIFIED_OFFICIAL', 'SUPPORTED_AUTHORITATIVE')) = (entity_id is not null)
    ),
    constraint interaction_alias_candidate_not_exact check (
        alias_status is null or alias_status not in ('AMBIGUOUS', 'NEEDS_REVIEW')
        or (entity_id is null and match_policy = 'CANDIDATE_ONLY_NOT_AUTOMERGE')
    ),
    constraint interaction_alias_non_overlay_has_entity check (alias_origin = 'REVIEWED_OVERLAY' or entity_id is not null)
);
create index if not exists idx_interaction_alias_norm on interaction_corpus_entity_aliases (alias_normalized);
-- 同一 overlay 版本中，一段文字只能有一個正式 entity
create unique index if not exists uq_interaction_alias_overlay_formal on interaction_corpus_entity_aliases
    (alias_normalized, overlay_version) where alias_origin = 'REVIEWED_OVERLAY' and entity_id is not null;

-- IngredientForm -[:CONTRIBUTES_TO_NUTRIENT]-> NutrientGroup 的來源（不是 SAME_AS、不可換算含量、不繼承 interaction）
create table if not exists interaction_form_nutrient_links (
    relationship_id text primary key,              -- OV1_REL_*；base corpus parent 為 CORPUS_PARENT:<form>
    form_entity_id text not null unique references interaction_corpus_entities(entity_id),
    nutrient_entity_id text not null references interaction_corpus_entities(entity_id),
    relationship_type text not null check (relationship_type = 'CONTRIBUTES_TO_NUTRIENT'),
    basis text not null check (basis in ('CORPUS_PARENT_NUTRIENT', 'REVIEWED_OVERLAY')),
    source_id text,
    source_url text,
    source_locator text,
    evidence_text jsonb,
    review_status text not null,
    retrieved_at timestamptz,
    amount_conversion_allowed boolean not null default false check (amount_conversion_allowed = false),
    automatic_interaction_inheritance boolean not null default false check (automatic_interaction_inheritance = false),
    corpus_version text not null,
    overlay_version text,
    created_at timestamptz not null default now(),
    constraint form_nutrient_not_self check (form_entity_id <> nutrient_entity_id)
);

-- form → nutrient 必須與 entity 表的 parent 一致，且 form 不是營養素群、nutrient 是營養素群
create or replace function check_form_nutrient_link() returns trigger language plpgsql as $$
declare f interaction_corpus_entities; n interaction_corpus_entities;
begin
    select * into f from interaction_corpus_entities where entity_id = new.form_entity_id;
    select * into n from interaction_corpus_entities where entity_id = new.nutrient_entity_id;
    if f.identity_level = 'NUTRIENT_GROUP' or n.identity_level <> 'NUTRIENT_GROUP'
       or f.parent_nutrient_entity_id is distinct from new.nutrient_entity_id then
        raise exception using errcode = '23514',
            message = format('0005: form→nutrient 不一致：%s → %s', new.form_entity_id, new.nutrient_entity_id);
    end if;
    return new;
end $$;
drop trigger if exists trg_check_form_nutrient_link on interaction_form_nutrient_links;
create trigger trg_check_form_nutrient_link before insert or update on interaction_form_nutrient_links
    for each row execute function check_form_nutrient_link();

-- Product 成分 → corpus entity 的 mapping（正式＋候選都存；只有正式狀態才可同步成 Neo4j CONTAINS）
create table if not exists product_ingredient_entity_mappings (
    id uuid primary key,                           -- uuid5('product_ingredient_entity_mapping', 自然鍵)
    product_id uuid not null references products(id) on delete cascade,
    ingredient_source text not null
        check (ingredient_source in ('HEALTH_FOOD_CANONICAL_INGREDIENT', 'TFDA_REGISTRATION_INGREDIENTS_RAW')),
    source_ingredient_id uuid references ingredients(id),   -- health_food：既有 canonical Ingredient（不修改）
    token_index integer,                                     -- 登記類：研究 token 序號
    raw_text text not null,                                  -- 官方原文（原樣）
    mapping_status text not null check (mapping_status in (
        'VERIFIED_EXACT', 'SUPPORTED_ALIAS', 'FORM_TO_GROUP', 'AMBIGUOUS', 'NEEDS_REVIEW', 'NO_MATCH'
    )),
    is_formal boolean generated always as (
        mapping_status in ('VERIFIED_EXACT', 'SUPPORTED_ALIAS', 'FORM_TO_GROUP')
    ) stored,
    entity_id text references interaction_corpus_entities(entity_id),       -- 只有正式狀態
    entity_type text check (entity_type is null or entity_type in ('NutrientGroup', 'IngredientForm')),
    via_entity_id text references interaction_corpus_entities(entity_id),   -- FORM_TO_GROUP 的來源 form
    relationship_id text references interaction_form_nutrient_links(relationship_id),  -- FORM_TO_GROUP 的關係來源
    candidate_entity_ids text[] not null default '{}',                      -- 候選（不是 CONTAINS）
    match_method text not null,
    matched_alias_ids text[] not null default '{}',
    alias_evidence_status text check (alias_evidence_status is null or alias_evidence_status in (
        'VERIFIED_OFFICIAL', 'SUPPORTED_AUTHORITATIVE', 'AMBIGUOUS', 'NEEDS_REVIEW'
    )),
    overlay_precedence text,
    confidence numeric(4, 3) not null check (confidence between 0 and 1),   -- mapping 規則信心，不是臨床確定度
    review_status text not null check (review_status in (
        'AUTO_ACCEPTED_RULE', 'MANUAL_REVIEWED', 'SOURCE_REVIEWED', 'PENDING_REVIEW', 'REJECTED', 'NOT_APPLICABLE'
    )),
    source_parse_status text,                      -- 研究 staging READY / NEEDS_REVIEW（解析就緒度，不是 clinical verification）
    amount_raw text,
    amount_normalized numeric,                     -- 只有 unit 與 amount_basis 皆確定時
    unit text,
    amount_basis text not null default 'UNKNOWN'
        check (amount_basis in ('UNKNOWN', 'ELEMENTAL', 'SALT_WEIGHT', 'ACTIVE_COMPOUND')),
    source_id uuid references source_registry(id),
    source_record_key text,
    source_url text,
    retrieved_at timestamptz,
    corpus_version text not null,
    overlay_version text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint pie_formal_has_entity check (
        (mapping_status in ('VERIFIED_EXACT', 'SUPPORTED_ALIAS', 'FORM_TO_GROUP')) = (entity_id is not null)
    ),
    constraint pie_entity_type_with_entity check ((entity_id is null) = (entity_type is null)),
    constraint pie_form_to_group_has_via check ((mapping_status = 'FORM_TO_GROUP') = (via_entity_id is not null)),
    constraint pie_form_to_group_targets_group check (mapping_status <> 'FORM_TO_GROUP' or entity_type = 'NutrientGroup'),
    constraint pie_relationship_only_form_to_group check (relationship_id is null or mapping_status = 'FORM_TO_GROUP'),
    constraint pie_reviewed_form_has_relationship check (
        match_method <> 'REVIEWED_FORM_TO_NUTRIENT' or relationship_id is not null
    ),
    -- 候選：一律待審，且要有候選 entity 或審核 alias（overlay 可能審核為候選但未指定 entity）
    constraint pie_candidates_pending check (
        mapping_status not in ('AMBIGUOUS', 'NEEDS_REVIEW')
        or (review_status in ('PENDING_REVIEW', 'REJECTED')
            and (cardinality(candidate_entity_ids) > 0 or cardinality(matched_alias_ids) > 0))
    ),
    constraint pie_formal_not_pending check (
        mapping_status not in ('VERIFIED_EXACT', 'SUPPORTED_ALIAS', 'FORM_TO_GROUP')
        or review_status in ('AUTO_ACCEPTED_RULE', 'MANUAL_REVIEWED', 'SOURCE_REVIEWED')
    ),
    -- overlay 候選 alias 不得產生正式 mapping
    constraint pie_candidate_alias_not_formal check (
        alias_evidence_status is null or alias_evidence_status not in ('AMBIGUOUS', 'NEEDS_REVIEW')
        or mapping_status in ('AMBIGUOUS', 'NEEDS_REVIEW')
    ),
    constraint pie_overlay_alias_has_version check (alias_evidence_status is null or overlay_version is not null),
    -- 正式只能來自正式比對方法；候選只能來自候選方法（不能只改 mapping_status 就轉正）
    constraint pie_formal_method check (
        mapping_status not in ('VERIFIED_EXACT', 'SUPPORTED_ALIAS', 'FORM_TO_GROUP') or match_method in (
            'CORPUS_CANONICAL_NAME_EXACT', 'CORPUS_SOURCED_SYNONYM_EXACT', 'REVIEWED_ALIAS_EXACT',
            'REVIEWED_OVERLAY_ALIAS_EXACT', 'OFFICIAL_PARENTHETICAL', 'CORPUS_PARENT_NUTRIENT', 'REVIEWED_FORM_TO_NUTRIENT')
    ),
    constraint pie_candidate_method check (
        mapping_status not in ('AMBIGUOUS', 'NEEDS_REVIEW') or match_method in (
            'REVIEWED_OVERLAY_CANDIDATE', 'DISPLAY_TRANSLATION_EXACT', 'CORPUS_ALIAS_NOT_READY',
            'ALIAS_CONTAINED_IN_TEXT', 'CONFLICTING_ENTITIES')
    ),
    constraint pie_form_to_group_method check (
        (mapping_status = 'FORM_TO_GROUP') = (match_method in ('CORPUS_PARENT_NUTRIENT', 'REVIEWED_FORM_TO_NUTRIENT'))
    ),
    constraint pie_amount_normalized_needs_basis check (
        amount_normalized is null or (unit is not null and amount_basis <> 'UNKNOWN')
    ),
    constraint pie_source_unit check (num_nonnulls(source_ingredient_id, token_index) >= 1)
);
-- 自然鍵唯一（重跑 importer 不會產生重複 mapping；raw_text 以 md5 避免 btree 長度限制）
create unique index if not exists uq_pie_natural_key on product_ingredient_entity_mappings (
    product_id, ingredient_source, coalesce(source_ingredient_id::text, ''), coalesce(token_index, -1), md5(raw_text),
    mapping_status, coalesce(entity_id, ''), coalesce(via_entity_id, ''), corpus_version, coalesce(overlay_version, '')
);
create index if not exists idx_pie_product on product_ingredient_entity_mappings (product_id);
create index if not exists idx_pie_entity_formal on product_ingredient_entity_mappings (entity_id) where is_formal;
create index if not exists idx_pie_status on product_ingredient_entity_mappings (mapping_status);

-- 候選不得就地轉正（審核通過 = 新的 reviewed alias＋importer 產生新列）；
-- 正式列引用的 overlay alias 不得是候選，overlay 正式 alias 必須指向同一 entity（FORM_TO_GROUP 為來源 form）
create or replace function check_entity_mapping_row() returns trigger language plpgsql as $$
declare bad integer;
begin
    if tg_op = 'UPDATE' and old.mapping_status in ('AMBIGUOUS', 'NEEDS_REVIEW')
       and new.mapping_status in ('VERIFIED_EXACT', 'SUPPORTED_ALIAS', 'FORM_TO_GROUP') then
        raise exception using errcode = '23514', message = format('0005: 候選 mapping 不得就地轉正：%s', new.id);
    end if;
    if new.mapping_status in ('VERIFIED_EXACT', 'SUPPORTED_ALIAS', 'FORM_TO_GROUP') then
        select count(*) into bad from interaction_corpus_entity_aliases a
        where a.alias_id = any(new.matched_alias_ids) and a.alias_origin = 'REVIEWED_OVERLAY'
          and (a.alias_status in ('AMBIGUOUS', 'NEEDS_REVIEW')
               or a.entity_id is distinct from coalesce(new.via_entity_id, new.entity_id));
        if bad > 0 then
            raise exception using errcode = '23514', message = format('0005: 正式 mapping 引用候選 / 不一致的 overlay alias：%s', new.id);
        end if;
        if new.match_method = 'REVIEWED_OVERLAY_ALIAS_EXACT' or new.alias_evidence_status is not null then
            if not exists (select 1 from interaction_corpus_entity_aliases a
                           where a.alias_id = any(new.matched_alias_ids) and a.alias_origin = 'REVIEWED_OVERLAY'
                             and a.alias_status in ('VERIFIED_OFFICIAL', 'SUPPORTED_AUTHORITATIVE')) then
                raise exception using errcode = '23514', message = format('0005: overlay 正式 mapping 缺少正式 alias：%s', new.id);
            end if;
        end if;
    end if;
    return new;
end $$;
drop trigger if exists trg_check_entity_mapping_row on product_ingredient_entity_mappings;
create trigger trg_check_entity_mapping_row before insert or update on product_ingredient_entity_mappings
    for each row execute function check_entity_mapping_row();

-- importer 同步 Neo4j CONTAINS 的唯一來源：正式且直接的 mapping（FORM_TO_GROUP 由 CONTRIBUTES_TO_NUTRIENT 推導，不另建 CONTAINS）
create or replace view product_ingredient_entity_contains with (security_invoker = true) as
select product_id, entity_id, entity_type, mapping_status, match_method, review_status, confidence,
       raw_text, ingredient_source, matched_alias_ids, alias_evidence_status, amount_raw, unit, amount_basis,
       corpus_version, overlay_version
from product_ingredient_entity_mappings
where is_formal and mapping_status in ('VERIFIED_EXACT', 'SUPPORTED_ALIAS');

do $$
declare t text;
begin
    foreach t in array array['interaction_corpus_sources', 'interaction_corpus_entities', 'interaction_corpus_entity_aliases',
                             'interaction_form_nutrient_links', 'product_ingredient_entity_mappings'] loop
        execute format('alter table %I enable row level security', t);
        execute format('revoke all on table %I from anon, authenticated', t);
        execute format('grant select, insert, update, delete on table %I to service_role', t);
    end loop;
    revoke all on product_ingredient_entity_contains from anon, authenticated;
    grant select on product_ingredient_entity_contains to service_role;
end $$;

insert into app_schema_meta (key, value) values ('migration_0005_product_ingredient_entity_mapping', 'applied')
on conflict (key) do update set value = excluded.value, updated_at = now();

commit;
