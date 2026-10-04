# TFDA 官方產品資料覆蓋擴充研究報告

研究基準：2026-10-04；retrieved_at 採 UTC ISO-8601。只整理官方可取得的兩類全量公開 export，不代表 TFDA 內部歷來登記資料全集。未操作 production、GitHub、Supabase、Neo4j 或 RAG；既有565筆健康食品完全未修改。

## 1. 筆數、下載與資料日期

| 項目 | 國產維生素錠狀／膠囊狀 | 輸入錠狀／膠囊狀 |
|---|---:|---:|
| data.gov.tw / InfoId | 9046 / 24 | 9047 / 23 |
| 原始／normalized 列數 | 1705 / 1705 | 7407 / 7407 |
| 最新可讀 metadata | 2026-09-10 10:40 | 2026-07-31 10:21 |
| export 內最新發證日期 | 2026-08-21 | 2026-06-18 |
| 最大有效日期 | 2031-11-03 | 2031-09-01 |
| 現行 metadata 更新頻率 | 每3月 | 每3月 |
| 資源本身最後更新时间 | NOT_AVAILABLE | NOT_AVAILABLE |

國產舊中文官方鏡像曾顯示2018-03-01 08:23、每日更新、上架2014-08-11；現行英文 metadata 顯示2026-09-10 10:40、每3月、上架2016-01-01。較早查詢亦見英文快照2026-06-01，重新開啟後取得9月更新版本，以後者記入現行 metadata。這些頁面／語系快照差異保留，不將舊 metadata 當作停止更新證據。國產內容有2026年發證資料，表示公開內容已超出舊metadata時間；僅憑此不能證明每日更新或零延遲。輸入 metadata 與內容最新發證日期亦不同。metadata timestamp 未自行添加時區；它不是 resource_updated_at。HTTP Date 和下載時間均不是資料更新時間。

原始CSV byte-for-byte保存；另下載同一官方API的全量JSON，將JSON null與CSV空欄對齊後，以包含所有原始欄位的多重集合比對，兩類均完全一致。API食品類OAS列出CSV／JSON export，未宣告limit或page參數，本次沒有抽樣、頁數限制或篩選。查詢頁無法成功擷取，因此無法獨立核对內部查詢系統總數。

## 2. 官方來源與現行查詢入口

