"""Shared Chinese labels for H&M dashboard presentation."""

from __future__ import annotations

import csv
import re
from functools import cache
from pathlib import Path

LABEL_FILE = Path(__file__).with_name("label_translations.csv")

# Product names mix apparel terms, proprietary abbreviations and model names.
# Recognize common generic phrases without interpreting unmatched text.
PRODUCT_TERMS = {
    "t shirt|t-shirt|tee shirt|tee shirts|tee|tees": "T恤",
    "tank top|tank tops|tanktop|tanktops|tank": "背心",
    "crop top|cropped top": "短款上衣",
    "top|tops": "上衣",
    "polo shirt|polo shirts|polo": "Polo衫",
    "shirt dress|shirt dresses|shirtdress|shirtdresses": "衬衫裙",
    "shirt|shirts": "衬衫",
    "blouse|blouses": "女式上衣",
    "wrap dress|wrap dresses": "裹身连衣裙",
    "midi dress|midi dresses": "中长连衣裙",
    "mini dress|mini dresses": "短款连衣裙",
    "maxi dress|maxi dresses": "长款连衣裙",
    "dress|dresses": "连衣裙",
    "pencil skirt|pencil skirts": "铅笔裙",
    "mini skirt|mini skirts|miniskirt": "迷你裙",
    "skirt|skirts": "半身裙",
    "blazer|blazers|blz": "西装外套",
    "jacket|jackets|jkt": "夹克",
    "trench coat|trench coats|trenchcoat": "风衣",
    "coat|coats": "大衣",
    "parka|parkas": "派克外套",
    "sweatshirt|sweatshirts": "卫衣",
    "hooded sweatshirt|hoodie|hoodies": "连帽卫衣",
    "cardigan|cardigans|cardi": "开衫",
    "sweater|sweaters|jumper|jumpers|pullover|pullovers": "套头衫",
    "vest|vests|waistcoat|waistcoats": "马甲",
    "tunic|tunics": "长款上衣",
    "jeans|jean": "牛仔裤",
    "denim": "牛仔布",
    "jogger|joggers|jogging pants|sweatpants": "束脚运动裤",
    "trouser|trousers|pant|pants|slacks|trs": "长裤",
    "chino|chinos": "休闲长裤",
    "cargo pants|cargo trousers": "工装裤",
    "bermuda shorts|bermudas": "百慕大短裤",
    "bike shorts|biker shorts|cycling shorts": "骑行短裤",
    "shorts": "短裤",
    "leggings|legging": "打底裤",
    "jegging|jeggings": "牛仔打底裤",
    "culotte|culottes": "裙裤",
    "dungaree|dungarees|bib overalls|overalls": "背带裤",
    "jumpsuit|jumpsuits": "连体裤",
    "playsuit|playsuits|romper|rompers": "连体短装",
    "bodysuit|bodysuits": "连体衣",
    "swimsuit|swimsuits|swimwear": "泳衣",
    "bikini top|bikini tops": "比基尼上衣",
    "bikini bottom|bikini bottoms": "比基尼泳裤",
    "bikini|bikinis": "比基尼",
    "swim shorts|swim trunks": "泳裤",
    "sports bra|sports bras": "运动文胸",
    "push-up bra|push up bra": "聚拢文胸",
    "bra|bras|bralette|bralettes": "文胸",
    "boxer briefs|boxer shorts|boxer|boxers": "平角内裤",
    "briefs|brief|panties|knickers|underpants": "内裤",
    "hipster briefs|hipster panties|hipster": "低腰内裤",
    "balconette bra|balconette": "半罩杯文胸",
    "softbra": "软杯文胸",
    "bandeau": "抹胸",
    "tanga": "坦加式内裤",
    "thong|thongs": "丁字裤",
    "underwear": "内衣",
    "pajamas|pyjamas|pyjama|pajama|pj": "睡衣",
    "nightgown|nightgowns|nightdress|nightdresses": "睡裙",
    "bathrobe|bathrobes": "浴袍",
    "tights|pantyhose": "连裤袜",
    "ankle socks|ankle sock": "短筒袜",
    "socks|sock": "袜子",
    "sneaker|sneakers|trainers": "运动鞋",
    "boot|boots|bootie|booties": "靴子",
    "sandal|sandals|sandalette": "凉鞋",
    "flip flop|flip flops|flip-flop|flip-flops": "人字拖",
    "slipper|slippers": "拖鞋",
    "ballerina shoes|ballerina flats|ballerina|ballerinas": "芭蕾平底鞋",
    "espadrille|espadrilles": "草编底鞋",
    "pumps|court shoe|court shoes": "浅口鞋",
    "wedge sandal|wedge sandals|wedge heels": "坡跟鞋",
    "loafer|loafers": "乐福鞋",
    "shoe|shoes": "鞋",
    "crossbody bag|cross body bag|cross-body bag": "斜挎包",
    "shoulder bag|shoulder bags": "单肩包",
    "tote bag|tote bags": "托特包",
    "shopper bag|shopper": "购物包",
    "handbag|handbags": "手提包",
    "backpack|backpacks": "双肩包",
    "bag|bags": "包",
    "wallet|wallets": "钱包",
    "scarf|scarves": "围巾",
    "sunglasses": "太阳镜",
    "beanie|beanies": "针织帽",
    "baseball cap|baseball caps": "棒球帽",
    "cap|caps|hat|hats": "帽子",
    "headband|headbands": "发带",
    "aliceband|alice band": "发箍",
    "hair clip|hair clips|hairclip|hairclips": "发夹",
    "scrunchie|scrunchies": "发圈",
    "glove|gloves": "手套",
    "mittens|mitten": "连指手套",
    "belt|belts": "腰带",
    "bow tie|bow ties": "领结",
    "neck tie|neck ties|necktie|neckties": "领带",
    "earring|earrings": "耳饰",
    "ear cuff|ear cuffs|earcuff|earcuffs": "耳骨夹",
    "ear stud|ear studs|stud earrings": "耳钉",
    "hoop earring|hoop earrings|hoop|hoops": "圈形耳饰",
    "necklace|necklaces": "项链",
    "bracelet|bracelets": "手链",
    "ring|rings": "戒指",
    "jewellery|jewelry": "首饰",
    "lipstick|lipsticks": "口红",
    "nail polish": "指甲油",
    "cushion cover|cushion covers": "靠垫套",
    "pillowcase|pillowcases": "枕套",
    "towel|towels": "毛巾",
    "blanket|blankets": "毯子",
    "bomber jacket|bomber": "飞行员夹克",
    "shacket|shackets": "衬衫式外套",
    "kaftan|caftan": "宽松长袍",
    "henley top|henley": "亨利领上衣",
    "kimono": "和风外搭",
    "long sleeve|long sleeves|long sleeved|long-sleeve|long-sleeved": "长袖",
    "short sleeve|short sleeves|short sleeved|short-sleeve|short-sleeved": "短袖",
    "sleeveless": "无袖",
    "v neck|v-neck|vneck": "V领",
    "crew neck|crew-neck|crewneck": "圆领",
    "turtleneck|turtle neck|roll neck": "高领",
    "high waist|high waisted|high-waist|high-waisted|highwaist": "高腰",
    "low rise|low-rise|low waist|low-waist": "低腰",
    "slim fit|slim-fit": "修身版型",
    "skinny fit|skinny-fit|skinny": "紧身版型",
    "regular fit|regular-fit": "常规版型",
    "relaxed fit|relaxed-fit|loose fit|loose-fit": "宽松版型",
    "straight leg|straight-leg": "直筒",
    "wide leg|wide-leg": "阔腿",
    "bootcut|boot cut|boot-cut": "微喇",
    "cropped": "短款",
    "oversized|oversize": "宽松大版",
    "seamless": "无缝",
    "padded": "带衬垫",
    "lace": "蕾丝",
    "linen": "亚麻",
    "cotton": "棉",
    "wool": "羊毛",
    "fleece": "抓绒",
    "velvet": "丝绒",
    "jersey": "平纹针织",
    "knit|knitted": "针织",
    "woven": "梭织",
    "ribbed": "罗纹",
    "printed": "印花",
    "striped": "条纹",
    "floral": "花卉图案",
    "polka dot|polka dots": "波点",
    "solid colour|solid color": "纯色",
    "rib knit|rib-knit": "罗纹针织",
    "faux fur|fake fur": "仿皮草",
    "zip up|zip-up": "拉链开合",
    "pull on|pull-on": "套穿式",
    "button down|button-down": "纽扣开襟",
    "black": "黑色",
    "white": "白色",
    "off white|off-white": "米白色",
    "dark blue": "深蓝色",
    "light blue": "浅蓝色",
    "navy blue|navy": "藏蓝色",
    "beige": "米色",
    "grey|gray": "灰色",
    "multicolour|multicolor|multi colour|multi color": "多色",
}
PRODUCT_PHRASES = {
    phrase: zh for choices, zh in PRODUCT_TERMS.items() for phrase in choices.split("|")
}
PRODUCT_PATTERN = re.compile(
    r"(?<!\w)(" + "|".join(map(re.escape, sorted(PRODUCT_PHRASES, key=len, reverse=True))) + r")(?!\w)",
    re.IGNORECASE,
)
PACK_PATTERN = re.compile(r"(?<!\w)(\d+)\s*-?\s*pack\b", re.IGNORECASE)


@cache
def translations() -> dict[tuple[str, str], str]:
    with LABEL_FILE.open(encoding="utf-8", newline="") as file:
        rows = csv.DictReader(file)
        if rows.fieldnames != ["field", "source", "zh"]:
            raise ValueError("标签词典需要 field、source 和 zh 三列")
        labels = {}
        for row in rows:
            key = (row["field"], row["source"])
            if key in labels or not all((*key, row["zh"])):
                raise ValueError(f"标签词典第 {rows.line_num} 行缺少值或存在重复")
            labels[key] = row["zh"]
    return labels


def bilingual(value, field: str):
    """Return a presentation label while leaving codes and unknown values intact."""
    if not isinstance(value, str) or not value:
        return value
    if field == "prod_name":
        parts = dict.fromkeys(PRODUCT_PHRASES[m[0].lower()] for m in PRODUCT_PATTERN.finditer(value))
        for pack in PACK_PATTERN.finditer(value):
            parts[f"{pack[1]} 件装"] = None
        zh = "、".join(parts)
    else:
        zh = translations().get((field, value), "")
    return f"{value}（{zh}）" if zh else value
