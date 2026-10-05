-- nutri_safe_ai（呷安心）0006：Interaction Corpus data layer（additive、idempotent）
--
-- 狀態：PROPOSAL（Interaction Runtime Phase 1），尚未套用到任何 DB（需使用者同意後於 SQL Editor 手動執行）。
-- 與 0005 的分工：
--   * 0005 已有 ingredient 端 identity（interaction_corpus_entities / aliases / sources / form links）。
--     本 migration 的 participants 直接 FK 到 0005 的 entity_id（N_* / F_*），不另建第二套 Calcium / Zinc / Vitamin identity。
--   * 0005 的 entity_type 限定 INGREDIENT；drug substance（D_*）在這裡用獨立的 drug_substances，不混入 ingredient identity。
--   * evidence 的 source 直接 FK 到 0005 的 interaction_corpus_sources（39 筆 corpus source 已在其中）。
-- 不修改 0001–0005 的任何表 / 欄位；不寫入資料列（只有 marker）。
-- 不建立 Product↔Product、Product→Interaction、SAME_AS；Product 只能經 0005 的 entity mapping 與 interaction 相連。
-- severity / certainty / evidence_level / evidence_use 是各自獨立的欄位，不合併、不換算。
-- 前置：0001 → 0005。整份在同一個 transaction，可重複執行。

begin;

do $$
begin
    if not exists (select 1 from app_schema_meta where key = 'project' and value = 'nutri_safe_ai') then
        raise exception '0006: 找不到 nutri_safe_ai 的 app_schema_meta 標記，請確認連線的是本專案 DB';
    end if;
    if not exists (select 1 from app_schema_meta where key = 'migration_0005_product_ingredient_entity_mapping') then
        raise exception '0006: 找不到 migration_0005 標記，請先依序套用 0001 → 0005';
    end if;
end $$;

-- Drug substance identity（先使用 corpus 現有 identity；不建立台灣藥品商品資料）
-- 缺 TFDA 成分代碼 / 官方中文名 → NEEDS_REVIEW；不自行翻譯、不依中文名 auto-merge。
create table if not exists drug_substances (
    drug_substance_id text primary key,            -- corpus 原始 ID（D_WAR、D_DTG_SODIUM…）
    canonical_name_en text not null,
    official_name_zh text,                         -- 只接受官方來源；corpus 目前全部為 NULL
    identity_level text not null check (identity_level in ('ACTIVE_MOIETY', 'SALT')),
    parent_active_moiety_id text references drug_substances(drug_substance_id),
    tfda_ingredient_code text,
    cas text,
    unii text,
    rxnorm_id text,
    taiwan_code_mapping_status text not null check (taiwan_code_mapping_status in ('NEEDS_REVIEW', 'MAPPED_REVIEWED')),
    source_id text not null,
    source_url text,
    retrieved_at timestamptz,
    review_status text not null,
    corpus_version text not null,
    raw_data jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint drug_salt_has_parent check ((identity_level = 'SALT') = (parent_active_moiety_id is not null)),
    constraint drug_mapped_has_official_identity check (
        taiwan_code_mapping_status <> 'MAPPED_REVIEWED'
        or (tfda_ingredient_code is not null and official_name_zh is not null)
    )
);

-- drug alias：EXACT 只限英文 canonical / 來源同義詞；中文譯名一律只是顯示，不能用來比對或合併
create table if not exists drug_substance_aliases (
    alias_id text primary key,
    drug_substance_id text not null references drug_substances(drug_substance_id),
    alias_text text not null,
    alias_normalized text not null,
    language text not null,
    alias_relation text,
    match_policy text not null check (match_policy in ('EXACT_AT_DECLARED_IDENTITY_LEVEL', 'DISPLAY_ONLY_NOT_AUTOMERGE')),
    source_id text,
    review_status text not null,
    corpus_version text not null,
    created_at timestamptz not null default now(),
    constraint drug_alias_zh_display_only check (language not like 'zh%' or match_policy = 'DISPLAY_ONLY_NOT_AUTOMERGE')
);
create index if not exists idx_drug_alias_norm on drug_substance_aliases (alias_normalized);

