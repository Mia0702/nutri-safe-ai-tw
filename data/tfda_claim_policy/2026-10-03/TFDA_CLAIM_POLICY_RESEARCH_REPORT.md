# 台灣 TFDA／MOHW 食品與健康食品宣稱法規研究報告

研究基準日：2026-10-03。檢索日期及時間以各筆 `retrieved_at`（UTC）為準。本研究只研究、下載、整理及結構化官方資料，未修改應用程式、GitHub、Supabase、Neo4j 或任何 production database。

## 1. 找到的官方法規、公告與附件

本次結構化來源共 71 筆，包括法律、法規命令、公告及附件、功效評估方法、官方問答、行政指導、官方案例與開放資料 metadata。主要證據如下；每份文件的原始 URL、下載附件、日期、版本及機關見 `tfda_claim_policy_sources.csv`，條文或附件位置見 rules／examples／documents。

| 來源 | 文件及用途 |
|---|---|
| SRC_FOOD_ACT | 食品安全衛生管理法：一般食品標示、宣傳、廣告與醫療效能之母法；第 22、28、44、45 條等原文保留於 RAG。 |
| SRC_HEALTH_ACT、SRC_HEALTH_RULES | 健康食品管理法及施行細則：法律定義、功效範圍、許可、標示、宣稱及罰則。 |
| SRC_CLAIM_REG | 食品及相關產品標示宣傳廣告涉及不實誇張易生誤解或醫療效能認定準則：整體判斷、不實等類型與醫療效能類型。 |
| SRC_CLAIM_ANNEX1、SRC_CLAIM_ANNEX2 | 現行附件一一般詞句及附件二營養素／特定成分功能詞句，含三項附註。 |
| SRC_GUIDANCE | 2026-05 食品廣告合規輔導指引第 1 版：完整廣告脈絡、隱含功效、例示及理由；屬行政指導。 |
| SRC_HEALTH_CATALOG、SRC_FUNC_* | 現行 14 項保健功效及 14 份現行方法；另保存四份近期被取代的方法。 |
| SRC_PERMIT | 健康食品申請許可辦法：申請途徑、產品及文件審查，非一般食品功效准許清單。 |
| SRC_HEALTH_LABELS、SRC_HEALTH_LABELS_ATT4 | 健康食品應加標示事項及問答：功效／成分／指標、動物試驗限定、警語及產品別條件。 |
| SRC_HEALTH_LOGO、SRC_HEALTH_DISPLAY_PDF | 健康食品標準圖樣使用規範及包裝功效呈現原則。 |
| SRC_HEALTH_FISH_PDF、SRC_HEALTH_RED_PDF | 魚油、紅麴健康食品規格標準；規格符合仍須個別產品取得許可。 |
| SRC_HEALTH_QA、SRC_HEALTH_PROPOSAL_PDF | 最新查驗登記問答及功效評估方法提案程序。 |
| SRC_HEALTH_AMOUNT_REPEAL | 2024-01-01 生效的舊成分含量標示規定廢止公告。 |
| SRC_NUTRITION_CURRENT、SRC_NUTRITION_CURRENT_ATT4、SRC_NUTRITION_CURRENT_ATT3 | 2024 營養宣稱公告、表七勘誤及問答；部分條款於 2026-01-01 生效。 |
| SRC_VITAMIN_CURRENT、SRC_VITAMIN_CURRENT_ATT3 | 維生素礦物質錠狀膠囊食品營養標示及功能詞句門檻、警語。 |
| SRC_NUTRITION_LABEL | 2022 營養標示部分修正，2024-07-01 生效；已下載官方平台提供之現行中文版全文。 |
| SRC_PHYSIO_NUTRIENTS、SRC_PHYSIO_COMPONENTS | 新增／修訂營養素及其他特定成分功能詞句之建議程序。 |
| SRC_INFANT_MOJ、SRC_INFANT_PRINCIPLES、SRC_INFANT_QA | 嬰兒配方食品廣告禁止與例外、網路資訊條件及促銷限制。 |
| SRC_CHILDREN_AD | 不適合兒童長期食用食品之分類、特定時段及促銷限制。 |
| SRC_SPECIAL_LABEL | 特定疾病配方食品法定標示，避免將合法必須標示的疾病資訊誤作廣告禁詞。 |
| SRC_AD_PENALTIES | 食安法第四十五條廣告處理原則：處分評價條件及裁量因素。 |
| SRC_CLAIM_QA、SRC_CLAIM_NAME_QA、SRC_SUGAR_QA | 官方違規類型例示、健康品名及不額外添加砂糖之條件問答。 |
| SRC_LAW_STANDARD、SRC_FOOD_RULES_VERIFIED、SRC_PHARMA_ACT | 生效日期計算、食品標示細則及非藥物醫療效能邊界。 |
| SRC_FAIRTRADE_ACT、SRC_CONSUMER_ACT | 公平交易法第 21 條及消費者保護法第 22 條之一般廣告補充規定。 |
| SRC_REPLACEMENT_CASE、SRC_REPLACEMENT_NEWS | 官方查處案例及 2026-10-01 食品不能取代正規醫療說明；不是新增法律。 |
| SRC_LEGACY_HEALTH_WORDING | 政府機關提供的 2022 年 TFDA 廣告彙編附錄八；舊例與現行品名規定衝突，標 NEEDS_REVIEW。 |
| SRC_DATASET_* | 五個政府資料開放平台資料集 metadata；用途與欄位另列 inventory。 |

官方主法入口：[食品安全衛生管理法](https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=L0040001)、[健康食品管理法](https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=L0040012)、[認定準則](https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=L0040140)。其他來源均以 sources 表內原始政府網址為準。

## 2. 文件管制層級與關係

母法規定義務、禁止與授權；認定準則依母法提供判斷類型及例外。公告附件規範特定詞句、營養門檻、食品類別、成分歸屬或必須加標示內容。功效評估方法規定如何證明受試產品的特定效果，不能直接作一般食品的可用宣稱。官方 QA、行政指導及新聞提供解釋、例示或案例脈絡，不能取代法律、現行附件或個別許可。

