# Reviewed Ingredient Alias & Form Mapping Overlay — v1.0

研究日期：2026-10-04（Asia/Taipei）；各retrieved_at保存UTC ISO-8601。只做官方identity desk review與離線simulation；沒有修改應用程式、GitHub、Supabase、Neo4j、production或原始成分。reviewed表示來源查核，不代表持照化學／醫療專家的最終簽核。

## 1. 可用成果與範圍

Reviewed aliases：250；VERIFIED_OFFICIAL：167；SUPPORTED_AUTHORITATIVE：24；AMBIGUOUS：14；NEEDS_REVIEW：45。只有前兩類可參與正式mapping；後兩類entity_id為NULL，候選另存candidate_entity_id。

共review 61個forms，其中13個沿用Corpus既有F_* ID，48個為新增提案。鋅7種、銅4種。另有61條CONTRIBUTES_TO_NUTRIENT關係；没有SAME_AS。來源25個官方／權威來源，另1個INPUT_ONLY基準紀錄，不作新identity evidence。

未嘗試一次處理20,892筆NEEDS_REVIEW。國產全量統計得到3,068個distinct ingredient tokens，優先範圍590個；本版選高頻可查核名稱及60個高頻未解token記錄review結果。frequency檔覆蓋所有tokens，但NO_MATCH只表示本overlay沒有可用對應，不是「官方已證明無對應」。

## 2. 原始檔與coverage基準

既有Library成果中的TFDA raw CSV：國產1,705筆、輸入7,407筆，原始CSV保持不變並比對原下載SHA256。健康食品InfoId19本次官方下載565筆。合計9,677筆；沒有讀取或變更production，也沒有合併跨category產品。

原Product Ingredient Bridge dry-run逐筆輸出與parser未取得；使用者同意以既有官方檔重新計算。本報告before/after使用同一保守matcher，所以是可重現的替代基準；不得宣稱等於原dry-run所有結果或production現況。

| Category | 官方筆數 | Before正式mapping產品 | After正式mapping產品 | After比例 |
|---|---:|---:|---:|---:|
| DOMESTIC | 1705 | 0 | 1588 | 93.14% |
| IMPORTED | 7407 | 2609 | 4206 | 56.78% |
| HEALTH_FOOD | 565 | 0 | 6 | 1.06% |

Health-food after是保守下限：InfoId19只有「保健功效相關成分」，不是完整製造配方。採整欄exact match，不解析EPA/DHA的百分比、tabs、多行或指標複方。故只有少數健康食品能在此公開欄位匹配，不能與production的product_ingredients coverage直接比較，也不代表剩餘產品沒有成分。

## 3. 指定營養素coverage

計數是同category每個官方字號至多一次；同產品不同形式不重複計入同營養素。各營養素產品數不能相加當獨立產品總數。Form沿已查核CONTRIBUTES_TO_NUTRIENT計入nutrient coverage，但不建立Form SAME_AS Nutrient。

| Nutrient | 國產after | 輸入after | 健康食品公開欄位after | 總before | 總after |
|---|---:|---:|---:|---:|---:|
| Calcium | 489 | 1391 | 3 | 920 | 1883 |
| Iron | 300 | 384 | 1 | 253 | 685 |
| Magnesium | 265 | 732 | 1 | 0 | 998 |
| Zinc | 508 | 1033 | 0 | 0 | 1541 |
| Copper | 230 | 422 | 0 | 0 | 652 |
| Vitamin D | 317 | 1109 | 0 | 312 | 1426 |
| Vitamin K | 111 | 146 | 0 | 7 | 257 |
| Vitamin C | 922 | 1234 | 0 | 57 | 2156 |
| Omega-3 / Fish oil | 17 | 497 | 1 | 497 | 515 |
| B1 | 752 | 986 | 0 | 60 | 1738 |
| B2 | 895 | 1228 | 0 | 563 | 2123 |
| B3 | 759 | 999 | 0 | 139 | 1758 |
| B5 | 669 | 739 | 0 | 0 | 1408 |
| B6 | 923 | 946 | 0 | 64 | 1869 |
| B7 | 560 | 656 | 0 | 656 | 1216 |
| B9 / Folate | 736 | 979 | 0 | 979 | 1715 |
| B12 | 865 | 1393 | 0 | 101 | 2258 |

## 4. Identity evidence與官方版本

