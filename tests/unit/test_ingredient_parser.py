"""成分原文 parser 的格式規則測試。

全部使用虛構（synthetic）成分名稱與含量，只驗證「文字格式」規則，
不對應任何真實產品。
"""

import pytest

from app.ingestion.ingredient_parser import IngredientParseStatus as S
from app.ingestion.ingredient_parser import is_valid_ingredient_name, parse_ingredient_text


def _names(raw):
    return [i.name_zh for i in parse_ingredient_text(raw).ingredients]


@pytest.mark.parametrize(
    "raw, expected",
    [
        # 中文頓號／全形逗號（NFKC 後變 ASCII 逗號）／分號
        ("甲甲素、乙乙醣", ["甲甲素", "乙乙醣"]),
        ("甲甲素，乙乙醣", ["甲甲素", "乙乙醣"]),
        ("甲甲素;乙乙醣；丙丙酸", ["甲甲素", "乙乙醣", "丙丙酸"]),
        # 英文 comma / semicolon
        ("Fakeoside A, Fakeoside B; Fakeol", ["Fakeoside A", "Fakeoside B", "Fakeol"]),
        # 數字中的逗號不切（千分位、化學位置編號）
        ("2,3,4-三虛構丁酸鈣", ["2,3,4-三虛構丁酸鈣"]),
        # 行內編號列表
        ("1. 虛構酸 2. 擬態醣 3. 測試素", ["虛構酸", "擬態醣", "測試素"]),
        ("(1)虛構蛋白:1.0~2.0 公克、(2)擬態纖維:3.0~4.0 公克", ["虛構蛋白", "擬態纖維"]),
        # 並列連接詞
        ("虛構菌及擬態素", ["虛構菌", "擬態素"]),
        # 「飽和」不是連接詞
        ("單元不飽和虛構酸", ["單元不飽和虛構酸"]),
        # 只有英文的長菌株名稱（含英數字代號）
        ("Fakebacillus imaginarius ZZ-0001", ["Fakebacillus imaginarius ZZ-0001"]),
        ("Fakebacillus imaginarius subsp. testii QQ123 乳酸菌", ["Fakebacillus imaginarius subsp. testii QQ123 乳酸菌"]),
        # 冒號標籤:菌名
        ("X菌:Fakebacillus alpha", ["Fakebacillus alpha"]),
        # 功效標題:名稱:含量
        ("免疫調節:虛構酸Z:0.10~0.20毫克", ["虛構酸Z"]),
        # 引號包住的成分名
        ("本產品以「虛構鈣」為指標成分", ["虛構鈣"]),
        # 立體化學標記 (-) 屬於名稱
        ("(-)-Fakechin-3-O-gallate", ["(-)-Fakechin-3-O-gallate"]),
        ("(-)Fakechin gallate (-)Fakechin", ["(-)Fakechin gallate", "(-)Fakechin"]),
        # 整段被括號包住
        ("(β-虛構聚醣) :50mg±10mg", ["β-虛構聚醣"]),
        # Word Symbol 字型私用區的 β
        ("-虛構聚醣", ["β-虛構聚醣"]),
        # 未閉合括號
        ("虛構抑制劑(Fakolin K", ["虛構抑制劑Fakolin K"]),
    ],
)
def test_split_formats(raw, expected):
    assert _names(raw) == expected


@pytest.mark.parametrize(
    "raw, name, name_en, amount",
    [
        ("虛構苷(Fakoside) 2.8~4.2毫克", "虛構苷", "Fakoside", "2.8~4.2毫克"),
        ("擬態酸1.36-2.04 mg/瓶 (50 mL/瓶)", "擬態酸", None, "1.36-2.04 mg/瓶 (50 mL/瓶)"),
        ("維生素Z3  4.2~7.5微克", "維生素Z3", None, "4.2~7.5微克"),        # 名稱尾端數字不是含量
        ("Fakebacillus ZZ-0006>5.0x108 CFU", "Fakebacillus ZZ-0006", None, ">5.0x108 CFU"),
        ("每顆含0.2~0.3 mg Fakeol B", "Fakeol B", None, "0.2~0.3 mg"),        # 含量在前
        ("105毫升含有虛構磺酸(Fakurine)1470毫克", "虛構磺酸", "Fakurine", "1470毫克"),  # 開頭是份量
        ("虛構菌每毫升含1.0×10^6 CFU 以上", "虛構菌", None, "1.0×10^6 CFU 以上"),
        ("虛構醣:≧7.5 毫克/毫升,做為品質管制指標成分。", "虛構醣", None, "≧7.5 毫克/毫升"),
        ("虛構多酚 250〜300毫克/粒", "虛構多酚", None, "250〜300毫克/粒"),
        ("虛構素A含量為 42.2 mg ± 6.4 mg", "虛構素A", None, "42.2 mg ± 6.4 mg"),
        ("虛構酸 (以甲、乙計):8-12毫克", "虛構酸", None, "8-12毫克"),  # 計算基準說明不拆成成分
    ],
)
def test_name_and_amount(raw, name, name_en, amount):
    result = parse_ingredient_text(raw)
    assert result.status == S.PARSED
    (ing,) = result.ingredients
    assert (ing.name_zh, ing.name_en, ing.amount_text) == (name, name_en, amount)