| source_id | 用途 | 官方URL | retrieved_at | 結果 |
|---|---|---|---|---|
| SRC_DOMESTIC_CSV | CORE_FULL_PRODUCT_EXPORT | https://data.fda.gov.tw/data/opendata/export/24/csv | 2026-10-04T03:13:44.070804+00:00 | SUCCESS |
| SRC_IMPORTED_CSV | CORE_FULL_PRODUCT_EXPORT | https://data.fda.gov.tw/data/opendata/export/23/csv | 2026-10-04T03:13:51.254225+00:00 | SUCCESS |
| SRC_DOMESTIC_METADATA | REFERENCE_METADATA_OR_OFFICIAL_ENTRY | https://data.gov.tw/en/datasets/9046 | 2026-10-04T03:30:36.534202+00:00 | SUCCESS |
| SRC_IMPORTED_METADATA | REFERENCE_METADATA_OR_OFFICIAL_ENTRY | https://data.gov.tw/dataset/9047 | 2026-10-04T03:30:36.534202+00:00 | SUCCESS |
| SRC_DOMESTIC_OLD_METADATA | REFERENCE_METADATA_OR_OFFICIAL_ENTRY | https://data.nat.gov.tw/dataset/9046 | 2026-10-04T03:30:36.534202+00:00 | STALE_METADATA |
| SRC_DOMESTIC_MANAGEMENT | REFERENCE_METADATA_OR_OFFICIAL_ENTRY | https://www.fda.gov.tw/TC/site.aspx?sid=2120 | 2026-10-04T03:30:36.534202+00:00 | SUCCESS |
| SRC_IMPORTED_MANAGEMENT | REFERENCE_METADATA_OR_OFFICIAL_ENTRY | https://www.fda.gov.tw/TC/site.aspx?sid=1508 | 2026-10-04T03:30:36.534202+00:00 | SUCCESS |
| SRC_DOMESTIC_QUERY | REFERENCE_METADATA_OR_OFFICIAL_ENTRY | https://consumer.fda.gov.tw/Food/DomesticFormulationsQuery.aspx?nodeID=166 | 2026-10-04T03:30:36.534202+00:00 | FETCH_FAILED |
| SRC_IMPORTED_QUERY | REFERENCE_METADATA_OR_OFFICIAL_ENTRY | https://consumer.fda.gov.tw/Food/CapsuleAuditQuery.aspx?nodeID=165 | 2026-10-04T03:30:36.534202+00:00 | FETCH_FAILED |
| SRC_DOMESTIC_SCOPE | REFERENCE_METADATA_OR_OFFICIAL_ENTRY | https://www.fda.gov.tw/tc/newsContent.aspx?cid=3&id=22788 | 2026-10-04T03:30:36.534202+00:00 | SUCCESS |
| SRC_HEALTH_METADATA_REFERENCE | REFERENCE_METADATA_OR_OFFICIAL_ENTRY | https://data.gov.tw/en/datasets/6951 | 2026-10-04T03:30:36.534202+00:00 | SUCCESS |
| SRC_DOMESTIC_JSON | FULL_JSON_CROSSCHECK | https://data.fda.gov.tw/data/opendata/export/24/json | 2026-10-04T03:20:57.072018+00:00 | SUCCESS |
| SRC_IMPORTED_JSON | FULL_JSON_CROSSCHECK | https://data.fda.gov.tw/data/opendata/export/23/json | 2026-10-04T03:21:09.275509+00:00 | SUCCESS |
| SRC_OFFICIAL_FOOD_OPENAPI | OFFICIAL_ENDPOINT_SCHEMA | https://data.fda.gov.tw/data/dataset/oas/classification/8d4ca1bf-ae1d-4280-849e-2fa2947a9cb2 | 2026-10-04T03:26:46.262631+00:00 | SUCCESS |
| SRC_HEALTH_CSV_REFERENCE | MATCH_REFERENCE_ONLY_NOT_PRODUCTION | https://data.fda.gov.tw/data/opendata/export/19/csv | 2026-10-04T03:21:16.793501+00:00 | SUCCESS |

下載授權：資料集 metadata 的政府資料開放授權條款第1版。`05`同時記錄成功來源、舊metadata以及擷取失敗的官方查詢入口；來源總數不表示所有頁面均成功取得。國產查詢頁回502／輸入查詢頁timeout，不自行以其他資料庫替代。舊 `DataAction.do?method=doDetail&infoId=...` 現在導向Swagger頁；HTTP200不代表取得舊metadata。食品類OpenAPI為目前可以驗證的下載端點文件。

## 3. 實際原始欄位、資料型態及缺值

以下非推測schema，直接來自本次CSV表頭。所有原始值以字串保存；日期亦先保存為字串。`11_tfda_product_field_profiles.csv`逐欄列出缺值比例、非空唯一值數、日期格式、HTML/entity、換行及各分隔符的列數。

| InfoId | 中文原始欄位 | 空值數 | 空值比例 | 非空唯一值數 | 日期格式 |
|---|---|---:|---:|---:|---|
| 24 | 查驗登記證號 | 0 | 0.0000% | 1705 | {} |
| 24 | 中文品名 | 0 | 0.0000% | 1670 | {} |
| 24 | 英文品名 | 919 | 53.9003% | 769 | {} |
| 24 | 有效日期 | 0 | 0.0000% | 818 | {"ROC_YYYMMDD":1705} |
| 24 | 發證日期 | 1 | 0.0587% | 945 | {"ROC_YYYMMDD":1704} |
| 24 | 申請商名稱 | 0 | 0.0000% | 521 | {} |
| 24 | 申請商電話 | 1 | 0.0587% | 505 | {} |
| 24 | 申請商地址 | 0 | 0.0000% | 518 | {} |
| 24 | 成分 | 0 | 0.0000% | 1580 | {} |
| 23 | 核備函字號 | 0 | 0.0000% | 7407 | {} |
| 23 | 中文品名 | 0 | 0.0000% | 7178 | {} |
| 23 | 英文品名 | 0 | 0.0000% | 6779 | {} |
| 23 | 有效日期 | 0 | 0.0000% | 1283 | {"ROC_YYYMMDD":7407} |
| 23 | 發證日期 | 0 | 0.0000% | 1800 | {"ROC_YYMMDD":197,"ROC_YYYMMDD":7210} |
| 23 | 申請商名稱 | 0 | 0.0000% | 696 | {} |
| 23 | 申請商電話 | 2 | 0.0270% | 672 | {} |
| 23 | 申請商地址 | 0 | 0.0000% | 686 | {} |
| 23 | 成分 | 5 | 0.0675% | 6577 | {} |
| 23 | 簽審文件編號 | 0 | 0.0000% | 7407 | {} |