-- Interaction：pairwise 臨床 / 生理交互作用，與 DUPLICATE_INTAKE 規則（重複攝取模板）語意分開
create table if not exists interactions (
    interaction_id text primary key,               -- INT_*（pairwise）、DUP_*（duplicate intake）
    record_kind text not null check (record_kind in ('PAIRWISE_INTERACTION', 'DUPLICATE_INTAKE_RULE')),
    interaction_type text not null,
    directionality text not null check (directionality in ('A_AFFECTS_B', 'B_AFFECTS_A', 'SELF_ACCUMULATION')),
    clinical_effect text,
    severity text not null,                        -- 後果嚴重度（來源判定）；與 certainty / evidence 分開
    severity_basis text,
    certainty text not null,                       -- 結論確定度；與 severity 分開
    evidence_level text not null,
    evidence_use text not null check (evidence_use in ('DECISION_ELIGIBLE', 'SUPPORTING', 'CONTEXT_ONLY', 'NEEDS_REVIEW')),
    decision_mode text not null,
    review_status text not null check (review_status in ('READY', 'NEEDS_REVIEW', 'EVIDENCE_CONFLICT')),
    mechanism_summary text,
    recommendation text,
    timing_advice text,
    dose_context text,
    population_context text,
    applicability_conditions text not null,        -- 結論只在這些條件成立時適用，不可省略
    dose_evaluation_default text not null default 'NOT_APPLICABLE'
        check (dose_evaluation_default in ('NOT_APPLICABLE', 'NOT_EVALUATED')),
    -- deterministic 結論資格：只由 corpus 既有欄位推導（READY ＋ DECISION_ELIGIBLE ＋ 非 CONFLICTING ＋ 可執行的 decision_mode）。
    -- EVIDENCE_CONFLICT / NEEDS_REVIEW / SUPPORTING / CONTEXT_ONLY 一律 false → 只能做說明，不能做肯定結論。
    deterministic_conclusion_allowed boolean generated always as (
        review_status = 'READY' and evidence_use = 'DECISION_ELIGIBLE' and certainty <> 'CONFLICTING'
        and decision_mode in ('CONDITIONAL_MESSAGE_ONLY', 'DUPLICATE_PRESENCE_OR_VALIDATED_ARITHMETIC_ONLY')
    ) stored,
    corpus_version text not null,
    retrieved_at timestamptz,
    raw_data jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    -- duplicate 是獨立語意：不是有害交互作用、沒有 severity 判定、沒有劑量結論
    constraint interaction_kind_matches_type check (
        (record_kind = 'DUPLICATE_INTAKE_RULE') = (interaction_type = 'DUPLICATE_INTAKE')
    ),
    constraint duplicate_rule_shape check (
        record_kind <> 'DUPLICATE_INTAKE_RULE' or (
            directionality = 'SELF_ACCUMULATION'
            and severity = 'DOSE_DEPENDENT_NOT_ASSIGNED'
            and dose_evaluation_default = 'NOT_EVALUATED'
        )
    ),
    constraint pairwise_shape check (
        record_kind <> 'PAIRWISE_INTERACTION'
        or (directionality in ('A_AFFECTS_B', 'B_AFFECTS_A') and dose_evaluation_default = 'NOT_APPLICABLE')
    ),
    constraint conflict_is_not_ready check (
        certainty <> 'CONFLICTING' or (review_status = 'EVIDENCE_CONFLICT' and evidence_use = 'NEEDS_REVIEW')
    ),
    constraint conflict_status_needs_conflict_semantics check (
        review_status <> 'EVIDENCE_CONFLICT' or certainty = 'CONFLICTING'
    )
);

-- 參與者：ingredient 端 FK 到 0005 identity、drug 端 FK 到 drug_substances；duplicate 只有一個 SELF 參與者
create table if not exists interaction_participants (
    interaction_id text not null references interactions(interaction_id) on delete cascade,
    role text not null check (role in ('AFFECTING', 'AFFECTED', 'SELF')),
    participant_kind text not null check (participant_kind in ('NutrientGroup', 'IngredientForm', 'DrugSubstance')),
    ingredient_entity_id text references interaction_corpus_entities(entity_id),
    drug_substance_id text references drug_substances(drug_substance_id),
    corpus_version text not null,
    created_at timestamptz not null default now(),
    primary key (interaction_id, role),
    constraint participant_exactly_one_identity check (
        (participant_kind = 'DrugSubstance') = (drug_substance_id is not null)
        and (participant_kind <> 'DrugSubstance') = (ingredient_entity_id is not null)
    )
);
create index if not exists idx_interaction_participants_ingredient on interaction_participants (ingredient_entity_id)
    where ingredient_entity_id is not null;
create index if not exists idx_interaction_participants_drug on interaction_participants (drug_substance_id)
    where drug_substance_id is not null;

-- participant 的類型必須符合 0005 entity 的 graph_label；SELF ⇔ duplicate rule（只能是營養素群 / form，不能是藥）
create or replace function check_interaction_participant() returns trigger language plpgsql as $$
declare k text; lbl text;
begin
    select record_kind into k from interactions where interaction_id = new.interaction_id;
    if (new.role = 'SELF') <> (k = 'DUPLICATE_INTAKE_RULE') then
        raise exception using errcode = '23514', message = format('0006: SELF 只屬於 DUPLICATE_INTAKE_RULE：%s', new.interaction_id);
    end if;
    if new.ingredient_entity_id is not null then
        select graph_label into lbl from interaction_corpus_entities where entity_id = new.ingredient_entity_id;
        if lbl is distinct from new.participant_kind then
            raise exception using errcode = '23514',
                message = format('0006: participant 類型與 entity 不一致：%s → %s', new.interaction_id, new.ingredient_entity_id);
        end if;
    end if;
    if k = 'DUPLICATE_INTAKE_RULE' and new.participant_kind = 'DrugSubstance' then
        raise exception using errcode = '23514', message = format('0006: duplicate rule 不可有藥品參與者：%s', new.interaction_id);
    end if;
    return new;