主證據為TFDA InfoId61「食品添加物使用範圍及限量暨規格標準資料集」。本次實取804筆，JSON每列同時有中文品名、英文品名、功能類別、使用規範及規格URL。選定列完整保存在identity_evidence_excerpts.json；source_locator使用1-based JSON row、中文品名與欄位，並有整份raw SHA256。資料集metadata更新2025-11-25，不等於每列法規修改日或生效日。

本次只取名稱與營養素歸屬，不把dataset的限量當新的cumulative intake法規閾值，不主張每個source是法規現行有效全文。找到較舊食品添加物手冊作導航後，優先回到實取官方dataset；未採用舊手冊的限量或過期規範。

NIH ODS 17份fact sheets確認salt/vitamer/mixture與parent nutrient，last_updated_date可取得則記錄。銅另使用ATSDR官方2024-10 Toxicological Profile的NCBI Bookshelf Table4-1及PubChem PUG API；PubChem實取CuO CID164827。未猜其他CAS、UNII、InChIKey、salt hydration或isomer。銅氧化物跨文件查核標SUPPORTED_AUTHORITATIVE，保留CuO而不把Cu2O視為同物。

魚油官方PDF第一頁提供Fish oil／魚油，已查看頁面圖像；EPA/DHA由TFDA檢驗方法修正對照表§1的中英名與縮寫確認。兩份原始PDF下載timeout，但官方web PDF成功讀取；來源明記OFFICIAL_WEB_VERIFIED_RAW_DOWNLOAD_FAILED並保存查核摘錄。修正對照表僅作identity證據，不當作現行方法法律版本結論。

## 5. 鋅／銅形式

| Entity | Official Chinese name | Canonical English | Parent | Identity qualification |
|---|---|---|---|---|
| F_ZN_OXIDE | 氧化鋅 | Zinc Oxide | N_ZN | SALT_FAMILY_UNSPECIFIED_HYDRATION |
| F_ZN_GLUCONATE | 葡萄糖酸鋅 | Zinc Gluconate | N_ZN | SALT_FAMILY_UNSPECIFIED_HYDRATION |
| F_ZN_SULFATE | 硫酸鋅 | Zinc Sulfate | N_ZN | SALT_FAMILY_UNSPECIFIED_HYDRATION |
| F_ZN_ACETATE | 醋酸鋅 | Zinc Acetate | N_ZN | SALT_FAMILY_UNSPECIFIED_HYDRATION |
| F_ZN_CHLORIDE | 氯化鋅 | Zinc Chloride | N_ZN | SALT_FAMILY_UNSPECIFIED_HYDRATION |
| F_ZN_BISGLYCINATE | 甘胺酸鋅 | Zinc Bisglycinate | N_ZN | SALT_FAMILY_UNSPECIFIED_HYDRATION |
| F_ZN_CITRATE | 檸檬酸鋅 | Zinc Citrate | N_ZN | SALT_FAMILY_UNSPECIFIED_HYDRATION |
| F_CU_SULFATE | 硫酸銅 | Copper Sulfate | N_CU | SALT_FAMILY_UNSPECIFIED_HYDRATION |
| F_CU_GLUCONATE | 葡萄糖酸銅 | Copper Gluconate | N_CU | SALT_FAMILY_UNSPECIFIED_HYDRATION |
| F_CU_OXIDE | 氧化銅 | Copper(II) oxide | N_CU | SALT_FAMILY_UNSPECIFIED_HYDRATION |
| F_CU_BISGLYCINATE | 甘胺酸銅 | Copper(II) bisglycinate | N_CU | SALT_FAMILY_UNSPECIFIED_HYDRATION |

Zinc/Copper gluconate、sulfate等在官方資料未限定水合型，保存declared salt family，不能擅自寫成無水化合物CAS；遇到pentahydrate、monohydrate、trihydrate等不同token不刪掉suffix來匹配。單水硫酸亞鐵另立F_FE_SULFATE_MONOHYDRATE，沒有合併進一般F_FE_SULFATE。

## 6. 必須保持的界線