| 所需概念 | 國產 | 輸入 |
|---|---|---|
| 證號／核備字號、申請商、成分、發證與有效日期 | AVAILABLE | AVAILABLE |
| 英文品名 | AVAILABLE，919筆空白 | AVAILABLE |
| 簽審文件編號 | NOT_AVAILABLE | AVAILABLE |
| 官方狀態（ACTIVE/EXPIRED/CANCELLED/REVOKED/MERGED等） | NOT_AVAILABLE | NOT_AVAILABLE |
| 製造商及其地址 | NOT_AVAILABLE | NOT_AVAILABLE |
| 原產國 | NOT_AVAILABLE | NOT_AVAILABLE |
| 每筆劑型、規格、食用方式、定量配方、每份劑量 | NOT_AVAILABLE | NOT_AVAILABLE |

沒有HTML tag或換行的欄位不代表所有資料均可無語意處理：成分的HTML entities為國產1列、輸入215列；`;;`於國產1705列、輸入7402列出現，剛好等於非空成分列數。申請商地址與電話中的標點不直接拆為多個地址或電話。只有成分可確認有來源序列分隔符；其餘欄位多值語意 NOT_CONFIRMED。

## 4. 官方字號與 canonical key

兩類字號皆為10個數字字元；國產欄名是「查驗登記證號」，輸入是「核備函字號」。這是本次資料的觀測格式，不宣稱所有未來法定字號永遠為10碼，也不由數字推定核發機關、年代或法律狀態。輸入有前導零，例如0088072109，必須當字串。簽審文件編號保留為獨立欄位，不能當健康食品許可證。

建議主鍵：`TFDA:{source_info_id}:{原始官方字號}`。例如 `TFDA:23:0088072109`。本次兩類各自字號唯一、跨類別原始字號交集為0，因此此鍵deterministic且所有9112列唯一。不要用品名、申請商、列順序或去除前導零後的整數當Product鍵。

未來若出現同一字號不同原始紀錄：先標NEEDS_REVIEW，不覆寫或合併；產品登記鍵仍為InfoId＋字號，原始版本鍵可另加依全部欄位、固定序列化規格計算的SHA-256。若匯入器必須為變體每列建立鍵，可用該字號鍵＋內容雜湊；完全重複列不能靠內容雜湊分開，應在snapshot層保留occurrence／raw_row_number，而非造新產品。

## 5. 日期與 official status 分離

6或7碼民國日期嚴格拆成YY/YYY＋MM＋DD，民國年加1911，驗證真實日曆日期，輸出YYYY-MM-DD。國產發證日期有1筆缺值；輸入發證日期197筆為6碼、7210筆7碼；兩類有效日期皆7碼。無日期解析失敗、無發證日晚於有效日或研究日。原始日期保留在issue_date_raw／expiry_date_raw及raw JSON。不能按原始日期字串排序選「最新」：六碼與七碼混合會錯。

official_status一律NULL。以2026-10-04為基準，有效日期當天視為CURRENT_BY_DATE，早於該日才為EXPIRED_BY_DATE；未取得產品個別效期起訖時間，這只是明確的日期比較慣例。它不能證明仍有法律效力，也不是撤銷、廢止、註銷或核准功效狀態。
國產日期比較：CURRENT_BY_DATE=1695，EXPIRED_BY_DATE=10。輸入：CURRENT_BY_DATE=7115，EXPIRED_BY_DATE=292。

