-- nutri_safe_ai（呷安心）0004：TFDA 產品登記覆蓋擴充（additive、idempotent）
--
-- 狀態：正式 migration，尚未套用到正式 DB（需使用者同意後手動執行）。
-- 用途：runtime 區分產品類別所需的 typed 欄位與 view 欄位。scripts/import_tfda_product_coverage.py --apply
--   會檢查本檔的 marker（migration_0004_tfda_product_coverage），沒有就拒絕寫入新類別。
--   * permits.expiry_date / signing_document_no：有效日期、簽審文件編號成為可查詢、可顯示的登記欄位。
--     official_status 不新增欄位：新類別 permits.status_raw = NULL、status = UNKNOWN 代表「官方資料集未提供狀態」
--     （runtime：UNKNOWN ≠ EXPIRED / CANCELLED / REVOKED，不觸發「許可已非有效」風險）。
--   * products.product_category CHECK：只允許三種法律身分；既有 565 筆皆為 health_food（0001 預設值），不改值、不改 ID。
--   * product_details view 末尾追加 product_category / expiry_date / signing_document_no / permit_status_raw
--     （create or replace view 只能在末尾追加欄位；既有欄位與順序不變；search_products 以 to_jsonb 自動帶出）。
-- 不新增資料表；不修改既有欄位語意；不寫入任何資料列（只有 app_schema_meta marker）。
-- derived_temporal_status 不建欄位（會隨時間改變，由 runtime 依 expiry_date 計算，只顯示為日期狀態）。
-- 前置：0001 → 0002 → 0003。整份在同一個 transaction，可重複執行。

begin;

do $$
begin
    if not exists (select 1 from app_schema_meta where key = 'project' and value = 'nutri_safe_ai') then
        raise exception '0004: 找不到 nutri_safe_ai 的 app_schema_meta 標記，請確認連線的是本專案 DB';
    end if;
    if not exists (select 1 from app_schema_meta where key = 'migration_0003_claim_policy_corpus') then
        raise exception '0004: 找不到 migration_0003_claim_policy_corpus 標記，請先依序套用 0001 → 0002 → 0003';
    end if;
end $$;

alter table permits add column if not exists expiry_date date;
alter table permits add column if not exists signing_document_no text;

alter table products drop constraint if exists products_product_category_valid;
alter table products add constraint products_product_category_valid
    check (product_category in ('health_food', 'domestic_vitamin_tablet_capsule', 'imported_tablet_capsule'));
create index if not exists idx_products_category on products (product_category);

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
    s.is_official,
    p.product_category,
    pm.expiry_date,
    pm.signing_document_no,
    pm.status_raw as permit_status_raw
from products p
left join companies c on c.id = p.company_id
left join permits pm on pm.id = p.permit_id
left join source_registry s on s.id = p.source_id;

revoke all on table product_details from anon, authenticated;
grant select on table product_details to service_role;

insert into app_schema_meta (key, value) values ('migration_0004_tfda_product_coverage', 'applied')
on conflict (key) do update set value = excluded.value, updated_at = now();

commit;