- Fish oil是mixture；Omega-3是nutrient group；EPA、DHA是各自declared fatty acid forms。魚油含量不等於EPA+DHA含量，未建立定量conversion。
- 葉酸在TFDA添加物原料名稱明確配Folic Acid，因此此官方原料token→F_FOLIC_ACID。N_B9 Folate仍為群，沒有Folate SAME_AS Folic acid。一般食品描述、營養標示或自然食物中的「葉酸」不適用此原料token規則。
- Riboflavin與Biotin原Corpus的N_* nutrient名稱在產品原料語境有specific form證據，本overlay使用新的F_RIBOFLAVIN、F_BIOTIN。這是產品原料mapping context的優先對應，不改原N_* ID、營養素群搜尋或建立SAME_AS。
- Vitamin K2只確認menaquinone family；另存F_K2_FAMILY，未猜MK-4、MK-7或MK-9。
- Ascorbic acid與明確L-Ascorbic acid、Sodium ascorbate與Sodium L-Ascorbate分列；化學立體型未確認不自動合併。
- 本多酸鈣的此次營養用途關係是B5；抗壞血酸鈣是C；不因名稱含「鈣」就自動計入Calcium營養劑量。
- 硬脂酸鎂雖高頻，不能因「鎂」字建立與氧化鎂相同的營養supplement或臨床interaction mapping。本版保留AMBIGUOUS，不列為可用alias。

## 7. 中文alias的優先順序與頻率

| 原始token | 出現產品數 | 正式mapping | Entity |
|---|---:|---|---|
| 葉酸 | 736 | VERIFIED_OFFICIAL | F_FOLIC_ACID |
| 菸鹼醯胺 | 734 | VERIFIED_OFFICIAL | F_NICOTINAMIDE |
| 本多酸鈣 | 669 | VERIFIED_OFFICIAL | F_CALCIUM_PANTOTHENATE |
| 生物素 | 560 | VERIFIED_OFFICIAL | F_BIOTIN |
| 鹽酸吡哆辛(維生素B6) | 494 | VERIFIED_OFFICIAL | F_PYRIDOXINE_HCL |
| 核黃素(維生素B2) | 475 | VERIFIED_OFFICIAL | F_RIBOFLAVIN |
| 氰鈷胺明(維生素B12) | 419 | VERIFIED_OFFICIAL | F_CYANOCOBALAMIN |
| 抗壞血酸(維生素C) | 315 | VERIFIED_OFFICIAL | F_ASCORBIC_ACID |
| 葡萄糖酸鋅 | 290 | VERIFIED_OFFICIAL | F_ZN_GLUCONATE |
| 氧化鎂 | 245 | VERIFIED_OFFICIAL | F_MG_OXIDE |
| 碳酸鈣 | 244 | VERIFIED_OFFICIAL | F_CA_CARBONATE |
| 鹽酸吡哆辛 | 233 | VERIFIED_OFFICIAL | F_PYRIDOXINE_HCL |
| 氰鈷胺明 | 196 | VERIFIED_OFFICIAL | F_CYANOCOBALAMIN |
| 核黃素 | 191 | VERIFIED_OFFICIAL | F_RIBOFLAVIN |
| 膽鈣化醇(維生素D3) | 166 | VERIFIED_OFFICIAL | F_D3 |
| 維生素B6 | 158 | VERIFIED_OFFICIAL | N_B6 |
| L-抗壞血酸(維生素C) | 156 | VERIFIED_OFFICIAL | F_L_ASCORBIC_ACID |
| 葡萄糖酸銅 | 154 | VERIFIED_OFFICIAL | F_CU_GLUCONATE |
| 抗壞血酸鈉 | 153 | VERIFIED_OFFICIAL | F_SODIUM_ASCORBATE |
| 硝酸硫胺明(維生素B1) | 149 | VERIFIED_OFFICIAL | F_THIAMINE_MONONITRATE |
| 維生素B1 | 145 | VERIFIED_OFFICIAL | N_B1 |
| L-抗壞血酸鈉 | 141 | VERIFIED_OFFICIAL | F_SODIUM_L_ASCORBATE |
| 維生素D3 | 140 | VERIFIED_OFFICIAL | F_D3 |
| 硝酸硫胺明 | 136 | VERIFIED_OFFICIAL | F_THIAMINE_MONONITRATE |
| 氧化鋅 | 134 | VERIFIED_OFFICIAL | F_ZN_OXIDE |

產品數對同一raw row去重；token_occurrences則保留重複出現。保留raw ingredient文字，不修改產品記錄。source_id=SRC_DOMESTIC_CSV可追蹤官方字號與範例；identity_source_id另外指向雙語證據，不能因原料字串出現在official product dataset就自動VERIFIED。

## 8. Exact matcher、precedence與輸出契約

國產／輸入只在literal `;;`邊界分token。每token只做NFKC、trim、連續空白合一與casefold；不移除chemical suffix、括號、百分比、劑量、品牌、斜線、標點或鹽名，不decode HTML entities來擴大比對，也不使用embedding。原始token另存，所有正規化只是lookup key。

