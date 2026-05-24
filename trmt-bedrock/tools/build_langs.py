"""Convert TRMT Java lang JSONs to Bedrock .lang files.

Bedrock and Java use different localisation key conventions; this script writes
both the resource-pack (player-facing names) and behaviour-pack (text strings
referenced by scripts) .lang files in the matching locales.
"""
import json
import os
from pathlib import Path

JAVA_LANG_DIR = Path("/home/ubuntu/work/trmt-source/assets/trmt/lang")
RP_TEXTS_DIR = Path("/home/ubuntu/work/trmt-addon/build/trmt_rp/texts")
BP_TEXTS_DIR = Path("/home/ubuntu/work/trmt-addon/build/trmt_bp/texts")

RP_TEXTS_DIR.mkdir(parents=True, exist_ok=True)
BP_TEXTS_DIR.mkdir(parents=True, exist_ok=True)

JAVA_TO_BEDROCK_LOCALE = {
    "en_us": "en_US",
    "de_de": "de_DE",
    "es_mx": "es_MX",
    "fr_fr": "fr_FR",
    "ja_jp": "ja_JP",
    "ko_kr": "ko_KR",
    "pl_pl": "pl_PL",
    "pt_br": "pt_BR",
    "ru_ru": "ru_RU",
    "zh_cn": "zh_CN",
}

# Map of Java keys to (Bedrock RP key OR None to skip, Bedrock BP key OR None)
def map_key(java_key: str) -> tuple[str | None, str | None]:
    if java_key == "effect.trmt.lightness":
        return ("effect.trmt.lightness", "trmt.effect.lightness")
    if java_key == "block.trmt.eroded_grass_block":
        return ("tile.trmt:eroded_grass_block.name", None)
    if java_key == "block.trmt.eroded_dirt":
        return ("tile.trmt:eroded_dirt.name", None)
    if java_key == "block.trmt.eroded_coarse_dirt":
        return ("tile.trmt:eroded_coarse_dirt.name", None)
    if java_key == "block.trmt.eroded_sand":
        return ("tile.trmt:eroded_sand.name", None)
    if java_key == "item.minecraft.potion.effect.trmt.lightness":
        return ("item.trmt:potion_of_lightness.name", None)
    if java_key == "item.minecraft.splash_potion.effect.trmt.lightness":
        return ("item.trmt:splash_potion_of_lightness.name", None)
    if java_key == "item.minecraft.lingering_potion.effect.trmt.lightness":
        return ("item.trmt:lingering_potion_of_lightness.name", None)
    if java_key == "item.minecraft.tipped_arrow.effect.trmt.lightness":
        return ("item.trmt:tipped_arrow_lightness.name", None)
    if java_key == "trmt.button.download_update":
        return (None, "trmt.button.download_update")
    if java_key == "trmt.disconnect.outdated":
        return (None, "trmt.disconnect.outdated")
    return (None, None)


PACK_NAME = {
    "en_US": ("TRMT — The Roads More Travelled", "Erosion mod ported from Java (milkucha). Walking causes terrain to erode; restore it with bone meal, brushes, hoes, shovels or Potions of Lightness."),
    "ru_RU": ("TRMT — Дороги Истоптанные", "Мод эрозии, портирован с Java (milkucha). Ходьба разрушает рельеф; восстановите его костной мукой, метлой, мотыгой, лопатой или зельем лёгкости."),
    "de_DE": ("TRMT — The Roads More Travelled", "Erosions-Mod, portiert von Java (milkucha). Laufen erodiert das Gelände; wiederherstellen mit Knochenmehl, Bürsten, Hacken, Schaufeln oder Tränken der Leichtigkeit."),
    "es_MX": ("TRMT — The Roads More Travelled", "Mod de erosión portado de Java (milkucha). Caminar erosiona el terreno; restáuralo con polvo de hueso, brochas, azadas, palas o pociones de ligereza."),
    "fr_FR": ("TRMT — The Roads More Travelled", "Mod d'érosion porté depuis Java (milkucha). Marcher érode le terrain ; restaurez-le avec de la poudre d'os, des pinceaux, des houes, des pelles ou des potions de légèreté."),
    "ja_JP": ("TRMT — The Roads More Travelled", "Java版から移植された侵食モッド（milkucha製）。歩行で地形が侵食される。骨粉、ハケ、クワ、シャベル、または軽さのポーションで復元可能。"),
    "ko_KR": ("TRMT — The Roads More Travelled", "Java에서 이식된 침식 모드 (milkucha 제작). 걸으면 지형이 침식되며, 뼛가루・솔・괭이・삽 또는 가벼움 물약으로 복구할 수 있습니다."),
    "pl_PL": ("TRMT — The Roads More Travelled", "Mod erozji przeniesiony z Javy (milkucha). Chodzenie eroduje teren; przywróć go mączką kostną, pędzlami, motykami, łopatami lub miksturami lekkości."),
    "pt_BR": ("TRMT — The Roads More Travelled", "Mod de erosão portado de Java (milkucha). Caminhar erode o terreno; restaure-o com farinha de osso, pincéis, enxadas, pás ou poções de leveza."),
    "zh_CN": ("TRMT — 侵蚀模组", "由 Java 版移植（milkucha 制作）。行走会侵蚀地形；可用骨粉、刷子、锄、铲或轻盈药水恢复。"),
}

languages = []
for java_locale, bedrock_locale in JAVA_TO_BEDROCK_LOCALE.items():
    java_file = JAVA_LANG_DIR / f"{java_locale}.json"
    with java_file.open(encoding="utf-8") as f:
        data = json.load(f)

    rp_lines = []
    bp_lines = []

    pack_name, pack_desc = PACK_NAME[bedrock_locale]
    # pack.name / pack.description are picked up by Bedrock's pack browser
    rp_lines.append(f"pack.name={pack_name}")
    rp_lines.append(f"pack.description={pack_desc}")
    bp_lines.append(f"pack.name={pack_name}")
    bp_lines.append(f"pack.description={pack_desc}")

    for k, v in data.items():
        rp_key, bp_key = map_key(k)
        if rp_key is not None:
            rp_lines.append(f"{rp_key}={v}")
        if bp_key is not None:
            bp_lines.append(f"{bp_key}={v}")

    # Add lang for the Lightness effect-status line shown by the script
    bp_lines.append("trmt.actionbar.lightness=§eLightness §7%ds")
    rp_lines.append("trmt.actionbar.lightness=§eLightness §7%ds")

    (RP_TEXTS_DIR / f"{bedrock_locale}.lang").write_text("\n".join(rp_lines) + "\n", encoding="utf-8")
    (BP_TEXTS_DIR / f"{bedrock_locale}.lang").write_text("\n".join(bp_lines) + "\n", encoding="utf-8")
    languages.append(bedrock_locale)

(RP_TEXTS_DIR / "languages.json").write_text(json.dumps(sorted(languages), indent=2) + "\n", encoding="utf-8")
(BP_TEXTS_DIR / "languages.json").write_text(json.dumps(sorted(languages), indent=2) + "\n", encoding="utf-8")
print("Wrote langs:", sorted(languages))