## 6. Canonical mapping、NULL及原始追溯

`06`逐一列出每類每個目標欄位的來源、可取得性與轉換。適合canonical Product登記層的值：官方中英文品名、具命名空間的登記字號、資料類別、資料集定義的permit_type、申請商及官方聯絡資訊、發證與有效日期、原始成分、來源URL與retrieved_at。permit_type只是來源制度／欄位的名稱，不是健康食品許可。

製造商、原產國、劑型及官方status留NULL；「國產」不是每個原料或產品原產國證據，申請商不是製造商。官方沒有個別劑型欄，不從品名中的錠、軟膠囊自行填值。簽審文件編號較適合registration／source record層，另加signing_document_no保留。raw_source_fields_json、原始日期、下載雜湊、raw_row_number、成分解析草稿／風險和derived_temporal_status應保留在source record或派生層。

CSV文字NULL表示缺失值；匯入器須顯式讀為null，不可当字串NULL。官方原始CSV不轉NULL、不改encoding或換行。normalized與其他生成CSV使用UTF-8 BOM；JSON／Markdown為UTF-8。raw_source_fields_json包含每個原始欄位及其原始字串，官方CSV空格／空字串不自行補值。每列raw_row_number從1起，指CSV資料記錄位置，不是檔案實體行號；raw_row_sha256以UTF-8、ensure_ascii=False、separators=(逗號,冒號)、表頭插入順序的JSON物件計算，未排序。raw_source_fields_json可以完整重建該列的欄位值；byte-for-byte檔案追溯仍須用原始檔sha256。

## 7. 成分品質與 parsing 風險

ingredients_raw完全保留原文；ingredients_normalized是JSON字串draft，先只在草稿decodeHTML entities，再以literal `;;`切來源token、去除token外側空白、保留順序與重複；末尾empty delimiter不造空化學成分。這是來源文字token序列，不能等同已識別的化學成分、各成分含量或已確定的複方BOM。草稿明確標chemical_identification／compound_expansion／dose_extraction均NOT_PERFORMED。

| 格式 | 處理 | 判斷界限 |
|---|---|---|
| literal ;; 來源序列 | deterministic split | token原樣保留，不推論成分層級 |
| &amp; 等標準HTML entity | deterministic decode，僅draft | raw不變 |
| 單一成分名及括號同義詞 | 保留整個opaque token | 不展開括號，不翻譯或串接化學ID |
| 、，, ;； / 換行 | 記錄出現格式，留opaque | 不當成一律有效的第二層分隔符 |
| 複方／括號中的多項原料 | NEEDS_REVIEW | 子原料、載體、主劑关系未確定，不造化學成分 |
| %、mg、IU等字串 | NEEDS_REVIEW，原文保留 | 可能是原料濃度而非每份或每日劑量 |
| 不平衡括號、重複tokens、空成分 | NEEDS_REVIEW | 不補括號、不deduplicate、不補配方 |

原文實例（取自官方CSV，僅說明格式）：國產1005010282的「複方膽鈣化醇(維生素D3);;維生素D3」不能據此確定兩者是不同劑量或獨立組成；其銀杏果粉亦重複，保留兩個tokens。國產1005036779的「維生素D3 0.25% (...)」保留整項，不推算每日維生素D攝取量。所有成分風險逐列可見`09`，含各類分隔符、token數、詞形提示、風險flag及source_row_key。

維生素、礦物質、植物／草本、複方、賦形劑、膠囊殼以原文字形建立搜尋提示（lexical_cues），不是官方成分類型認定，不代表已完整分類；明膠或甘油出現不能證明屬膠囊殼。草本粉末、萃取物、載體混合及膠囊構材等可能混在同一來源序列，資料無法完整確定角色。

### 逐類風險列數（同列可有多個flag）