少數反向parenthetical英文格式（例如VITAMIN B12 (CYANOCOBALAMIN)）是逐項列舉review，採SUPPORTED_AUTHORITATIVE，依据同列中英文名稱和明確vitamin annotation。沒有通用「去括號」或「去salt」heuristic。`Copper (Ⅱ) Bisglycinate`的Ⅱ是chemical oxidation state，不刪除；不能錯誤產生Copper→bisglycinate的alias。

正式lookup流程：確認OFFICIAL_PRODUCT_INGREDIENT_TOKEN context→使用mapping_status允許的overlay exact alias→沒有overlay才依原Corpus READY exact alias。若overlay對同文字提供更精確F_*，按precedence=PRODUCT_FORM_CONTEXT_OVER_BASE_NUTRIENT_ALIAS使用F_*；仍保留N_*與F_*不同identity。importer必須顯式支援此context與precedence，不能兩個entity同時寫成SAME_AS或覆蓋原Corpus的通用alias。

新form ID是穩定英文化學形式ID；沿用既有13個F_* ID。alias_id用NFKC/casefold key、entity ID與context的SHA256前16位產生，與排序無關。NULL在CSV為literal `NULL`，JSON為null；UTF-8。source_locator、retrieved_at、overlay_version不可捨棄。

各檔用途：reviewed_ingredient_aliases.csv存採用/待審對應；reviewed_ingredient_forms.csv存61形式；reviewed_form_to_nutrient.csv存歸屬evidence；reviewed_alias_sources.csv存官方來源與INPUT_ONLY基準；domestic_alias_frequency.csv存全量排序。product_ingredient_overlay_simulation.csv為離線26k級逐token mapping明細，含官方字號、raw row、alias ID及source，供replay，不是production匯入命令。

## 9. Quality Gate與應保留的unknowns

22項檢查通過，包含新alias/source/entity FK、accepted keys不衝突、原始CSV SHA256一致、N/F分離、無Drug alias新增、沒有定量conversion或自動interaction inheritance、正確產品筆數、UTF-8，以及獨立從輸出CSV重新核算coverage。

| Check | Result |
|---|---|
| verified_alias_has_source | PASS |
| accepted_alias_has_identity_source | PASS |
| form_to_nutrient_has_evidence | PASS |
| relationship_only_contributes | PASS |
| no_form_same_as_nutrient | PASS |
| fish_omega_epa_dha_distinct | PASS |
| folate_folic_acid_distinct | PASS |
| accepted_overlay_alias_keys_unique | PASS |
| all_form_entities_exist_or_declared_in_overlay | PASS |
| all_parent_entities_exist | PASS |
| canonical_form_names_nonempty | PASS |
| no_drug_alias_added | PASS |
| bare_english_nutrient_not_redirected_to_compound | PASS |
| raw_product_ingredients_unmodified | PASS |
| official_authoritative_identity_hosts | PASS |
| no_amount_conversion_or_interaction_inheritance | PASS |
| ambiguous_needs_review_not_used | PASS |
| product_counts_match_requested | PASS |
| no_application_or_production_mutation | PASS |
| all_csv_utf8 | PASS |
| independent_export_fk_and_coverage_reconciliation | PASS |
| no_bare_nutrient_canonical_form | PASS |

Known gaps：原dry-run逐筆parser未取得；health-food公開欄位不是全配方；複方、載體、濃度、品牌、乾燥粉、鹽水合物、K2 homolog仍有unknown；化學識別碼只有限量查核。60個priority unresolved aliases與尚未review的scope tokens並不因本版存在而變成安全或無interaction。

## 10. Ingredient Bridge importer與未來更新

Ready for Ingredient Bridge importer：YES，限本overlay的191個accepted exact aliases與已查核歸屬契約，未知token保持待審。這不代表現有importer無需調整、不代表coverage已在production實現，也不代表新增forms可無條件沿用所有nutrient interaction warnings。

建議匯入時將原始Product→F_* mapping evidence與F_*→N_* affiliation分開保存；interaction查詢須另外確認來源的specific form、dose、route、population與判斷資格。Overlay只提供identity與membership evidence，不新增interaction facts、severity或健康功效。

更新流程：固定raw source snapshot及hash→重新統計literal tokens→按產品頻率review新名稱→官方雙語表＋ODS/PubChem核對→版本化overlay與sources→以相同matcher重算coverage→保留被取代alias的歷史與reason。新增水合型或isomer用新F_*，不能覆寫salt family造成歷史mapping變義。

## 11. 官方來源索引

