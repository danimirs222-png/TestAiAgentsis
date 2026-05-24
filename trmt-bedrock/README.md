# TRMT — The Roads More Travelled (Bedrock port)

Bedrock Edition (`.mcaddon`) port of [milkucha/trmt](https://github.com/milkucha/trmt)
v0.5-26.1+26.1.1+26.1.2, originally a Fabric mod for Minecraft Java Edition.

> The Java mod is licensed CC-BY-NC-4.0. This port keeps the same license and
> credits the original author. Bedrock-specific code (`trmt_bp/scripts/main.js`,
> JSON definitions, baked grass-side/grass-top textures and the four bottle
> icons) is new work written specifically for this conversion.

## What's in here

- `dist/TRMT-0.5-bedrock.mcaddon` — combined add-on. Open this on a device with
  Minecraft Bedrock Edition to import both packs at once.
- `dist/trmt_bp.mcpack` / `dist/trmt_rp.mcpack` — behaviour pack and resource
  pack as individual `.mcpack` files, in case you want to install them
  separately.
- `trmt_bp/` — behaviour pack source (blocks, items, loot tables, recipes, the
  Script API logic in `scripts/main.js`, all 10 localisation files).
- `trmt_rp/` — resource pack source (textures, geometry for the eroded sand
  stages, baked grass top/side textures, blocks.json, terrain/item texture
  catalogs, all 10 localisation files).
- `tools/` — small Python scripts used to bake textures and convert the Java
  language JSONs to Bedrock `.lang` format. They are not needed at runtime; they
  are checked in so the build is reproducible.

## How the Java mod was ported

The Java mod uses Fabric mixins to inject erosion logic into vanilla classes
(`SpreadingSnowyBlock`, `SandBlock`, `Mob`, `ServerPlayer`, `BoneMealItem`,
`BrushItem`, `HoeItem`, `ShovelItem`, `SugarCaneBlock`). Bedrock has no mixin
analogue, so every hook was rewritten on top of the [`@minecraft/server` Script
API](https://learn.microsoft.com/en-us/minecraft/creator/scriptapi/):

| Java mixin / class | Bedrock equivalent |
|---|---|
| `ServerPlayerEntityMixin.onTick` | `system.runInterval(.., 1)` iterating `world.getAllPlayers()`. Adjacent-step propagation, mounted/leash multipliers and the Lightness exemption are mirrored 1:1. |
| `MobEntityMixin.onTick` | `system.runInterval(.., 4)` iterating non-player entities in all three dimensions. |
| `BoneMealItemMixin` / `BrushItemMixin` / `HoeItemMixin` / `ShovelItemMixin` | `world.beforeEvents.itemUseOn` and `world.afterEvents.itemCompleteUse` listeners that perform the same block transformations and durability damage. |
| `SugarCaneBlockMixin` | `itemUseOn` listener that places sugar cane onto `eroded_sand:stage=0` and rejects placements onto higher stages. |
| `GrassBlockMixin` (de-erosion) | A dedicated `tickDeErosion` interval that scans the cooldown map and reverts blocks once the configured day count elapses, applying the `isIsolated` halving rule. |
| `ErosionMapManager` + `ChunkErosionMap` + `ErosionEntry` | An in-memory `Map<string, entry>` keyed by `dim|x,y,z`, persisted into two world dynamic properties (`trmt:entries`, `trmt:cooldowns`) on a 2-second cadence. |
| `BlockColorRegistry` (grass biome tint) | `tint_method: "grass"` on the eroded-grass-block top face. The four side faces are pre-baked at plains-biome colour to avoid the dirt portion of the side being over-tinted. |
| `ModelLoadingPlugin` (custom grass model) | Geometry stays as full-block; the visual layering is handled by baking the eroded overlay onto a neutral grey grass-top texture in `tools/bake_grass_top.py`. |
| `ErodedSandBlock` custom outline/collision shapes | Four custom geometry files (`trmt_rp/models/blocks/eroded_sand_s{1,2,3,4}.geo.json`) with the same heights as the Java models (14/14/12/10) plus `minecraft:collision_box` overrides per permutation (10/10/10/10). |
| `VersionCheckPayload` / disconnect screen with update button | Skipped — Bedrock add-ons ship as a single artifact, so there is no client/server version mismatch path. The localisation strings are still translated and present in the `.lang` files. |
| Brewing recipe registry (Java datagen) | `trmt_bp/recipes/brew_lightness*.json` declarative recipes for the brewing stand: Awkward + Feather → Lightness, Lightness + Redstone → Long Lightness, Lightness + Gunpowder → Splash, Splash + Dragon's Breath → Lingering. Tipped Arrow uses an 8-arrow shaped recipe around a Lingering bottle. |
| `MobEffect` "Lightness" (custom) | Implemented in scripts as a dynamic-property timer (`trmt:lightness_until` on the player) plus a tag (`trmt_lightness`); an action bar countdown is shown each second using `trmt.actionbar.lightness` from the language files. Splash/lingering bottles emulate vanilla throw physics by integrating a parabolic trajectory and applying the effect within a 4-block radius. |
| `/trmt reload-config` / `/trmt convert-to-vanilla` / `/trmt eroded-chunks` | Exposed as `/scriptevent trmt:reload-config`, `/scriptevent trmt:convert-to-vanilla <chunkRadius>`, `/scriptevent trmt:eroded-chunks`, plus chat aliases `!trmt reloadconfig` etc. |

Everything in `TRMTConfig` — the multipliers (0.5 / 2.0 / 1.5), the four block
threshold ranges, the vegetation list (19 entries), the de-erosion timeouts in
days (5 for grass, 3/5/8/13 for sand, 8 for dirt, 13 for coarse dirt), the
toggles for grass/dirt/sand/leaves/vegetation erosion and de-erosion — lives in
the `DEFAULT_CONFIG` constant of `scripts/main.js`. The script reads
`world.getDynamicProperty('trmt:config')` first, so a server operator can paste
a custom config JSON over the default by running:

```
/scriptevent trmt:reload-config
```

after editing the property (see in-game commands below).

## In-game commands

All commands run from any source that can execute `/scriptevent`:

| Command | Effect |
|---|---|
| `/scriptevent trmt:reload-config` | re-load the config from world properties |
| `/scriptevent trmt:save-config` | flush the current config back to world properties |
| `/scriptevent trmt:convert-to-vanilla 32` | revert every eroded block in the surrounding 32-chunk radius |
| `/scriptevent trmt:eroded-chunks` | print how many chunks currently hold cooldown entries |
| `/scriptevent trmt:give-lightness 3600` | apply Lightness to the executing entity for 3600 ticks (3 min) |

Chat shortcuts are also accepted: `!trmt reloadconfig`, `!trmt convert-to-vanilla 32`,
`!trmt eroded-chunks`, `!trmt lightness 3600`.

## Build from source

The artefacts in `dist/` are reproducible. From this directory:

```
python3 tools/make_potion_icons.py        # writes the four bottle icons
python3 tools/bake_grass_top.py           # bakes the eroded grass-top textures
python3 tools/build_langs.py              # converts Java lang JSONs to .lang
(cd trmt_bp && zip -r ../dist/trmt_bp.mcpack .)
(cd trmt_rp && zip -r ../dist/trmt_rp.mcpack .)
zip -r dist/TRMT-0.5-bedrock.mcaddon trmt_bp trmt_rp
```

`tools/build_langs.py` reads the original Java mod's lang JSONs from
`/home/ubuntu/work/trmt-source/assets/trmt/lang/`. If you don't have the Java
source extracted, the `.lang` files are already committed under
`trmt_{bp,rp}/texts/` so this step is optional.

## Notes & caveats

- The Bedrock format version targeted is `1.21.40`. Older clients will refuse
  the manifest's `min_engine_version`. If you need to run on an older client,
  drop `min_engine_version` to your version and verify that
  `permutations` + `tag:*` style component tags are supported in your build —
  both have been stable since 1.20.x.
- Custom mob effects do not exist in Bedrock. The Lightness effect is emulated
  via a player tag (`trmt_lightness`) + a dynamic property (`trmt:lightness_until`).
  It does not appear in the vanilla effects HUD; instead, a translated action
  bar countdown is shown each second while the effect is active.
- Splash / lingering bottles do not use a vanilla potion entity (Bedrock does
  not expose one to add-ons). Instead the script integrates a parabolic
  trajectory from the player's head and applies the effect to nearby players
  on impact. Lingering bottles maintain a 30-second cloud that reapplies the
  effect to anyone within 3 blocks.
- The Java mod's outdated-client disconnect screen with a "Download Update"
  button has no Bedrock equivalent (add-ons ship together with their world),
  so it is intentionally absent. The translation strings (`trmt.disconnect.outdated`,
  `trmt.button.download_update`) are still included in the `.lang` files so a
  future server / realm host could surface them via a different mechanism.