同一公告的網頁、PDF、問答維護頁與官方鏡像不重複建立同一法律規則。附件及勘誤各有獨立 source_id，跨來源例句仍連到所屬 rule_id。中央法規標準法第 13／14 條用於區分發布日起施行與指定日期施行；第三日計算已在 version_note 說明。

## 3. 現行與歷史版本

71 筆來源中，61 筆 CURRENT、9 筆 SUPERSEDED、1 筆 NEEDS_REVIEW。這是 canonical 來源筆數，包含官方 QA、新聞及資料集，不等於有 61 部現行法律。CURRENT 表示目前官方提供／所採版本；不代表每一條文均已生效、每一附註均無時效問題，亦不代表所有產品都可套用。

SUPERSEDED 包括 2019、2020、2021 認定準則歷史全文、2015 營養宣稱公告、2021 營養標示全文，及舊骨質、血脂、血糖、胃腸方法。歷史全文的未修改條文不能因此認為全部失效；預設檢索使用現行整編文字。

近期方法過渡條款須同時檢查「試驗開始在新版生效前」及「申請在公告指定期間內」。骨質 2025-04-10、胃腸 2026-01-20、血糖與血脂 2026-08-20 新版，均不能以舊方法仍可有限過渡而將舊來源一般標為 CURRENT。其他較早修正方法的過渡期不得無期限延長。

日期欄記錄該版本之公告／公布、施行及修正日期。網頁維護日、dataset 更新日及 QA 平台 metadata 不冒充法律日期；無法確認或不適用之日期留空並附註。部分修正的 source effective_date 不取代每條 rule 的適用日期。嬰兒配方「開罐價」禁令之施行日期仍未定，不能因條文已列入現行全文即判為已生效。

## 4. 可使用宣稱的制度

一般食品先適用認定準則第 3 條的整體表現要求，再核對第 4 條、附件及其他特別規定。附件一保留 27 個通常可使用詞句／類似詞句；附件二保留 22 組營養素／特定成分及其生理功能詞句。附件二的含量條件、鉻每日最低攝取量 6 微克及明確歸屬營養素／成分三項附註均保存。詞句相似不等於全部脈絡都合法。

營養宣稱須按食品型態、每 100 公克／100 毫升／100 大卡或每日最低攝取量等基準、營養素分類及表格門檻核對。維礦錠膠囊還須確認營養添加劑來源、族群每日參考值及每日最低攝取量至少 15% 等條件。營養宣稱與營養標示含量、沖泡／復水基準及零值判定需一致；一般標示誤差不能直接擴張無／不含宣稱門檻。

表七食品限制採 2024-03-20 勘誤後文字，尤其「額外使用營養添加劑之零食類食品」及無糖碳酸飲料、特定無糖糖果等括號例外，不把全部零食視為一類。官方不額外添加砂糖 QA 的許可句子只及於如實說明來源，不把題目中的「含量低於 0.5%」併入可用例句，也不推定等於無糖。

## 5. 禁止及限制宣稱的制度

認定準則第 4 條的類型包括與事實不符、無證據或證據不足、涉及維持或改變器官／組織／生理或外觀功能等；各類型及條件分別保存，不能因語意相近自行合併。第 5 條保存疾病、症候群或症狀之預防、改善、減輕、診斷、治療，減輕或降低導致疾病有關的體內成分，以及中藥材效能三個判斷類型。

這些是意涵及對象條件，不是單詞禁令。核准健康食品、特定營養缺乏疾病表達制度、附件二成分功能詞句與特定疾病配方的法定標示仍須辨別。不得只因「改善」「傷口」「降低」等詞出現即判違規。官方例示保存疾病／成分／產品類別、原因及限定語；2026 指引表格整頁保存，避免拆出負面關鍵字。

品名、圖片、見證、研究、專利、同音錯字、暗示及前後文也可能使訊息產生功效意涵。食品取代手術／正規醫療的官方例句及說明已納入，但新聞沒有提供完整裁處書、救濟結果或新的通用禁詞。嬰兒配方、兒童不適合長期食用食品另有媒介、資訊、時間及促銷條件。

## 6. 產品核准功效與一般食品宣稱的差別

健康食品是依法取得許可、具有經主管機關認定保健功效的法定類別；「保健食品」市場名稱不能證明許可。健康食品管理法第 6／7／13／14 條及施行細則原文已保留。許可與功效均是產品特定：產品配方、試驗、攝取量、族群、動物或人體證據及核定文字必須相符，不能只憑成分名稱、同類商品許可證或某項方法存在就宣稱功效。

依法核准的健康食品也只能在核准範圍內宣稱，不能藉核准保健功效擴張為醫療效能。健康食品標章須有許可並符合官方圖樣、比例、顏色及尺寸條件；標章不表示所有功能均獲核准。規格標準途徑的魚油／紅麴產品仍需許可，且有專屬警語與標示。健康食品應加標示公告的動物試驗、非藥品、患病就醫、用量、糖量、魚油及紅麴交互作用警語條件分別保留。

現行功效項目為：骨質保健、調節血脂、胃腸功能改善、免疫調節、牙齒保健、護肝、調節血糖、延緩衰老、抗疲勞、輔助調節血鐵、輔助調節血壓、不易形成體脂肪、輔助調整過敏體質、膝關節保健。目錄公告另有其他使用類似詞句之功效條款，不自行新增第 15 項。完整方法、官方名稱、日期及歷史來源見 function catalog。

catalog 的 effective_date 是 2025-07-03 功效項目公告日期；evaluation_effective_date 是該方法的生效日期。official_definition 有原文時採方法「適用範圍」，並附註其性質；沒有獨立定義則留空，不以 AI 補寫功效定義。

## 7. 官方違規廣告 open data

找到 5 個官方資料集及 1 個官方查詢入口，只建立來源 inventory，沒有大量下載案例。