| source_id | Authority | URL | Retrieval |
|---|---|---|---|
| TFDA_ADDITIVES_61 | TFDA | https://data.fda.gov.tw/data/opendata/export/61/json | SUCCESS |
| TFDA_HEALTH_19 | TFDA | https://data.fda.gov.tw/data/opendata/export/19/json | SUCCESS |
| ODS_Calcium | NIH Office of Dietary Supplements | https://ods.od.nih.gov/factsheets/Calcium-HealthProfessional/ | SUCCESS |
| ODS_Iron | NIH Office of Dietary Supplements | https://ods.od.nih.gov/factsheets/Iron-HealthProfessional/ | SUCCESS |
| ODS_Magnesium | NIH Office of Dietary Supplements | https://ods.od.nih.gov/factsheets/Magnesium-HealthProfessional/ | SUCCESS |
| ODS_Zinc | NIH Office of Dietary Supplements | https://ods.od.nih.gov/factsheets/Zinc-HealthProfessional/ | SUCCESS |
| ODS_Copper | NIH Office of Dietary Supplements | https://ods.od.nih.gov/factsheets/Copper-HealthProfessional/ | SUCCESS |
| ODS_VitaminD | NIH Office of Dietary Supplements | https://ods.od.nih.gov/factsheets/VitaminD-HealthProfessional/ | SUCCESS |
| ODS_VitaminK | NIH Office of Dietary Supplements | https://ods.od.nih.gov/factsheets/VitaminK-HealthProfessional/ | SUCCESS |
| ODS_VitaminC | NIH Office of Dietary Supplements | https://ods.od.nih.gov/factsheets/VitaminC-HealthProfessional/ | SUCCESS |
| ODS_Thiamin | NIH Office of Dietary Supplements | https://ods.od.nih.gov/factsheets/Thiamin-HealthProfessional/ | SUCCESS |
| ODS_Riboflavin | NIH Office of Dietary Supplements | https://ods.od.nih.gov/factsheets/Riboflavin-HealthProfessional/ | SUCCESS |
| ODS_Niacin | NIH Office of Dietary Supplements | https://ods.od.nih.gov/factsheets/Niacin-HealthProfessional/ | SUCCESS |
| ODS_PantothenicAcid | NIH Office of Dietary Supplements | https://ods.od.nih.gov/factsheets/PantothenicAcid-HealthProfessional/ | SUCCESS |
| ODS_VitaminB6 | NIH Office of Dietary Supplements | https://ods.od.nih.gov/factsheets/VitaminB6-HealthProfessional/ | SUCCESS |
| ODS_Biotin | NIH Office of Dietary Supplements | https://ods.od.nih.gov/factsheets/Biotin-HealthProfessional/ | SUCCESS |
| ODS_Folate | NIH Office of Dietary Supplements | https://ods.od.nih.gov/factsheets/Folate-HealthProfessional/ | SUCCESS |
| ODS_VitaminB12 | NIH Office of Dietary Supplements | https://ods.od.nih.gov/factsheets/VitaminB12-HealthProfessional/ | SUCCESS |
| ODS_Omega3FattyAcids | NIH Office of Dietary Supplements | https://ods.od.nih.gov/factsheets/Omega3FattyAcids-HealthProfessional/ | SUCCESS |
| PC_COPPER_OXIDE | NCBI PubChem | https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/cupric%20oxide/property/IUPACName,MolecularFormula,InChIKey/JSON | SUCCESS |
| TFDA_EPA_DHA | TFDA | https://www.fda.gov.tw/tc/includes/GetFile.ashx?id=f637889177787370640 | OFFICIAL_WEB_VERIFIED_RAW_DOWNLOAD_FAILED |
| NCBI_COPPER_TABLE | ATSDR / NLM NCBI Bookshelf | https://www.ncbi.nlm.nih.gov/books/NBK610366/table/ch4.tab1/?report=objectonly | SUCCESS |
| TFDA_FISH_BILINGUAL | TFDA | https://www.fda.gov.tw/tc/includes/GetFile.ashx?cid=47306&id=f638604343249800284&type=2 | OFFICIAL_WEB_VERIFIED_RAW_DOWNLOAD_FAILED |
| SRC_DOMESTIC_CSV | 衛生福利部食品藥物管理署 | https://data.fda.gov.tw/data/opendata/export/24/csv | LIBRARY_MATERIALIZED_AND_HASH_VERIFIED |
| SRC_IMPORTED_CSV | 衛生福利部食品藥物管理署 | https://data.fda.gov.tw/data/opendata/export/23/csv | LIBRARY_MATERIALIZED_AND_HASH_VERIFIED |