end $$;
drop trigger if exists trg_check_interaction_participant on interaction_participants;
create trigger trg_check_interaction_participant before insert or update on interaction_participants
    for each row execute function check_interaction_participant();

-- Evidence：保留來源追溯；supports_direction / evidence_use 逐筆保存（衝突證據保持衝突語意）
create table if not exists interaction_evidence (
    evidence_id text primary key,
    interaction_id text not null references interactions(interaction_id) on delete cascade,
    source_id text not null references interaction_corpus_sources(source_id),
    source_type text not null,
    authority text,
    is_official boolean not null,                  -- source_type 為 OFFICIAL_*（PubMed 文獻不是官方裁示）
    title text,
    study_type text,
    pmid text,
    doi text,
    publication_date text,
    retrieved_at timestamptz,
    source_retrieval_status text not null,         -- 來源取得狀態；取得失敗的來源不可作為 evidence
    population text,
    sample_size text,
    dose_context text,
    result_summary_zh text,
    limitations_zh text,
    supports_direction text not null,
    evidence_level text not null,
    certainty text not null,                       -- 單筆證據的 GRADE 類確定度（多為 NOT_FORMALLY_GRADED），不等於 interaction.certainty
    evidence_use text not null check (evidence_use in ('DECISION_ELIGIBLE', 'SUPPORTING', 'CONTEXT_ONLY')),
    review_status text not null,
    source_url text,
    article_or_section text,
    applicability_scope text,
    publication_types jsonb,
    corpus_version text not null,
    raw_data jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    constraint evidence_source_retrieved check (
        source_retrieval_status !~ '^(FETCH_FAILED|BLOCKED|UNVERIFIED)'
    )
);
create index if not exists idx_interaction_evidence_interaction on interaction_evidence (interaction_id);
create index if not exists idx_interaction_evidence_source on interaction_evidence (source_id);

-- 唯讀稽核 view：違反資料層語意的列（匯入後應為 0 列）
create or replace view interaction_integrity_violations with (security_invoker = true) as
select i.interaction_id, 'NO_EVIDENCE' as violation
from interactions i where not exists (select 1 from interaction_evidence e where e.interaction_id = i.interaction_id)
union all
select i.interaction_id, 'ELIGIBLE_WITHOUT_ELIGIBLE_EVIDENCE'
from interactions i where i.deterministic_conclusion_allowed and not exists (
    select 1 from interaction_evidence e where e.interaction_id = i.interaction_id and e.evidence_use = 'DECISION_ELIGIBLE')
union all
select i.interaction_id, 'BAD_PARTICIPANT_SHAPE'
from interactions i
where (i.record_kind = 'PAIRWISE_INTERACTION' and (
          select count(*) from interaction_participants p where p.interaction_id = i.interaction_id and p.role in ('AFFECTING', 'AFFECTED')) <> 2)
   or (i.record_kind = 'DUPLICATE_INTAKE_RULE' and (
          select count(*) from interaction_participants p where p.interaction_id = i.interaction_id and p.role = 'SELF') <> 1)
union all
select i.interaction_id, 'PAIRWISE_SAME_PARTICIPANT'
from interactions i
join interaction_participants a on a.interaction_id = i.interaction_id and a.role = 'AFFECTING'
join interaction_participants b on b.interaction_id = i.interaction_id and b.role = 'AFFECTED'
where i.record_kind = 'PAIRWISE_INTERACTION'
  and a.ingredient_entity_id is not distinct from b.ingredient_entity_id
  and a.drug_substance_id is not distinct from b.drug_substance_id;

-- Runtime 之後只應該讀這個 view 取得「可做 deterministic 結論」的 pairwise interaction（duplicate rule 不在其中）
create or replace view interaction_conclusion_eligible with (security_invoker = true) as
select i.* from interactions i
where i.record_kind = 'PAIRWISE_INTERACTION' and i.deterministic_conclusion_allowed;

do $$
declare t text;
begin
    foreach t in array array['drug_substances', 'drug_substance_aliases', 'interactions', 'interaction_participants',
                             'interaction_evidence'] loop
        execute format('alter table %I enable row level security', t);
        execute format('revoke all on table %I from anon, authenticated', t);
        execute format('grant select, insert, update, delete on table %I to service_role', t);
    end loop;
    revoke all on interaction_integrity_violations, interaction_conclusion_eligible from anon, authenticated;
    grant select on interaction_integrity_violations, interaction_conclusion_eligible to service_role;
end $$;

insert into app_schema_meta (key, value) values ('migration_0006_interaction_corpus', 'applied')
on conflict (key) do update set value = excluded.value, updated_at = now();

commit;