| 資料集 | 官方 metadata | 更新頻率與資料性質 |
|---|---|---|
| TFDA 違規食品廣告資料集 | https://data.gov.tw/dataset/6949 | 不定期；CSV、JSON、XML。產品、業者、負責人、處分機關、日期、法條、刊播日期及媒體等。只收函知 TFDA 範圍，不能推定涵蓋全部裁罰。 |
| 臺中市食品違規廣告處辦統計表 | https://data.gov.tw/dataset/83771 | 不定期；XML、JSON、CSV，公布 API 文件入口。產品類別、查處情形、媒體、網站、查獲機關及違規類型等。 |
| 桃園市食品廣告違規案件調查結果每月統計 | https://data.gov.tw/dataset/168539 | 每月 CSV；案由、商號、產品、來源、調查結果。最新 metadata 更新 2026-10-01，資源月份不保證連續。 |
| 臺北市食品、藥品、醫材及化粧品違規廣告處罰案件統計表 | https://data.gov.tw/dataset/133356 | 不定期；metadata 所列 CSV 是檔案索引欄位，不能當逐案裁罰表，且需分離產品類別。 |
| 臺南市食品違規廣告裁罰統計表 | https://data.gov.tw/dataset/161400 | 每年；JSON、CSV，主要是年度總件數及總金額，不是逐案文案。 |
| 官方違規廣告查詢系統 | https://pmds.fda.gov.tw/illegalad/ | 有官方連結；未確認公開 API、批次下載方式或其授權，列 NEEDS_REVIEW。 |

五個 dataset metadata 均列政府資料開放授權條款第 1 版。inventory 保存欄位、格式、資源主機或 URL、API 文件、metadata 更新與擷取時間；metadata 欄位不等於已實際下載確認 CSV header。沒有確認違規原文完整欄位、確定救濟結果、全量性或所有資源端點均可存取。

## 8. 可採 deterministic rule 的內容

在必要產品及日期事實已取得時，可檢核來源／版本選擇、營養素及食品分類、數值基準和門檻、明定族群／型態、必須標示項目、明確附加警語、證號及已核准功效資料、圖樣尺寸、指定日期及過渡條款的兩個日期。這是條件比對，不代表文字相符就免除整體廣告判斷。

法規附件表格、指標及量化條件保留在原始 PDF 與 RAG，沒有把全部科學試驗細節編成可執行程式。rules 是可追溯的判斷單位，不是已部署的裁罰引擎。任一所需事實未知，不能以預設值代入。

## 9. 需要 evidence 或 semantic comparison 的內容

事實真實性、證據是否足夠、研究受試品是否等於販售品、配方與劑量、試驗人群、結果指標、外推範圍及絕對比較宣稱，必須審查證據。廣告整體意涵、疾病暗示、器官功能、同音變形、見證、專利或研究引用、商標與品名排列、是否使消費者認為可取代醫療，必須取得完整文字及視覺／音訊後比較。

核准功效與文案的比對還需要個別許可內容。不能把某評估方法列出的句式、動物試驗結果或一般營養素詞句，轉為另一產品的准許功效。

## 10. NEEDS_REVIEW 與不能自動判定之處

共有 29 條 rule 的 review_status 為 NEEDS_REVIEW，其中 11 條 policy_effect 亦為 NEEDS_REVIEW；兩欄意義不同。前者是資料／適用判斷需複核，後者表示本列不下確定法律效果結論。另有 3 筆 function 需複核、1 筆來源版本未確認、30 個 OCR RAG 區段未逐字校對。

具體待辦包括：2026 血糖公告為衛授食字第 1151301749 號，但方法 PDF 頁首列 1151301762 號，保留兩者；免疫調節方法未取得明確施行文字；延緩衰老 30 頁全文 OCR 尚未完整校對，但第 30 頁宣稱及統計段落已人工對照原圖；2022 彙編附錄八的「健康茶」等舊例與 2022-07-01 生效品名規定衝突，不能直接准許；嬰兒配方開罐價條款施行日未定；行政指導、裁量及官方新聞不能直接轉為自動裁處。

26 筆來源日期檢核欄為 NEEDS_REVIEW，包含未明列及 QA／行政指導等沒有法定施行日的文件，不是 26 筆法律已知失效。日期空白及例外說明必須保留。2026 指引參考文獻的日期／URL 有錯置，採原公告及官方原法，不用參考文獻取代來源驗證。

## 11. 完整性限制與驗收

已涵蓋核心兩法、認定準則現行附件、全部 14 項目及方法、主要營養宣稱門檻、健康食品標章／警語、特殊食品廣告規則及官方違規資料來源。未逐一蒐集所有歷史附件、所有地方機關函釋、全部官方 FAQ、全部品牌產品許可、完整個案處分書、法院救濟或所有地方開放資料。科學方法的全文保留，不代表每個試驗細節均已成為一條 rule。

全國法規資料庫顯示整編資料截止 2026-09-24，資料庫整編可能落後公布日；本次另核對 TFDA 原公告及近期訊息至 2026-10-01。研究基準日的未被發現公告仍是完整性風險，不能聲稱保證任何機關未有更新。

驗收已檢查：所有 source_id 存在；每條規則有官方來源及對應原文；每筆例句有規則／來源且原文可查；每項功效有目錄及方法來源；日期格式與政策效果 enum；來源均政府官方；CURRENT／SUPERSEDED／NEEDS_REVIEW 清楚；同來源、位置、原文、條件完全相同的規則無重複；CSV 為 UTF-8（含 BOM）、JSONL 為 UTF-8。官方法規與例句原文未以摘要取代，PDF 換行、頁碼及表格閱讀須回原檔確認。校驗詳細結果見 validation JSON。

## 12. 後續 corpus 更新建議

定期檢查 MOJ 全文／沿革、TFDA 法規公告與健康食品方法目录、食品標示諮詢服務平台及 data.gov.tw metadata；發布後至生效前仍需保留不同時間狀態。官方平台改用 foodlabel.fda.gov.tw，後續優先使用該政府網址。

