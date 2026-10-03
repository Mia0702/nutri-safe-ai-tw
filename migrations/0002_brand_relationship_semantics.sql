-- nutri_safe_ai（呷安心）0002：Company → Brand 關係語意
--
-- 用途：把「已套用舊版 0001」的既有 Supabase DB 升級到目前 schema。
--   舊版 0001 沒有 brands.company_relation、對應 constraint，
--   search_entity_aliases 也沒有回傳 brand_company_relation。
--   既有 table 不會因重跑 create table if not exists 而更新，因此以本檔增量升級。
-- 全新 DB：先跑目前的 0001（已含相同定義），再跑本檔 → 本檔為 no-op。
-- 本檔可重複執行（idempotent）：欄位 add if not exists、constraint 只在不存在時建立、函式 create or replace。
-- 執行方式：Supabase Dashboard → SQL Editor 貼上整份執行（整份在同一個 transaction）。
--
-- 關係語意（與 app/schemas/enums.py BrandCompanyRelation 一致）：
--   OWNS_BRAND             擁有品牌：只限非推測（OFFICIAL_RECORD / MANUAL_REVIEWED）且 inference_basis 有 ownership_evidence
--   ASSOCIATED_WITH_BRAND  僅知有關聯（例如由產品名稱推導），不代表官方確認擁有

begin;

-- 防呆：只允許套用在本專案、且已套用 0001 的 DB
do $$
begin
    if to_regclass('public.app_schema_meta') is null then
        raise exception '0002: 找不到 app_schema_meta，請先套用 0001';
    end if;
    if not exists (select 1 from app_schema_meta where key = 'project' and value = 'nutri_safe_ai') then
        raise exception '0002: 找不到 nutri_safe_ai 的 app_schema_meta 標記，請確認連線的是本專案 DB 並已套用 0001';
    end if;
    if to_regclass('public.brands') is null then
        raise exception '0002: 找不到 brands table，請先套用 0001';
    end if;
end $$;

-- 1. 欄位：既有列一律補為 ASSOCIATED_WITH_BRAND（舊資料沒有 ownership_evidence，不可能是 OWNS_BRAND）
alter table brands add column if not exists company_relation text;
alter table brands alter column company_relation set default 'ASSOCIATED_WITH_BRAND';
update brands set company_relation = 'ASSOCIATED_WITH_BRAND' where company_relation is null;
alter table brands alter column company_relation set not null;

-- 2. constraints：只在不存在時建立，重跑不會重建或重複
do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conrelid = 'public.brands'::regclass and conname = 'brands_company_relation_valid'
    ) then
        alter table brands add constraint brands_company_relation_valid
            check (company_relation in ('OWNS_BRAND', 'ASSOCIATED_WITH_BRAND'));
    end if;
    if not exists (
        select 1 from pg_constraint
        where conrelid = 'public.brands'::regclass and conname = 'brands_owns_requires_evidence'
    ) then
        alter table brands add constraint brands_owns_requires_evidence check (
            company_relation <> 'OWNS_BRAND'
            or (evidence_status <> 'INFERRED' and company_id is not null and inference_basis ? 'ownership_evidence')
        );
    end if;
end $$;

-- 3. 別名查詢回傳品牌的 company_relation（簽章不變，create or replace 保留既有權限）
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

revoke execute on function search_entity_aliases(text[], text[], integer) from public, anon, authenticated;
grant execute on function search_entity_aliases(text[], text[], integer) to service_role;

-- 4. 記錄已套用（獨立 key，不與 0001 的 schema_version 互相覆蓋）
insert into app_schema_meta (key, value) values ('migration_0002_brand_relationship_semantics', 'applied')
on conflict (key) do nothing;

commit;