| risk flag | 國產 | 輸入 |
|---|---:|---:|
| COMPOUND_RELATIONSHIP_UNAVAILABLE | 360 | 55 |
| CONCENTRATION_OR_AMOUNT_CONTEXT_UNRESOLVED | 558 | 158 |
| MISSING_INGREDIENTS | 0 | 5 |
| MISSING_ISSUE_DATE | 1 | 0 |
| OPAQUE_COMPOUND_OR_NESTED_LIST | 538 | 787 |
| REPEATED_SOURCE_TOKENS | 852 | 1885 |
| SECONDARY_SEPARATOR_OPAQUE | 38 | 1052 |
| UNBALANCED_BRACKETS | 6 | 12 |

## 8. Dataset內重複與跨類別候選

本次字號重複組、完全重複原始列組均為0。國產29組、輸入198組「同中文品名、不同字號」另列在`07`，全數CONFIRMATION_REQUIRED，全部保留。最終duplicate groups採強重複（字號或完全原始列）口徑，不能將同名組算成已確認重複產品。

`08`共有103組候選，其中國產vs輸入85組、兩個新類別vs官方公開健康食品參考18組。分類CONFIRMATION_REQUIRED=98、FAMILY_ONLY=5，VERIFIED=0、LIKELY=0；沒有合併任何產品。候選是pair，不是唯一產品筆數。

匹配流程：在同品名（中／英）或同申請商的block內比較；只為比對建立NFKC、HTMLentity解碼、casefold及去空白的名稱值，原官方品名不變。同名本身只建立CONFIRMATION_REQUIRED。只有同中／英名＋同申請商＋完整成分文字相同才允許LIKELY，本次無符合；沒有官方交叉引用就不給VERIFIED。同申請商且中文3字元shingle Jaccard>=0.65，只標FAMILY_ONLY。這是研究用可重現候選規則，不是產品同一性的法律結論，也不能保證召回所有改名、英譯、品牌別名。未達候選條件的pair不輸出NO_MATCH巨型笛卡兒積；未輸出的pair不代表已證明不同產品。製造商、原產國、劑型缺失，不假裝有这些匹配證據。

## 9. 與 HEALTH_FOOD 的法律身分差別

新類別只表示查有官方查驗登記／核備資料。國產範圍公告源於食品安全衛生管理法第21條及2018-02-12衛授食字第1061303461號，進口核備資料集也是食品登記資訊；兩者都不能推出健康食品法定許可、小綠人、approved health claim、安全無虞或醫療功效。健康食品公開metadata另說明該名稱是法定名稱，須依健康食品制度審查及取得許可；本次沒有使用健康食品功效欄替任何新類別建立功效。另有獨立健康食品許可仍須產品個別證據比對。

## 10. 是否與既有565健康食品疑似重疊

僅為候選比對讀取TFDA InfoId19的公開健康食品CSV：本次也是565列。沒有讀取使用者production資料庫或其canonical IDs；同筆數不保證是相同565列。公開參考產品沒有混入01–04兩類產品成果，也不打包其原始CSV或功效欄。`08`只列候選名稱、字號、官方URL及證據。source inventory記錄其retrieved_at／hash，右側鍵以REFERENCE:TFDA:19:區分，不可匯入canonical Product。

找到18組疑似候選，例如「威瑪舒培薑黃養護錠」、「心之友達Q10紅麴膠囊」、「GSH 顧甘肽膠囊」等同名線索，以及「杏輝®活芯®升級版軟膠囊」與「活芯®升級版軟膠囊」家族線索。健康食品公開集缺少本次可直接一對一比較的完整配方欄，保健功效相關成分也不是完整成分，故沒有用它替代ingredients_raw匹配。所有此類候選仍需人工及production快照確認。

## 11. READY與NEEDS_REVIEW的匯入範圍