更新時先下載原文並留 SHA-256、下載時間及父頁附件關係，逐條對照版本與勘誤，建立新的來源版次並將舊版本標 SUPERSEDED；過渡適用條件保留獨立欄位，不能整批改成現行。行政指導及 QA 變更需回核法規。掃描 PDF 先 OCR 再人工校對涉及數字、否定、例外與准許句式的段落。

正式自動判斷前補入產品特定許可資料與完整文案證據，讓未知條件及來源衝突進入 NEEDS_REVIEW。若日後下載違規案例，先確認每項資源實際欄位、授權、資料範圍與 API 限制，再分離已裁處、疑似、撤銷或救濟狀態，避免以違規商品名稱或單句當訓練標籤。

本次交付檔案為 sources、rules、examples、health function catalog、documents JSONL、此報告，另附 open-data inventory、validation 與官方原始資料封裝。Documents 數量 976 是條文／PDF 頁面／metadata 等 RAG 區段，不是 976 份獨立法規。


## 附錄 A：現行評估方法逐項對照

| 正式項目 | 評估方法名稱 | 方法生效日 | 方法來源及官方 URL | 複核狀態 |
|---|---|---|---|---|
| 骨質保健 | 健康食品之骨質保健功效評估方法 | 2025-04-10 | [SRC_FUNC_48774](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=48774) | SOURCE_VERIFIED_CONTEXT_REQUIRED |
| 調節血脂 | 健康食品之調節血脂保健功效評估方法 | 2026-08-20 | [SRC_FUNC_51765](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=51765) | SOURCE_VERIFIED_CONTEXT_REQUIRED |
| 胃腸功能改善 | 健康食品之胃腸功能改善保健功效評估方法 | 2026-01-20 | [SRC_FUNC_50276](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=50276) | SOURCE_VERIFIED_CONTEXT_REQUIRED |
| 免疫調節 | 健康食品之免疫調節功能評估方法 | 未確認 | [SRC_FUNC_20313](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=20313) | NEEDS_REVIEW |
| 牙齒保健 | 健康食品之牙齒保健功效評估方法 | 2023-03-08 | [SRC_FUNC_42702](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=42702) | SOURCE_VERIFIED_CONTEXT_REQUIRED |
| 護肝 | 健康食品之護肝保健功效評估方法 | 2016-04-25 | [SRC_FUNC_20319](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=20319) | SOURCE_VERIFIED_CONTEXT_REQUIRED |
| 調節血糖 | 健康食品之調節血糖保健功效評估方法 | 2026-08-20 | [SRC_FUNC_51766](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=51766) | NEEDS_REVIEW |
| 延緩衰老 | 健康食品之延緩衰老保健功效評估方法 | 2015-07-09 | [SRC_FUNC_20324](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=20324) | NEEDS_REVIEW |
| 抗疲勞 | 健康食品之抗疲勞保健功效評估方法 | 2021-04-15 | [SRC_FUNC_20318](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=20318) | SOURCE_VERIFIED_CONTEXT_REQUIRED |
| 輔助調節血鐵 | 健康食品之輔助調節血鐵保健功效評估方法 | 2024-02-23 | [SRC_FUNC_46200](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=46200) | SOURCE_VERIFIED_CONTEXT_REQUIRED |
| 輔助調節血壓 | 健康食品之輔助調節血壓保健功效評估方法 | 2022-02-23 | [SRC_FUNC_40068](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=40068) | SOURCE_VERIFIED_CONTEXT_REQUIRED |
| 不易形成體脂肪 | 健康食品之不易形成體脂肪保健功效評估方法 | 2013-10-23 | [SRC_FUNC_20325](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=20325) | SOURCE_VERIFIED_CONTEXT_REQUIRED |
| 輔助調整過敏體質 | 健康食品之輔助調整過敏體質功能評估方法 | 2007-10-01 | [SRC_FUNC_20322](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=20322) | SOURCE_VERIFIED_CONTEXT_REQUIRED |
| 膝關節保健 | 健康食品之膝關節保健功效評估方法 | 2025-07-03 | [SRC_FUNC_49249](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=49249) | SOURCE_VERIFIED_CONTEXT_REQUIRED |

## 附錄 B：全部來源版本索引

附件及下載鏈見 sources CSV 與 raw manifest。日期空白維持未確認／不適用，不補推測。