@pytest.mark.parametrize(
    "raw, expected",
    [
        # 份量／劑型／標題／規格不能變成 Ingredient
        ("每份(2粒)含\n虛構酸A 10~20 mg\n魚油純度(A+B) 33~49%", ["虛構酸A"]),
        ("【7克/包】\n保健功效相關成分含量,每份含\n總虛構纖維   5.67~6.93公克", ["總虛構纖維"]),
        ("每顆膠囊495毫克,內容物400毫克,\n含保健功效相關成分虛構聚醣 225.0±45.0毫克。", ["虛構聚醣"]),
        ("品管指標成分:\n虛構鎂:105 ± 21 mg/瓶", ["虛構鎂"]),
        ("A.膠囊-每份(4粒)含:\n(1).虛構酸:47.7~69.3毫克\nB.粉劑-每份(1包)含:\n(1).虛構酸:95.4~138.6毫克", ["虛構酸"]),
        ("以虛構素(Fakein)為品質管制指標;每一份(1顆)含0.2~0.3 mg Fakein", ["虛構素"]),  # 中英同一成分合併
        ("虛構菌 1.0 × 1010 CFU以上/10公克\n≧0.20公克/每瓶42毫升(抗疲勞)", ["虛構菌"]),
        ("本產品之保健功效為產品整體的綜合結果,本產品以「虛構鈣」為指標成分,含量為 268.8~403.2 毫克/包", ["虛構鈣"]),
        ("以總虛構酸為品管指標成分\n(總虛構酸以 A、B 與 C 合計)。", ["總虛構酸"]),
    ],
)
def test_non_ingredient_text_is_not_built(raw, expected):
    result = parse_ingredient_text(raw)
    assert result.status == S.PARSED
    assert [i.name_zh for i in result.ingredients] == expected


@pytest.mark.parametrize(
    "raw, status, n_ingredients",
    [
        (None, S.NO_SOURCE_INGREDIENT, 0),
        ("   ", S.NO_SOURCE_INGREDIENT, 0),
        ("虛構酸", S.PARSED, 1),
        # 多個冒號又沒有含量 → 無法可靠判斷，不捏造
        ("1.總虛構菌(含A菌與C菌) A菌:Fakebacillus a C菌:Fakebacillus c 2.擬態菌(B菌) B菌:Fakebacillus b",
         S.PARTIALLY_PARSED, 1),
        ("扣除賦形劑干擾後虛構醣含量≧15 mg/0.4 g (每錠)", S.UNPARSED, 0),
        ("虛構菌(Fakelli) 其中含擬態菌(Fakebacterium x)", S.UNPARSED, 0),
        ("0.20~0.30 毫克", S.PARSED, 0),
    ],
)
def test_status(raw, status, n_ingredients):
    result = parse_ingredient_text(raw)
    if status == S.PARSED and n_ingredients == 0:
        # 只有含量、沒有任何名稱 → 沒有成分即為 UNPARSED
        status = S.UNPARSED
    assert result.status == status
    assert len(result.ingredients) == n_ingredients


def test_raw_text_always_preserved_and_unresolved_reported():
    raw = "虛構酸 10 mg\n扣除賦形劑干擾後擬態醣含量≧15 mg"
    result = parse_ingredient_text(raw)
    assert result.raw_text == raw
    assert result.status == S.PARTIALLY_PARSED
    assert result.unresolved_segments == ["扣除賦形劑干擾後擬態醣含量≧15 mg"]
    assert result.ingredients[0].source_segment == "虛構酸 10 mg"


@pytest.mark.parametrize(
    "name, ok",
    [
        ("虛構酸", True), ("Fakeoside A", True), ("2,3,4-三虛構丁酸鈣", True), ("維生素Z3", True),
        ("", False), ("每份", False), ("8.0±1.6 毫克", False), ("魚油純度", False), ("保健功效相關成分", False),
        ("12虛構", False), ("虛構酸(未閉合", False), ("本產品以虛構酸", False),
    ],
)
def test_name_validation(name, ok):
    assert is_valid_ingredient_name(name) is ok