READY是「結構與日期有效，且未觸發本次保守的成分風險flag」，不是法律有效、可販售、功效核准或臨床交互作用資料已驗證。無英文品名或電話是來源缺值，留NULL；不因選用欄位缺值自動攔截。來源無製造商等欄位也不把全部列當錯誤。已過日期同樣不等於異常原始資料，不單憑此將每列標NEEDS_REVIEW。
本次國產READY=288、NEEDS_REVIEW=1417；輸入READY=4781、NEEDS_REVIEW=2626；總READY=5069、NEEDS_REVIEW=4043。
NEEDS_REVIEW主要是成分複方關係、重複token、第二層分隔符及濃度語意不明，另有國產1筆缺發證日期／輸入5筆缺成分。flag是本次parser品質判定，不是官方對產品的異常或違法認定。全部9112筆都能作可追溯的source staging記錄，並不代表全部能直接用於Interaction。READY列也只能自動匯入原始登記事實與來源token，化學ID、交互作用與功效仍未建立。

## 12. 自動品質Gate與可追溯性

`10`包含每類筆數、缺值、重複、解析失敗、日期比較、review原因及13項Gate。兩类均13項通過：raw與normalized筆數一致、所有原始欄位與值完整保留、raw row number／hash對應、主鍵唯一、category正確、無HEALTH_FOOD新產品分類、無approved-claim欄位、核心URL官方、原始bytes雜湊未變、可解析日期均有效、official_status全NULL、官方缺失欄位全NULL、UTF-8可roundtrip。CSV與JSON全量內容一致另有獨立結果。

| Gate計數 | 國產 | 輸入 |
|---|---:|---:|
| 證號空值 | 0 | 0 |
| 中文品名空值 | 0 | 0 |
| 成分空值 | 0 | 5 |
| 日期解析失敗 | 0 | 0 |
| 主鍵重複組 | 0 | 0 |

CSV／JSON crosscheck及原始檔hash只能證明本次公開下載一致，不能證明官方內部資料没有漏列。每個新產品可沿 source_row_key → raw_row_number/raw JSON/hash → 01或02原始檔 → source inventory的URL／retrieved_at追溯。匹配右側公開健康食品參考則以官方字號與URL追溯，未宣稱能追到production ID。

## 13. 完整性與更新問題

目前可確認最新官方可取得的兩個export全量各1705／7407筆；無法獨立從consumer查詢頁核对全部資料，不能宣稱涵蓋所有有效、撤銷、歷史或尚未同步登記。metadata提供每3月頻率，但沒有明確資源最後更新時間；目前較新的發證日期不等於完整同步截止日。舊國產鏡像頻率、上架及metadata日期和新英文頁不一致；已分來源記錄。製造商／原產國／劑型／官方status／規格／食用方式／劑量無法從這兩個export取得。原始成分缺少穩定chemical ID、角色與複方結構。名稱及申請商比對可能漏掉改名或登記轉移；無production快照無法驗證565個canonical產品的真正重疊。

## 14. 未來更新與 importer design

Ready for importer design：YES，意指兩類schema、主鍵、NULL規格、日期轉換與raw追溯已具體驗證，足以開始設計隔離的staging importer；不等於批准寫入production或自動合併健康食品。

建議至少依官方每3月頻率取新snapshot，也可較頻繁檢查但不聲稱官方每天更新；每次保存原始CSV、retrieved_at、hash、metadata頁與OpenAPI schema，並以JSON全量核對。在正式更新前比較表頭、字號集合、內容hash、缺值、日期及各reviewflag變化；新增或消失字號不能直接當新增上市或撤銷。保留版本／delta及來源記錄，禁止覆寫歷史raw。新增未知欄位或字號碰撞先quarantine；勿以缺資料推論country、manufacturer或法律status。以確切production565快照和官方個別許可查詢補做交叉確認，核對後仍以不同制度證號分別保存；Ingredient Interaction需另建官方／可驗證成分對照與經人工驗證的compound parser。

## 檔案清單與交付邊界

01–02官方原始CSV，03–04normalized，05來源，06mapping，07重複候選，08跨類候選，09逐列ingredient风险，10validation JSON及本報告為必備成果。另附11逐欄profile、SHA256清單及ZIP。ZIP附兩類JSON／食品OpenAPI及必要來源證據，但排除健康食品原始參考CSV，不輸出任何健康食品功效內容。所有檔案都是研究與匯入設計素材，不是production寫入。