| source_id | 文件 | 類型 | 版本 | 公布或上架日 | 生效日 | 官方 URL |
|---|---|---|---|---|---|---|
| SRC_FOOD_ACT | 食品安全衛生管理法 | 法律 | CURRENT | 2019-06-12 | 2019-06-14 | [原始來源](https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=L0040001) |
| SRC_HEALTH_ACT | 健康食品管理法 | 法律 | CURRENT | 2020-01-15 | 2020-01-17 | [原始來源](https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=L0040012) |
| SRC_HEALTH_RULES | 健康食品管理法施行細則 | 法規命令 | CURRENT | 2019-01-17 | 2019-01-19 | [原始來源](https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=L0040013) |
| SRC_CLAIM_REG | 食品及相關產品標示宣傳廣告涉及不實誇張易生誤解或醫療效能認定準則 | 法規命令 | CURRENT | 2024-05-08 | 2024-05-10 | [原始來源](https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=L0040140) |
| SRC_CLAIM_ANNEX1 | 認定準則第四條附件一：通常得使用之詞句或類似之詞句 | 法規附件 | CURRENT | 2019-06-12 | 2019-06-14 | [原始來源](https://law.moj.gov.tw/LawClass/LawGetFile.ashx?FileId=0000250007&lan=C) |
| SRC_CLAIM_ANNEX2 | 認定準則第四條附件二：營養素或特定成分得敘述之生理功能詞句或類似詞句 | 法規附件 | CURRENT | 2024-05-08 | 2024-05-10 | [原始來源](https://law.moj.gov.tw/LawClass/LawGetFile.ashx?FileId=0000368191&lan=C) |
| SRC_CLAIM_HIST_2019 | 認定準則歷史全文 2019-06-12 | 歷史法規 | SUPERSEDED | 2019-06-12 | 2019-06-14 | [原始來源](https://law.moj.gov.tw/LawClass/LawOldVer.aspx?pcode=L0040140&lnndate=20190612&lser=001) |
| SRC_CLAIM_HIST_2020 | 認定準則歷史全文 2020-08-04 | 歷史法規 | SUPERSEDED | 2020-08-04 | 2022-07-01 | [原始來源](https://law.moj.gov.tw/LawClass/LawOldVer.aspx?pcode=L0040140&lnndate=20200804&lser=001) |
| SRC_CLAIM_HIST_2021 | 認定準則歷史全文 2021-05-24 | 歷史法規 | SUPERSEDED | 2021-05-24 | 2021-05-26 | [原始來源](https://law.moj.gov.tw/LawClass/LawOldVer.aspx?pcode=L0040140&lnndate=20210524&lser=001) |
| SRC_GUIDANCE | 食品廣告合規輔導指引（第1版） | 行政指導 | CURRENT | 2026-05-19 | 逐條／未確認／不適用 | [原始來源](https://www.fda.gov.tw/tc/includes/GetFile.ashx?id=f639147208057524649) |
| SRC_HEALTH_CATALOG | 健康食品管理法所稱保健功效之項目 | 公告 | CURRENT | 2025-07-03 | 2025-07-03 | [原始來源](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1758&id=49250) |
| SRC_PERMIT | 健康食品申請許可辦法 | 法規命令 | CURRENT | 2024-05-17 | 2024-05-19 | [原始來源](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1758&id=46678) |
| SRC_HEALTH_LABELS | 健康食品應加標示事項 | 公告 | CURRENT | 2022-11-08 | 2024-01-01 | [原始來源](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1758&id=41899) |
| SRC_HEALTH_LABELS_ATT4 | 健康食品應加標示事項修正規定問答集 | 官方QA | CURRENT | 未確認／不適用 | 逐條／未確認／不適用 | [原始來源](https://www.fda.gov.tw/tc/includes/GetFile.ashx?id=f638515606305998604&type=1) |
| SRC_HEALTH_LOGO | 健康食品標準圖樣使用規範 | 使用規範 | CURRENT | 1999-08-03 | 逐條／未確認／不適用 | [原始來源](https://www.fda.gov.tw/tc/includes/GetFile.ashx?id=f638048205824518979&type=1) |
| SRC_HEALTH_DISPLAY_PDF | 健康食品之保健功效於包裝標示呈現原則 | 行政指導 | CURRENT | 2015-06-10 | 逐條／未確認／不適用 | [原始來源](https://www.fda.gov.tw/tc/includes/GetFile.ashx?id=f638036917586137835&type=1) |
| SRC_HEALTH_PROPOSAL_PDF | 健康食品保健功效評估方法提案申請作業指引 | 申請程序 | CURRENT | 2017-04-17 | 逐條／未確認／不適用 | [原始來源](https://www.fda.gov.tw/tc/includes/GetFile.ashx?id=f638048910710467987&type=1) |
| SRC_HEALTH_FISH_PDF | 魚油健康食品規格標準 | 法規命令 | CURRENT | 2007-12-24 | 2007-12-31 | [原始來源](https://www.fda.gov.tw/tc/includes/GetFile.ashx?id=f638047310590876999&type=1) |
| SRC_HEALTH_RED_PDF | 紅麴健康食品規格標準 | 法規命令 | CURRENT | 2007-12-24 | 2007-12-31 | [原始來源](https://www.fda.gov.tw/tc/includes/GetFile.ashx?id=f638047301874651562&type=1) |
| SRC_HEALTH_QA | 健康食品查驗登記常見問答集（115.05.22） | 官方QA | CURRENT | 未確認／不適用 | 逐條／未確認／不適用 | [原始來源](https://www.fda.gov.tw/tc/includes/GetFile.ashx?id=f639258641830680019&iid=1762&type=3) |
| SRC_HEALTH_AMOUNT_REPEAL | 廢止健康食品應於產品容器或外包裝明顯標示保健功效之相關成分含量（廢止公告） | 廢止公告 | CURRENT | 2022-11-08 | 2024-01-01 | [原始來源](https://www.fda.gov.tw/tc/newsContent.aspx?cid=3&id=28312) |
| SRC_NUTRITION_CURRENT | 包裝食品營養宣稱應遵行事項（含2024表七勘誤） | 公告 | CURRENT | 2024-02-19 | 2024-02-19 | [原始來源](https://www.fda.gov.tw/tc/newsContent.aspx?cid=3&id=30378) |
| SRC_NUTRITION_CURRENT_ATT4 | 包裝食品營養宣稱應遵行事項表七勘誤函 | 勘誤 | CURRENT | 2024-03-20 | 逐條／未確認／不適用 | [原始來源](https://www.fda.gov.tw/tc/includes/GetFile.ashx?id=f638466352069420586) |
| SRC_NUTRITION_CURRENT_ATT3 | 包裝食品營養宣稱應遵行事項問答集（113.9.26） | 官方QA | CURRENT | 未確認／不適用 | 逐條／未確認／不適用 | [原始來源](https://www.fda.gov.tw/tc/includes/GetFile.ashx?id=f638629434885343306) |
| SRC_NUTRITION_OLD | 包裝食品營養宣稱應遵行事項（2015公告版本） | 歷史公告 | SUPERSEDED | 2015-03-03 | 2016-01-01 | [原始來源](https://www.fda.gov.tw/tc/newsContent.aspx?cid=3&id=12187) |
| SRC_VITAMIN_CURRENT | 包裝維生素礦物質類之錠狀膠囊狀食品營養標示應遵行事項 | 公告 | CURRENT | 2019-11-07 | 2019-11-07 | [原始來源](https://www.fda.gov.tw/tc/newsContent.aspx?cid=3&id=25734) |
| SRC_VITAMIN_CURRENT_ATT3 | 維生素礦物質類錠狀膠囊狀食品營養標示問答集 | 官方QA | CURRENT | 未確認／不適用 | 逐條／未確認／不適用 | [原始來源](https://www.fda.gov.tw/tc/includes/GetFile.ashx?id=f637090858541493422) |
| SRC_NUTRITION_LABEL | 包裝食品營養標示應遵行事項（2022修正及現行全文） | 公告 | CURRENT | 2022-06-23 | 2024-07-01 | [原始來源](https://www.fda.gov.tw/tc/newsContent.aspx?cid=3&id=28026) |
| SRC_NUTRITION_LABEL_BASE_PDF | 包裝食品營養標示應遵行事項（2021全文版本） | 歷史公告 | SUPERSEDED | 2021-04-27 | 2021-04-27 | [原始來源](https://www.fda.gov.tw/tc/includes/GetFile.ashx?id=f637550502868310912) |
| SRC_PHYSIO_NUTRIENTS | 申請增修訂一般營養素可敘述之生理功能例句建議程序 | 申請程序 | CURRENT | 2017-01-12 | 逐條／未確認／不適用 | [原始來源](https://www.fda.gov.tw/tc/siteContent.aspx?sid=10766) |
| SRC_PHYSIO_COMPONENTS | 非屬一般營養素之其他特定成分可使用生理功能詞句建議程序 | 申請程序 | CURRENT | 2020-01-13 | 逐條／未確認／不適用 | [原始來源](https://www.fda.gov.tw/tc/siteContent.aspx?sid=10805) |
| SRC_INFANT_MOJ | 嬰兒與較大嬰兒配方食品廣告及促銷管理辦法 | 法規命令 | CURRENT | 2014-12-29 | 逐條／未確認／不適用 | [原始來源](https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=L0040117) |
| SRC_INFANT_PRINCIPLES | 嬰兒與較大嬰兒配方食品廣告處理原則 | 行政規則 | CURRENT | 2023-02-14 | 2023-02-14 | [原始來源](https://www.fda.gov.tw/tc/includes/GetFile.ashx?id=f638120605964395093) |
| SRC_INFANT_QA | 嬰兒與較大嬰兒配方食品廣告處理原則問答集 | 官方QA | CURRENT | 未確認／不適用 | 逐條／未確認／不適用 | [原始來源](https://www.fda.gov.tw/tc/includes/GetFile.ashx?id=f638137122858996702) |
| SRC_CHILDREN_AD | 不適合兒童長期食用之食品廣告及促銷管理辦法 | 法規命令 | CURRENT | 2014-11-20 | 2016-01-01 | [原始來源](https://www.fda.gov.tw/TC/lawContent.aspx?cid=62&id=2909) |
| SRC_SPECIAL_LABEL | 特定疾病配方食品應加標示事項 | 公告 | CURRENT | 2020-02-06 | 2020-02-06 | [原始來源](https://www.fda.gov.tw/tc/newsContent.aspx?cid=3&id=25888) |
| SRC_AD_PENALTIES | 食品安全衛生管理法第四十五條規定廣告處理原則 | 行政規則 | CURRENT | 2024-01-31 | 2024-01-31 | [原始來源](https://www.fda.gov.tw/tc/includes/GetFile.ashx?id=f638466965041891744) |
| SRC_CLAIM_QA | 認定準則Q&A（108.08.09） | 官方QA | CURRENT | 2019-08-09 | 逐條／未確認／不適用 | [原始來源](https://www.fda.gov.tw/tc/includes/GetFile.ashx?id=f637013889905960746) |
| SRC_CLAIM_NAME_QA | 食品品名不得標示健康字樣規定Q&A | 官方QA | CURRENT | 未確認／不適用 | 逐條／未確認／不適用 | [原始來源](https://www.fda.gov.tw/tc/includes/GetFile.ashx?id=f637321542084126960) |
| SRC_LAW_STANDARD | 中央法規標準法 | 法律 | CURRENT | 2004-05-19 | 2004-05-21 | [原始來源](https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=A0030133) |
| SRC_FOOD_RULES_VERIFIED | 食品安全衛生管理法施行細則 | 法規命令 | CURRENT | 2017-07-13 | 2017-07-15 | [原始來源](https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=L0040003) |
| SRC_PHARMA_ACT | 藥事法（限非藥物醫療效能邊界） | 法律 | CURRENT | 2026-03-04 | 逐條／未確認／不適用 | [原始來源](https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=L0030001) |
| SRC_FUNC_48774 | 健康食品之骨質保健功效評估方法 | 保健功效評估方法 | CURRENT | 2025-04-10 | 2025-04-10 | [原始來源](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=48774) |
| SRC_FUNC_51765 | 健康食品之調節血脂保健功效評估方法 | 保健功效評估方法 | CURRENT | 2026-08-20 | 2026-08-20 | [原始來源](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=51765) |
| SRC_FUNC_50276 | 健康食品之胃腸功能改善保健功效評估方法 | 保健功效評估方法 | CURRENT | 2026-01-20 | 2026-01-20 | [原始來源](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=50276) |
| SRC_FUNC_20313 | 健康食品之免疫調節功能評估方法 | 保健功效評估方法 | CURRENT | 1999-08-02 | 逐條／未確認／不適用 | [原始來源](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=20313) |
| SRC_FUNC_42702 | 健康食品之牙齒保健功效評估方法 | 保健功效評估方法 | CURRENT | 2023-03-08 | 2023-03-08 | [原始來源](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=42702) |
| SRC_FUNC_20319 | 健康食品之護肝保健功效評估方法 | 保健功效評估方法 | CURRENT | 2016-04-25 | 2016-04-25 | [原始來源](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=20319) |
| SRC_FUNC_51766 | 健康食品之調節血糖保健功效評估方法 | 保健功效評估方法 | CURRENT | 2026-08-20 | 2026-08-20 | [原始來源](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=51766) |
| SRC_FUNC_20324 | 健康食品之延緩衰老保健功效評估方法 | 保健功效評估方法 | CURRENT | 2015-07-09 | 2015-07-09 | [原始來源](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=20324) |
| SRC_FUNC_20318 | 健康食品之抗疲勞保健功效評估方法 | 保健功效評估方法 | CURRENT | 2021-04-15 | 2021-04-15 | [原始來源](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=20318) |
| SRC_FUNC_46200 | 健康食品之輔助調節血鐵保健功效評估方法 | 保健功效評估方法 | CURRENT | 2024-02-23 | 2024-02-23 | [原始來源](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=46200) |
| SRC_FUNC_40068 | 健康食品之輔助調節血壓保健功效評估方法 | 保健功效評估方法 | CURRENT | 2022-02-23 | 2022-02-23 | [原始來源](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=40068) |
| SRC_FUNC_20325 | 健康食品之不易形成體脂肪保健功效評估方法 | 保健功效評估方法 | CURRENT | 2013-10-23 | 2013-10-23 | [原始來源](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=20325) |
| SRC_FUNC_20322 | 健康食品之輔助調整過敏體質功能評估方法 | 保健功效評估方法 | CURRENT | 2007-07-12 | 2007-10-01 | [原始來源](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=20322) |
| SRC_FUNC_49249 | 健康食品之膝關節保健功效評估方法 | 保健功效評估方法 | CURRENT | 2025-07-03 | 2025-07-03 | [原始來源](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=49249) |
| SRC_FUNC_20312 | 健康食品之骨質保健舊評估方法 | 歷史評估方法 | SUPERSEDED | 2013-02-05 | 逐條／未確認／不適用 | [原始來源](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=20312) |
| SRC_FUNC_20315 | 健康食品之調節血脂舊評估方法 | 歷史評估方法 | SUPERSEDED | 2007-07-18 | 逐條／未確認／不適用 | [原始來源](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=20315) |
| SRC_FUNC_20314 | 健康食品之調節血糖舊評估方法 | 歷史評估方法 | SUPERSEDED | 2007-07-18 | 逐條／未確認／不適用 | [原始來源](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=20314) |
| SRC_FUNC_20316 | 健康食品之胃腸功能改善舊評估方法 | 歷史評估方法 | SUPERSEDED | 2003-08-29 | 逐條／未確認／不適用 | [原始來源](https://www.fda.gov.tw/tc/siteListContent.aspx?sid=1760&id=20316) |
| SRC_REPLACEMENT_CASE | 新北市衛生局公開違規廣告查處案例（食品取代手術等） | 官方案例新聞 | CURRENT | 2025-03-11 | 逐條／未確認／不適用 | [原始來源](https://www.fda.gov.tw/TC/csmnewsContent.aspx?id=t623435&mid=267) |
| SRC_REPLACEMENT_NEWS | 食藥署呼籲食品非藥品，切勿相信誇大宣稱延誤就醫 | 官方說明新聞 | CURRENT | 2026-10-01 | 逐條／未確認／不適用 | [原始來源](https://www.fda.gov.tw/tc/newsContent.aspx?cid=4&id=t634723) |
| SRC_LEGACY_HEALTH_WORDING | TFDA食品、化粧品、醫療器材及藥品廣告法規彙編（2022年版）附錄八 | 官方歷史彙編 | NEEDS_REVIEW | 未確認／不適用 | 逐條／未確認／不適用 | [原始來源](https://www.mlshb.gov.tw/nc/api/v3/storage/files/fd238b2e08f115626b952c90db516e8f67717b7d051d8f1fd1b3821dbacfbe42.pdf) |
| SRC_DATASET_6949 | 違規食品廣告資料集 | 官方資料集metadata | CURRENT | 2014-07-07 | 逐條／未確認／不適用 | [原始來源](https://data.gov.tw/dataset/6949) |
| SRC_DATASET_83771 | 臺中市食品違規廣告處辦統計表 | 官方資料集metadata | CURRENT | 2018-05-04 | 逐條／未確認／不適用 | [原始來源](https://data.gov.tw/dataset/83771) |
| SRC_DATASET_168539 | 桃園市政府衛生局食品廣告違規案件調查結果每月統計 | 官方資料集metadata | CURRENT | 2024-05-03 | 逐條／未確認／不適用 | [原始來源](https://data.gov.tw/dataset/168539) |
| SRC_DATASET_133356 | 臺北市政府衛生局公告食品、藥品、醫療器材及化粧品違規廣告處罰案件統計表 | 官方資料集metadata | CURRENT | 2020-11-25 | 逐條／未確認／不適用 | [原始來源](https://data.gov.tw/dataset/133356) |
| SRC_DATASET_161400 | 臺南市政府食品違規廣告裁罰統計表 | 官方資料集metadata | CURRENT | 2023-01-16 | 逐條／未確認／不適用 | [原始來源](https://data.gov.tw/dataset/161400) |
| SRC_FAIRTRADE_ACT | 公平交易法 | 法律 | CURRENT | 2017-06-14 | 逐條／未確認／不適用 | [原始來源](https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=J0150002) |
| SRC_CONSUMER_ACT | 消費者保護法 | 法律 | CURRENT | 2015-06-17 | 2015-06-19 | [原始來源](https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=J0170001) |
| SRC_SUGAR_QA | 包裝食品營養宣稱問答：不額外添加砂糖與總糖含量 | 官方QA | CURRENT | 未確認／不適用 | 逐條／未確認／不適用 | [原始來源](https://foodlabel.fda.gov.tw/FdaFrontEndApp/FoodFAQ/Edit?SystemId=9143a0db-8e34-4bd3-bf77-e1eee28dd716) |

## 附錄 C：需複核的規則索引

此表只列 review_status=NEEDS_REVIEW；不把所有官方例句變成詞彙黑名單。

| rule_id | source_id／位置 | policy_effect | 複核項目 |
|---|---|---|---|
| RULE_0002 | SRC_CLAIM_REG／第3條 | NEEDS_REVIEW | 應綜合品名、文字、影像、聲音等整體表現認定 |
| RULE_0021 | SRC_FOOD_ACT／第45條 | RESTRICTED | 違規食品廣告裁罰、停止刊播及更正等制度 |
| RULE_0057 | SRC_HEALTH_DISPLAY_PDF／第二點第一款 | RESTRICTED | 功效項目與核准敘述並列之版面要求（一） |
| RULE_0058 | SRC_HEALTH_DISPLAY_PDF／第二點第二款 | RESTRICTED | 功效項目與核准敘述並列之版面要求（二） |
| RULE_0059 | SRC_HEALTH_DISPLAY_PDF／第二點第三款 | RESTRICTED | 功效項目與核准敘述並列之版面要求（三） |
| RULE_0060 | SRC_HEALTH_DISPLAY_PDF／第二點第四款 | RESTRICTED | 功效項目與核准敘述並列之版面要求（四） |
| RULE_0090 | SRC_INFANT_MOJ／第7條第2項但書 | NEEDS_REVIEW | 開罐價促銷禁止之施行日期尚未定 |
| RULE_0110 | SRC_FUNC_20313／方法本文／科學評估 | REQUIRES_EVIDENCE | 免疫調節應依官方方法提出功效評估與科學證據 |
| RULE_0119 | SRC_FUNC_51766／肆、保健功效敘述之宣稱／第1處 | PRODUCT_SPECIFIC_ONLY | 調節血糖功效敘述依檢測指標結果及核發許可核可 |
| RULE_0120 | SRC_FUNC_51766／公告事項／舊方法試驗過渡條款 | PERMITTED | 調節血糖新方法生效前已開始試驗案件之有限舊法過渡 |
| RULE_0121 | SRC_FUNC_20324／全文／掃描PDF30頁 | NEEDS_REVIEW | 延緩衰老掃描評估方法之OCR需逐字核對後才能自動判斷 |
| RULE_0137 | SRC_REPLACEMENT_NEWS／官方說明／最後段落 | NEEDS_REVIEW | 食品不能取代正規醫療之官方說明與個案判斷脈絡 |
| RULE_0143 | SRC_GUIDANCE／肆／廣告定義及輔導說明 | NEEDS_REVIEW | 封閉社群逐一私訊與B2B亦可能構成廣告 |
| RULE_0144 | SRC_GUIDANCE／伍一／成分分類與原料資料庫 | NEEDS_REVIEW | 原料資料庫非完整正面表列，中藥材產品屬性須個案判定 |
| RULE_0145 | SRC_GUIDANCE／伍三(二) | NEEDS_REVIEW | 同音錯字隱喻專利見證仍須按實質意涵判斷 |
| RULE_0146 | SRC_GUIDANCE／伍四／絕對性描述 | REQUIRES_EVIDENCE | 唯一100%有效首創等絕對描述須客觀可信佐證 |
| RULE_0147 | SRC_GUIDANCE／伍六／完整表達及歸屬 | NEEDS_REVIEW | 營養功能詞句不得截取片段當產品名或錯置給其他成分 |
| RULE_0148 | SRC_GUIDANCE／伍七(一) | REQUIRES_EVIDENCE | 科學資料須同配方同對象且完整呈現 |
| RULE_0149 | SRC_GUIDANCE／伍七(二) | REQUIRES_EVIDENCE | 專利獎項驗證不得逾法規且需真實佐證 |
| RULE_0150 | SRC_GUIDANCE／伍八／廣告見證責任 | NEEDS_REVIEW | 見證評論個人經驗與專業身分仍須整體判斷 |
| RULE_0151 | SRC_GUIDANCE／伍九／比較廣告 | REQUIRES_EVIDENCE | 比較廣告須有公正客觀相當標準及完整佐證 |
| RULE_0152 | SRC_CLAIM_NAME_QA／Q2／A2 | NEEDS_REVIEW | 健康商標或品牌與品名併排易誤認時仍須檢視 |
| RULE_0153 | SRC_LEGACY_HEALTH_WORDING／附錄八／名詞宣稱3(2) | NEEDS_REVIEW | 舊前置健康詞例示不得直接當現行准許判斷 |
| RULE_0154 | SRC_AD_PENALTIES／第二點 | RESTRICTED | 違規廣告裁罰處理原則（二） |
| RULE_0155 | SRC_AD_PENALTIES／第三點 | RESTRICTED | 違規廣告裁罰處理原則（三） |
| RULE_0156 | SRC_AD_PENALTIES／第四點 | RESTRICTED | 違規廣告裁罰處理原則（四） |
| RULE_0157 | SRC_AD_PENALTIES／第五點 | RESTRICTED | 違規廣告裁罰處理原則（五） |
| RULE_0158 | SRC_AD_PENALTIES／第七點 | RESTRICTED | 違規廣告裁罰處理原則（七） |
| RULE_0159 | SRC_AD_PENALTIES／第八點 | RESTRICTED | 違規廣告裁罰處理原則（八） |

## 附錄 D：格式、欄位與筆數

```json
{
  "sources": 71,
  "rules": 163,
  "examples": 267,
  "health_functions": 14,
  "documents": 976,
  "current_official_documents": 61,
  "superseded_documents": 9,
  "source_version_needs_review": 1,
  "needs_review_rules": 29,
  "needs_review_effect_rules": 11,
  "needs_review_function_rows": 3,
  "sources_with_unconfirmed_or_nonstatutory_dates": 26,
  "ocr_unverified_chunks": 30,
  "open_data_sources_found": 5,
  "official_query_portals": 1
}
```

CSV 含 UTF-8 BOM，使用支援 RFC 4180 的 CSV parser，勿逐行 split，因原文欄位含換行。JSONL 每行一個 JSON object；content 保留全文條文或 PDF 頁面。source_id 為外鍵，example 的 rule_id 指向規則；功效列另以 evaluation_source_id 指向方法，historical_evaluation_source_ids 保存已下載的歷史方法。official_definition 不是本研究新增的醫學定義。

raw manifest 包含原始文件及文字衍生檔的 SHA-256、官方父頁、實際附件 URL、版本與擷取時間。官方 Azure 檔案以食品標示平台 API 的公開附件鏈證明來源，並保留該 API 原始 JSON。完整下載包不包含研究用程式或任何應用程式資料庫檔。
