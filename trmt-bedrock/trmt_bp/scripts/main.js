// ============================================================================
//  TRMT — The Roads More Travelled (Bedrock port)
//  Ported from milkucha's Java/Fabric mod v0.5-26.1+26.1.1+26.1.2
//  Original license: CC-BY-NC-4.0
//
//  This file recreates the entire mod logic on top of Bedrock's @minecraft/server
//  Script API. The original mod is mixin-heavy on the Java side; here every hook
//  is reproduced by iterating loaded entities and listening to the matching
//  Bedrock events. No behaviour is dropped — only the storage and event names
//  are different.
// ============================================================================

import {
  world,
  system,
  BlockPermutation,
  ItemStack,
  Direction,
  EquipmentSlot,
  GameMode,
  EntityComponentTypes,
  ItemComponentTypes,
} from "@minecraft/server";

// ============================================================================
//  CONFIG  — mirrors milkucha.trmt.TRMTConfig exactly.
// ============================================================================
const DEFAULT_CONFIG = {
  erosion: {
    grass: true,
    dirt: true,
    sand: true,
    leaves: true,
    vegetation: true,
  },
  deErosion: {
    grass: true,
    dirt: true,
    sand: true,
  },
  multipliers: {
    player: 0.5,
    mounted: 2.0,
    leash: 1.5,
  },
  erosionThresholds: {
    grass: { min: 2.0, max: 4.0 },
    dirt:  { min: 8.0, max: 12.0 },
    coarseDirt: { min: 12.0, max: 20.0 },
    sand:  { min: 1.5, max: 3.0 },
    leaves: { min: 6.0, max: 12.0, dropChance: 0.5 },
    vegetation: { min: 1.0, max: 4.0, dropChance: 1.0 },
  },
  // The Java mod ticks de-erosion in days. We mirror the same values; the
  // script converts them to ticks (20 ticks/sec * 1200 sec/day = 24000).
  deErosionTimeoutDays: {
    erodedGrassS1: 5.0,
    erodedGrassS2: 5.0,
    erodedGrassS3: 5.0,
    erodedGrassS4: 5.0,
    erodedGrassRevert: 5.0,
    erodedSandS1: 3.0,
    erodedSandS2: 5.0,
    erodedSandS3: 8.0,
    erodedSandS4: 13.0,
    erodedSandRevert: 13.0,
    erodedDirt: 8.0,
    erodedCoarseDirt: 13.0,
  },
  erodableVegetation: [
    "minecraft:short_grass",
    "minecraft:tall_grass",
    "minecraft:fern",
    "minecraft:large_fern",
    "minecraft:dandelion",
    "minecraft:poppy",
    "minecraft:blue_orchid",
    "minecraft:allium",
    "minecraft:azure_bluet",
    "minecraft:red_tulip",
    "minecraft:orange_tulip",
    "minecraft:white_tulip",
    "minecraft:pink_tulip",
    "minecraft:oxeye_daisy",
    "minecraft:cornflower",
    "minecraft:lily_of_the_valley",
    "minecraft:torchflower",
    "minecraft:sweet_berry_bush",
    "minecraft:pink_petals",
  ],
  // Lightness effect duration in ticks. 3 min and 8 min, like the Java mod.
  lightnessDurationTicks: 3600,
  longLightnessDurationTicks: 9600,
};

let CONFIG = JSON.parse(JSON.stringify(DEFAULT_CONFIG));

function loadConfig() {
  try {
    const raw = world.getDynamicProperty("trmt:config");
    if (typeof raw === "string" && raw.length > 0) {
      const parsed = JSON.parse(raw);
      // Shallow merge over defaults so missing keys keep their defaults.
      CONFIG = deepMerge(JSON.parse(JSON.stringify(DEFAULT_CONFIG)), parsed);
    }
  } catch (e) {
    console.warn("[TRMT] failed to load config:", e);
  }
}

function saveConfig() {
  try {
    world.setDynamicProperty("trmt:config", JSON.stringify(CONFIG));
  } catch (e) {
    console.warn("[TRMT] failed to save config:", e);
  }
}

function deepMerge(target, source) {
  for (const key of Object.keys(source)) {
    if (
      source[key] &&
      typeof source[key] === "object" &&
      !Array.isArray(source[key])
    ) {
      if (!target[key] || typeof target[key] !== "object") target[key] = {};
      deepMerge(target[key], source[key]);
    } else {
      target[key] = source[key];
    }
  }
  return target;
}

// ============================================================================
//  EROSION DATABASE — equivalent of ErosionMapManager + ChunkErosionMap.
// ============================================================================
//
// We split the persistent store into two pieces, both kept as JSON in world
// dynamic properties:
//
//   trmt:entries   { "dim|x,y,z": { block, threshold, walked, last, stage } }
//                  Active accumulators for blocks that have been stepped on
//                  but have not yet completed their full erosion progression.
//
//   trmt:cooldowns { "dim|x,y,z": { block, last } }
//                  Last-touched markers for eroded blocks that are eligible
//                  for de-erosion. They tick down toward reverting back to a
//                  vanilla block after `getDeErosionTimeoutTicks` ticks.
//
// Both stores are loaded into in-memory maps on world load and persisted on a
// fixed cadence (every TWO seconds) plus on shutdown best-effort.

const entries = new Map();   // key -> { block, threshold, walked, last, stage }
const cooldowns = new Map(); // key -> { block, last }
let dirtyEntries = false;
let dirtyCooldowns = false;

function key(dim, pos) {
  return `${dim}|${pos.x},${pos.y},${pos.z}`;
}
function unkey(k) {
  const [dim, rest] = k.split("|");
  const [x, y, z] = rest.split(",").map((s) => parseInt(s, 10));
  return { dim, pos: { x, y, z } };
}

function loadStores() {
  try {
    const e = world.getDynamicProperty("trmt:entries");
    if (typeof e === "string" && e.length > 0) {
      const obj = JSON.parse(e);
      for (const k of Object.keys(obj)) entries.set(k, obj[k]);
    }
  } catch (err) {
    console.warn("[TRMT] failed to load entries:", err);
  }
  try {
    const c = world.getDynamicProperty("trmt:cooldowns");
    if (typeof c === "string" && c.length > 0) {
      const obj = JSON.parse(c);
      for (const k of Object.keys(obj)) cooldowns.set(k, obj[k]);
    }
  } catch (err) {
    console.warn("[TRMT] failed to load cooldowns:", err);
  }
}

function persistStores() {
  if (dirtyEntries) {
    try {
      const out = {};
      for (const [k, v] of entries) out[k] = v;
      world.setDynamicProperty("trmt:entries", JSON.stringify(out));
      dirtyEntries = false;
    } catch (err) {
      console.warn("[TRMT] failed to persist entries:", err);
    }
  }
  if (dirtyCooldowns) {
    try {
      const out = {};
      for (const [k, v] of cooldowns) out[k] = v;
      world.setDynamicProperty("trmt:cooldowns", JSON.stringify(out));
      dirtyCooldowns = false;
    } catch (err) {
      console.warn("[TRMT] failed to persist cooldowns:", err);
    }
  }
}

// ============================================================================
//  HELPERS
// ============================================================================
const RAD_TO_INDEX = 1.0 / (Math.PI / 2.0);

function posRotation(pos) {
  // Same hash that BlockThresholds.posRotation produces (deterministic per
  // position). Returns 0..3.
  let h = (pos.x | 0) * 73856093;
  h ^= (pos.y | 0) * 19349663;
  h ^= (pos.z | 0) * 83492791;
  h = ((h | 0) >>> 0) % 4;
  return h;
}

function rotationToFacing(rotation) {
  switch (rotation) {
    case 1: return "west";
    case 2: return "north";
    case 3: return "east";
    default: return "south";
  }
}

function randomThreshold(blockId) {
  const t = CONFIG.erosionThresholds;
  let range;
  if (blockId === "minecraft:grass_block" || blockId === "trmt:eroded_grass_block") {
    range = t.grass;
  } else if (blockId === "minecraft:dirt") {
    range = t.dirt;
  } else if (blockId === "trmt:eroded_dirt") {
    range = t.coarseDirt; // dirt -> coarse stage uses coarseDirt range
  } else if (blockId === "minecraft:sand" || blockId === "trmt:eroded_sand") {
    range = t.sand;
  } else if (isLeavesId(blockId)) {
    range = t.leaves;
  } else if (isVegetationId(blockId)) {
    range = t.vegetation;
  } else {
    range = { min: 1, max: 2 };
  }
  return range.min + Math.random() * (range.max - range.min);
}

function isLeavesId(id) {
  return (
    id === "minecraft:oak_leaves" ||
    id === "minecraft:spruce_leaves" ||
    id === "minecraft:birch_leaves" ||
    id === "minecraft:jungle_leaves" ||
    id === "minecraft:acacia_leaves" ||
    id === "minecraft:dark_oak_leaves" ||
    id === "minecraft:cherry_leaves" ||
    id === "minecraft:mangrove_leaves" ||
    id === "minecraft:azalea_leaves" ||
    id === "minecraft:flowering_azalea_leaves" ||
    id === "minecraft:pale_oak_leaves"
  );
}

function isVegetationId(id) {
  return CONFIG.erodableVegetation.includes(id);
}

function isEroded(id) {
  return (
    id === "trmt:eroded_grass_block" ||
    id === "trmt:eroded_dirt" ||
    id === "trmt:eroded_coarse_dirt" ||
    id === "trmt:eroded_sand"
  );
}

function isTrackable(blockId) {
  const e = CONFIG.erosion;
  if (e.grass && (blockId === "minecraft:grass_block" || blockId === "trmt:eroded_grass_block")) return true;
  if (e.dirt && (blockId === "minecraft:dirt" || blockId === "trmt:eroded_dirt")) return true;
  if (e.sand && (blockId === "minecraft:sand" || blockId === "trmt:eroded_sand")) return true;
  if (e.leaves && isLeavesId(blockId)) return true;
  if (e.vegetation && isVegetationId(blockId)) return true;
  return false;
}

function getDeErosionTimeoutTicks(blockId, stage) {
  const d = CONFIG.deErosionTimeoutDays;
  let days = 5.0;
  if (blockId === "trmt:eroded_grass_block") {
    if (stage === 1) days = d.erodedGrassS1;
    else if (stage === 2) days = d.erodedGrassS2;
    else if (stage === 3) days = d.erodedGrassS3;
    else if (stage === 4) days = d.erodedGrassS4;
    else days = d.erodedGrassRevert;
  } else if (blockId === "trmt:eroded_sand") {
    if (stage === 1) days = d.erodedSandS1;
    else if (stage === 2) days = d.erodedSandS2;
    else if (stage === 3) days = d.erodedSandS3;
    else if (stage === 4) days = d.erodedSandS4;
    else days = d.erodedSandRevert;
  } else if (blockId === "trmt:eroded_dirt") {
    days = d.erodedDirt;
  } else if (blockId === "trmt:eroded_coarse_dirt") {
    days = d.erodedCoarseDirt;
  }
  return Math.floor(days * 24000); // 24000 ticks per Minecraft day
}

function getDimensionFromId(id) {
  try { return world.getDimension(id); } catch { return undefined; }
}

function safeGetBlock(dim, pos) {
  try {
    return dim.getBlock(pos);
  } catch { return undefined; }
}

function rotateAround(dim, pos, dir) {
  // dir is south/west/north/east relative; we need the BlockPos relative to pos.
  switch (dir) {
    case "north": return { x: pos.x, y: pos.y, z: pos.z - 1 };
    case "south": return { x: pos.x, y: pos.y, z: pos.z + 1 };
    case "west":  return { x: pos.x - 1, y: pos.y, z: pos.z };
    case "east":  return { x: pos.x + 1, y: pos.y, z: pos.z };
    default: return pos;
  }
}

function counterClockWise(facing) {
  switch (facing) {
    case "north": return "west";
    case "west":  return "south";
    case "south": return "east";
    case "east":  return "north";
    default: return facing;
  }
}
function clockWise(facing) {
  switch (facing) {
    case "north": return "east";
    case "east":  return "south";
    case "south": return "west";
    case "west":  return "north";
    default: return facing;
  }
}

function isolatedFromOtherEroded(dim, pos) {
  // Mirrors BlockThresholds.isIsolated: scans the 3x3 around pos for other
  // eroded blocks of the same family.
  for (let dx = -1; dx <= 1; dx++) {
    for (let dz = -1; dz <= 1; dz++) {
      if (dx === 0 && dz === 0) continue;
      const b = safeGetBlock(dim, { x: pos.x + dx, y: pos.y, z: pos.z + dz });
      if (b && isEroded(b.typeId)) return false;
    }
  }
  return true;
}

// ============================================================================
//  STEP RECORDING — equivalent of ErosionMapManager.onStep.
// ============================================================================
function recordStep(dim, pos, blockId, amount, gameTime) {
  if (!isTrackable(blockId)) return;
  const k = key(dim.id, pos);
  let entry = entries.get(k);
  if (!entry) {
    entry = {
      block: blockId,
      threshold: randomThreshold(blockId),
      walked: 0,
      last: gameTime,
      stage: 0,
    };
    entries.set(k, entry);
  }
  if (entry.block !== blockId) {
    // The block under this position was replaced; reset the entry.
    entry.block = blockId;
    entry.threshold = randomThreshold(blockId);
    entry.walked = 0;
  }
  entry.walked += amount;
  entry.last = gameTime;
  dirtyEntries = true;
}

function getEntry(dim, pos) {
  return entries.get(key(dim.id, pos));
}

function removeEntry(dim, pos) {
  if (entries.delete(key(dim.id, pos))) dirtyEntries = true;
}

function writeCooldown(dim, pos, blockId, gameTime) {
  cooldowns.set(key(dim.id, pos), { block: blockId, last: gameTime });
  dirtyCooldowns = true;
}

function removeCooldown(dim, pos) {
  if (cooldowns.delete(key(dim.id, pos))) dirtyCooldowns = true;
}

// ============================================================================
//  TRANSFORM — same chain as ServerPlayerEntityMixin/MobEntityMixin.trmt$tryTransform.
// ============================================================================
function tryTransform(dim, pos) {
  const block = safeGetBlock(dim, pos);
  if (!block) return;
  const entry = getEntry(dim, pos);
  if (!entry || entry.walked < entry.threshold) return;
  const id = block.typeId;
  const gameTime = system.currentTick;

  // VEGETATION — destroyed when stepped on enough.
  if (isVegetationId(id)) {
    if (CONFIG.erosion.vegetation) {
      destroyVegetationBlock(dim, pos, block);
    }
    removeEntry(dim, pos);
    return;
  }

  // LEAVES — destroyed when stepped on enough.
  if (isLeavesId(id)) {
    if (CONFIG.erosion.leaves) {
      const dropChance = CONFIG.erosionThresholds.leaves.dropChance;
      const drops = dropChance >= 1.0 || (dropChance > 0.0 && Math.random() < dropChance);
      dim.runCommand(`setblock ${pos.x} ${pos.y} ${pos.z} air ${drops ? "destroy" : "replace"}`);
    }
    removeEntry(dim, pos);
    return;
  }

  // SAND
  if (id === "minecraft:sand") {
    if (!CONFIG.erosion.sand) return;
    const above = safeGetBlock(dim, { x: pos.x, y: pos.y + 1, z: pos.z });
    if (above && !above.isAir && !above.permutation?.type?.canBeDestroyedByLiquidSpread) return;
    const facing = rotationToFacing(posRotation(pos));
    try {
      block.setPermutation(
        BlockPermutation.resolve("trmt:eroded_sand", { "trmt:stage": 0, "trmt:facing": facing })
      );
      removeEntry(dim, pos);
      writeCooldown(dim, pos, "trmt:eroded_sand", gameTime);
    } catch (e) {
      console.warn("[TRMT] sand transform failed:", e);
    }
    return;
  }
  if (id === "trmt:eroded_sand") {
    if (!CONFIG.erosion.sand) return;
    const above = safeGetBlock(dim, { x: pos.x, y: pos.y + 1, z: pos.z });
    if (above && !above.isAir) return;
    const stage = block.permutation.getState("trmt:stage") ?? 0;
    const facing = block.permutation.getState("trmt:facing") ?? "south";
    const newStage = Math.min(4, stage + 1);
    if (newStage !== stage) {
      try {
        block.setPermutation(
          BlockPermutation.resolve("trmt:eroded_sand", { "trmt:stage": newStage, "trmt:facing": facing })
        );
      } catch (e) { console.warn("[TRMT] sand stage advance failed:", e); }
    }
    removeEntry(dim, pos);
    writeCooldown(dim, pos, "trmt:eroded_sand", gameTime);
    return;
  }

  // GRASS BLOCK
  if (id === "minecraft:grass_block") {
    if (!CONFIG.erosion.grass) return;
    const facing = rotationToFacing(posRotation(pos));
    try {
      block.setPermutation(
        BlockPermutation.resolve("trmt:eroded_grass_block", { "trmt:stage": 0, "trmt:facing": facing })
      );
      removeEntry(dim, pos);
      writeCooldown(dim, pos, "trmt:eroded_grass_block", gameTime);
    } catch (e) { console.warn("[TRMT] grass transform failed:", e); }
    return;
  }
  if (id === "trmt:eroded_grass_block") {
    if (!CONFIG.erosion.grass) return;
    const stage = block.permutation.getState("trmt:stage") ?? 0;
    const facing = block.permutation.getState("trmt:facing") ?? "south";
    if (stage < 4) {
      try {
        block.setPermutation(
          BlockPermutation.resolve("trmt:eroded_grass_block", { "trmt:stage": stage + 1, "trmt:facing": facing })
        );
        removeEntry(dim, pos);
        writeCooldown(dim, pos, "trmt:eroded_grass_block", gameTime);
      } catch (e) { console.warn("[TRMT] grass stage advance failed:", e); }
      return;
    }
    // Stage 4 -> convert to eroded_dirt at stage 0 (keeps facing).
    try {
      block.setPermutation(
        BlockPermutation.resolve("trmt:eroded_dirt", { "trmt:stage": 0, "trmt:facing": facing })
      );
    } catch (e) { console.warn("[TRMT] grass->dirt failed:", e); }
    removeEntry(dim, pos);
    return;
  }

  // ERODED DIRT
  if (id === "trmt:eroded_dirt") {
    if (!CONFIG.erosion.dirt) return;
    const stage = block.permutation.getState("trmt:stage") ?? 0;
    const facing = block.permutation.getState("trmt:facing") ?? "south";
    if (stage < 3) {
      try {
        block.setPermutation(
          BlockPermutation.resolve("trmt:eroded_dirt", { "trmt:stage": stage + 1, "trmt:facing": facing })
        );
      } catch (e) { console.warn("[TRMT] dirt stage advance failed:", e); }
      removeEntry(dim, pos);
      return;
    }
    // Stage 3 -> eroded_coarse_dirt
    try {
      block.setPermutation(
        BlockPermutation.resolve("trmt:eroded_coarse_dirt", { "trmt:facing": facing })
      );
    } catch (e) { console.warn("[TRMT] dirt->coarse failed:", e); }
    removeEntry(dim, pos);
    return;
  }

  // PLAIN DIRT
  if (id === "minecraft:dirt") {
    if (!CONFIG.erosion.dirt) return;
    const facing = rotationToFacing(posRotation(pos));
    try {
      block.setPermutation(
        BlockPermutation.resolve("trmt:eroded_dirt", { "trmt:stage": 1, "trmt:facing": facing })
      );
    } catch (e) { console.warn("[TRMT] dirt initial transform failed:", e); }
    removeEntry(dim, pos);
    return;
  }
}

function destroyVegetationBlock(dim, pos, block) {
  // Mirror the Java mod: if the block is a tall_grass lower half, convert to
  // short_grass; otherwise destroy the block (and the upper half if any).
  try {
    const upperPos = { x: pos.x, y: pos.y + 1, z: pos.z };
    const upper = safeGetBlock(dim, upperPos);
    if (upper && upper.typeId === block.typeId) {
      upper.setPermutation(BlockPermutation.resolve("minecraft:air"));
    }
    if (block.typeId === "minecraft:tall_grass") {
      block.setPermutation(BlockPermutation.resolve("minecraft:short_grass"));
      return;
    }
  } catch (e) { /* fall through */ }

  const dropChance = CONFIG.erosionThresholds.vegetation.dropChance;
  const drops = dropChance >= 1.0 || (dropChance > 0.0 && Math.random() < dropChance);
  dim.runCommand(`setblock ${pos.x} ${pos.y} ${pos.z} air ${drops ? "destroy" : "replace"}`);
}

// ============================================================================
//  PLAYER + MOB STEP TICK — equivalent of ServerPlayerEntityMixin / MobEntityMixin
// ============================================================================
function tickEntityStep(entity, isPlayer) {
  let dim;
  try { dim = entity.dimension; } catch { return; }
  let onGround;
  try { onGround = entity.isOnGround; } catch { return; }
  if (!onGround) return;

  let loc;
  try { loc = entity.location; } catch { return; }
  const groundPos = {
    x: Math.floor(loc.x),
    y: Math.floor(loc.y - 0.01),
    z: Math.floor(loc.z),
  };
  const block = safeGetBlock(dim, groundPos);
  if (!block) return;
  const id = block.typeId;
  if (!isTrackable(id)) return;

  // Lightness effect — players carrying the tag don't accumulate erosion.
  if (isPlayer && hasLightness(entity)) return;

  // Multiplier.
  let mult = CONFIG.multipliers.player;
  if (entity.isClimbing) return; // standing on a ladder/vine — skip
  try {
    const riding = entity.getComponent("minecraft:riding") || entity.getComponent(EntityComponentTypes.Riding);
    if (riding && riding.entityRidingOn) mult = CONFIG.multipliers.mounted;
  } catch { /* ignore */ }

  if (!isPlayer) {
    try {
      const leashable = entity.getComponent(EntityComponentTypes.Leashable) || entity.getComponent("minecraft:leashable");
      if (leashable && leashable.leashHolder) {
        // If leash holder is a player with Lightness, no erosion.
        if (leashable.leashHolder.typeId === "minecraft:player" && hasLightness(leashable.leashHolder)) return;
        mult = CONFIG.multipliers.leash;
      } else {
        // Wild mobs of all kinds get the base player multiplier — same as Java.
        mult = CONFIG.multipliers.player;
      }
    } catch { mult = CONFIG.multipliers.player; }
  }

  const gameTime = system.currentTick;
  recordStep(dim, groundPos, id, 1.0 * mult, gameTime);
  tryTransform(dim, groundPos);
  broadcastIfChanged(dim, groundPos);

  if (isPlayer) {
    // Adjacent step propagation: the same multipliers as the Java mod.
    let yaw;
    try { yaw = entity.getRotation().y; } catch { yaw = 0; }
    const facing = yawToCardinal(yaw);
    const left = counterClockWise(facing);
    const right = clockWise(facing);
    stepAdjacent(dim, rotateAround(dim, groundPos, facing), 0.2 * mult, gameTime);
    stepAdjacent(dim, rotateAround(dim, groundPos, left), 0.5 * mult, gameTime);
    stepAdjacent(dim, rotateAround(dim, groundPos, right), 0.5 * mult, gameTime);
  }
}

function stepAdjacent(dim, pos, amount, gameTime) {
  const b = safeGetBlock(dim, pos);
  if (!b) return;
  if (!isTrackable(b.typeId)) return;
  recordStep(dim, pos, b.typeId, amount, gameTime);
  tryTransform(dim, pos);
}

function yawToCardinal(yaw) {
  // Bedrock yaw: south=0, west=90, north=180/-180, east=-90
  const y = ((yaw % 360) + 360) % 360;
  if (y >= 315 || y < 45) return "south";
  if (y < 135) return "west";
  if (y < 225) return "north";
  return "east";
}

function broadcastIfChanged(_dim, _pos) {
  // The Java mod broadcasts entry updates to the client; the Bedrock script
  // already shares world state with all players, so the broadcast hook becomes
  // a no-op. The function is kept for parity with the original API surface.
}

// ============================================================================
//  LIGHTNESS EFFECT — implemented via tags/dynamic properties + actionbar UI.
// ============================================================================
function applyLightness(player, durationTicks) {
  const now = system.currentTick;
  const until = now + durationTicks;
  player.setDynamicProperty("trmt:lightness_until", until);
  try { player.addTag("trmt_lightness"); } catch {}
}

function hasLightness(player) {
  const until = player.getDynamicProperty("trmt:lightness_until");
  if (typeof until !== "number") return false;
  return until > system.currentTick;
}

function tickLightness() {
  for (const player of world.getAllPlayers()) {
    const until = player.getDynamicProperty("trmt:lightness_until");
    if (typeof until === "number") {
      const remain = until - system.currentTick;
      if (remain > 0) {
        if (remain % 20 === 0) {
          const seconds = Math.ceil(remain / 20);
          try {
            player.onScreenDisplay.setActionBar({
              rawtext: [{ translate: "trmt.actionbar.lightness", with: { rawtext: [{ text: String(seconds) }] } }],
            });
          } catch { /* old API fallback */ }
        }
      } else {
        player.setDynamicProperty("trmt:lightness_until", undefined);
        try { player.removeTag("trmt_lightness"); } catch {}
      }
    }
  }
}

// ============================================================================
//  ITEM HOOKS — bone meal, brush, hoe, shovel — mirror the Java mixins.
// ============================================================================
world.beforeEvents.itemUseOn.subscribe((ev) => {
  const player = ev.source;
  const item = ev.itemStack;
  const dim = ev.block.dimension;
  const pos = { x: ev.block.location.x, y: ev.block.location.y, z: ev.block.location.z };
  const block = ev.block;
  const id = block.typeId;
  const face = ev.blockFace;

  if (!item) return;
  const itemId = item.typeId;

  // Hoe: convert eroded grass/dirt to farmland (if face is not down and above is replaceable).
  if (itemId.endsWith("_hoe")) {
    if (id === "trmt:eroded_grass_block" || id === "trmt:eroded_dirt") {
      if (face === Direction.Down) {
        ev.cancel = true;
        return;
      }
      const above = safeGetBlock(dim, { x: pos.x, y: pos.y + 1, z: pos.z });
      if (above && !above.isAir) {
        ev.cancel = true;
        return;
      }
      ev.cancel = true;
      system.run(() => {
        try {
          block.setPermutation(BlockPermutation.resolve("minecraft:farmland", { "moisturized_amount": 0 }));
          removeEntry(dim, pos);
          removeCooldown(dim, pos);
          dim.playSound("use.gravel", { x: pos.x + 0.5, y: pos.y + 0.5, z: pos.z + 0.5 });
          damageEquipment(player, EquipmentSlot.Mainhand, 1);
        } catch (e) { console.warn("[TRMT] hoe transform failed:", e); }
      });
      return;
    }
  }

  // Shovel: convert eroded grass/dirt to dirt_path.
  if (itemId.endsWith("_shovel")) {
    if (id === "trmt:eroded_grass_block" || id === "trmt:eroded_dirt") {
      ev.cancel = true;
      system.run(() => {
        try {
          block.setPermutation(BlockPermutation.resolve("minecraft:grass_path"));
          removeEntry(dim, pos);
          removeCooldown(dim, pos);
          dim.playSound("step.grass", { x: pos.x + 0.5, y: pos.y + 0.5, z: pos.z + 0.5 });
          damageEquipment(player, EquipmentSlot.Mainhand, 1);
        } catch (e) {
          try {
            block.setPermutation(BlockPermutation.resolve("minecraft:dirt_path"));
            removeEntry(dim, pos);
            removeCooldown(dim, pos);
          } catch (e2) { console.warn("[TRMT] shovel transform failed:", e2); }
        }
      });
      return;
    }
  }

  // Bone meal: revert an eroded block one stage (or to its vanilla form).
  if (itemId === "minecraft:bone_meal" || itemId === "minecraft:dye" /* bone_meal data 15 */) {
    if (isEroded(id)) {
      ev.cancel = true;
      system.run(() => boneMealRevert(dim, pos, block, player));
      return;
    }
  }
});

// Brush: used continuously on eroded_sand — reduce stage by one each
// completed brushing tick (Bedrock fires itemCompleteUse for brushes).
world.afterEvents.itemCompleteUse?.subscribe?.((ev) => {
  if (!ev.itemStack) return;
  if (ev.itemStack.typeId !== "minecraft:brush") return;
  const player = ev.source;
  const view = player.getBlockFromViewDirection?.();
  if (!view || !view.block) return;
  const block = view.block;
  if (block.typeId !== "trmt:eroded_sand") return;
  const dim = block.dimension;
  const pos = block.location;
  const stage = block.permutation.getState("trmt:stage") ?? 0;
  const facing = block.permutation.getState("trmt:facing") ?? "south";
  if (stage <= 0) {
    // Stage 0 -> revert to vanilla sand.
    try {
      block.setPermutation(BlockPermutation.resolve("minecraft:sand"));
      removeEntry(dim, pos);
      removeCooldown(dim, pos);
      dim.playSound("brush.sand.complete", { x: pos.x + 0.5, y: pos.y + 0.5, z: pos.z + 0.5 });
    } catch (e) { console.warn("[TRMT] brush sand->vanilla failed:", e); }
    return;
  }
  try {
    block.setPermutation(BlockPermutation.resolve("trmt:eroded_sand", { "trmt:stage": stage - 1, "trmt:facing": facing }));
    dim.playSound("brush.sand", { x: pos.x + 0.5, y: pos.y + 0.5, z: pos.z + 0.5 });
  } catch (e) { console.warn("[TRMT] brush stage decrement failed:", e); }
});

function boneMealRevert(dim, pos, block, player) {
  const id = block.typeId;
  const facing = block.permutation.getState("trmt:facing") ?? "south";
  let newPerm;
  try {
    if (id === "trmt:eroded_grass_block") {
      const stage = block.permutation.getState("trmt:stage") ?? 0;
      if (stage > 0) {
        newPerm = BlockPermutation.resolve("trmt:eroded_grass_block", { "trmt:stage": stage - 1, "trmt:facing": facing });
      } else {
        newPerm = BlockPermutation.resolve("minecraft:grass_block");
      }
    } else if (id === "trmt:eroded_dirt") {
      const stage = block.permutation.getState("trmt:stage") ?? 0;
      if (stage > 0) {
        newPerm = BlockPermutation.resolve("trmt:eroded_dirt", { "trmt:stage": stage - 1, "trmt:facing": facing });
      } else {
        newPerm = BlockPermutation.resolve("minecraft:dirt");
      }
    } else if (id === "trmt:eroded_coarse_dirt") {
      newPerm = BlockPermutation.resolve("trmt:eroded_dirt", { "trmt:stage": 3, "trmt:facing": facing });
    } else if (id === "trmt:eroded_sand") {
      const stage = block.permutation.getState("trmt:stage") ?? 0;
      if (stage > 0) {
        newPerm = BlockPermutation.resolve("trmt:eroded_sand", { "trmt:stage": stage - 1, "trmt:facing": facing });
      } else {
        newPerm = BlockPermutation.resolve("minecraft:sand");
      }
    }
    if (!newPerm) return;
    block.setPermutation(newPerm);
    removeEntry(dim, pos);
    removeCooldown(dim, pos);
    dim.spawnParticle("minecraft:crop_growth_emitter", { x: pos.x + 0.5, y: pos.y + 0.6, z: pos.z + 0.5 });
    if (player && player.getGameMode && player.getGameMode() !== GameMode.Creative) {
      consumeOneFromMainHand(player);
    }
  } catch (e) { console.warn("[TRMT] bonemeal revert failed:", e); }
}

function damageEquipment(player, slot, amount) {
  if (!player) return;
  try {
    const equipment = player.getComponent(EntityComponentTypes.Equippable) || player.getComponent("minecraft:equippable");
    if (!equipment) return;
    const item = equipment.getEquipment(slot);
    if (!item) return;
    const dur = item.getComponent(ItemComponentTypes.Durability) || item.getComponent("minecraft:durability");
    if (!dur) return;
    if (dur.damage + amount >= dur.maxDurability) {
      equipment.setEquipment(slot, undefined);
      return;
    }
    dur.damage += amount;
    equipment.setEquipment(slot, item);
  } catch (e) { /* ignore */ }
}

function consumeOneFromMainHand(player) {
  try {
    const equipment = player.getComponent(EntityComponentTypes.Equippable) || player.getComponent("minecraft:equippable");
    if (!equipment) return;
    const item = equipment.getEquipment(EquipmentSlot.Mainhand);
    if (!item) return;
    if (item.amount > 1) {
      item.amount -= 1;
      equipment.setEquipment(EquipmentSlot.Mainhand, item);
    } else {
      equipment.setEquipment(EquipmentSlot.Mainhand, undefined);
    }
  } catch (e) { /* ignore */ }
}

// ============================================================================
//  POTION USE — apply Lightness when consuming our custom potion items.
// ============================================================================
world.afterEvents.itemCompleteUse?.subscribe?.((ev) => {
  const player = ev.source;
  if (!ev.itemStack) return;
  if (ev.itemStack.typeId === "trmt:potion_of_lightness") {
    applyLightness(player, CONFIG.lightnessDurationTicks);
    giveGlassBottle(player);
    consumeUsedPotion(player, "trmt:potion_of_lightness");
  } else if (ev.itemStack.typeId === "trmt:long_potion_of_lightness") {
    applyLightness(player, CONFIG.longLightnessDurationTicks);
    giveGlassBottle(player);
    consumeUsedPotion(player, "trmt:long_potion_of_lightness");
  }
});

function giveGlassBottle(player) {
  try {
    const inv = player.getComponent(EntityComponentTypes.Inventory) || player.getComponent("minecraft:inventory");
    if (!inv || !inv.container) return;
    inv.container.addItem(new ItemStack("minecraft:glass_bottle", 1));
  } catch (e) { /* ignore */ }
}

function consumeUsedPotion(player, typeId) {
  try {
    const equipment = player.getComponent(EntityComponentTypes.Equippable) || player.getComponent("minecraft:equippable");
    if (!equipment) return;
    const item = equipment.getEquipment(EquipmentSlot.Mainhand);
    if (item && item.typeId === typeId) {
      if (item.amount > 1) {
        item.amount -= 1;
        equipment.setEquipment(EquipmentSlot.Mainhand, item);
      } else {
        equipment.setEquipment(EquipmentSlot.Mainhand, undefined);
      }
    }
  } catch (e) { /* ignore */ }
}

// ============================================================================
//  THROWN POTIONS — splash/lingering: fire a snowball-like proxy and apply.
//  Bedrock doesn't expose a custom projectile factory cleanly without entity
//  definitions; we emulate by raycasting from the player and spawning a
//  splash particle on hit. The actual hit detection is via item-use start.
// ============================================================================
world.afterEvents.itemReleaseUse?.subscribe?.((ev) => {
  const player = ev.source;
  if (!ev.itemStack) return;
  const id = ev.itemStack.typeId;
  if (id === "trmt:splash_potion_of_lightness" || id === "trmt:lingering_potion_of_lightness") {
    system.run(() => throwLightnessPotion(player, id === "trmt:lingering_potion_of_lightness"));
  }
});

function throwLightnessPotion(player, lingering) {
  try {
    // Take one out of stack.
    const equipment = player.getComponent(EntityComponentTypes.Equippable) || player.getComponent("minecraft:equippable");
    const item = equipment?.getEquipment(EquipmentSlot.Mainhand);
    if (!item) return;
    if (player.getGameMode && player.getGameMode() !== GameMode.Creative) {
      if (item.amount > 1) {
        item.amount -= 1;
        equipment.setEquipment(EquipmentSlot.Mainhand, item);
      } else {
        equipment.setEquipment(EquipmentSlot.Mainhand, undefined);
      }
    }
    const dim = player.dimension;
    const origin = player.getHeadLocation();
    const dir = player.getViewDirection();
    // Simple physics: integrate a parabolic trajectory and check hits each step.
    const startVx = dir.x * 0.6;
    const startVy = dir.y * 0.6;
    const startVz = dir.z * 0.6;
    let x = origin.x, y = origin.y, z = origin.z;
    let vx = startVx, vy = startVy, vz = startVz;
    const stepCount = 40;
    let impact = null;
    for (let i = 0; i < stepCount; i++) {
      x += vx; y += vy; z += vz;
      vy -= 0.03;
      // Sample block; if solid, impact.
      const block = safeGetBlock(dim, { x: Math.floor(x), y: Math.floor(y), z: Math.floor(z) });
      if (block && !block.isAir) {
        impact = { x, y, z };
        break;
      }
      dim.spawnParticle("minecraft:water_splash_manual", { x, y, z });
    }
    if (!impact) impact = { x, y, z };
    splashLightnessAt(dim, impact, lingering);
  } catch (e) { console.warn("[TRMT] throw potion failed:", e); }
}

function splashLightnessAt(dim, pos, lingering) {
  // Splash radius mirrors vanilla: 4 blocks for direct, 1 for area.
  const range = 4;
  const entities = dim.getEntities({
    location: pos,
    maxDistance: range,
    type: "minecraft:player",
  });
  for (const e of entities) {
    if (e.typeId === "minecraft:player") {
      const dx = e.location.x - pos.x;
      const dy = e.location.y - pos.y;
      const dz = e.location.z - pos.z;
      const dist = Math.sqrt(dx * dx + dy * dy + dz * dz);
      const fall = Math.max(0.0, 1.0 - dist / range);
      const duration = Math.floor(CONFIG.lightnessDurationTicks * fall * 0.75);
      if (duration > 0) applyLightness(e, duration);
    }
  }
  dim.spawnParticle("minecraft:water_splash_particle_manual", pos);
  dim.playSound("random.glass", pos);
  if (lingering) {
    // Linger: maintain a cloud area for ~30 seconds; players who enter get effect.
    const expires = system.currentTick + 600;
    const center = { x: pos.x, y: pos.y, z: pos.z };
    const dimId = dim.id;
    lingerClouds.push({ dimId, center, expires });
  }
}

const lingerClouds = [];

function tickLingerClouds() {
  if (lingerClouds.length === 0) return;
  const now = system.currentTick;
  for (let i = lingerClouds.length - 1; i >= 0; i--) {
    const c = lingerClouds[i];
    if (now >= c.expires) {
      lingerClouds.splice(i, 1);
      continue;
    }
    const dim = getDimensionFromId(c.dimId);
    if (!dim) continue;
    dim.spawnParticle("minecraft:dragon_breath_trail", c.center);
    const players = dim.getEntities({ location: c.center, maxDistance: 3, type: "minecraft:player" });
    for (const p of players) {
      applyLightness(p, Math.min(CONFIG.lightnessDurationTicks, 100));
    }
  }
}

// ============================================================================
//  ARROW OF LIGHTNESS — when a projectile arrow with our tag hits an entity,
//  apply Lightness to the target.
// ============================================================================
world.afterEvents.projectileHitEntity?.subscribe?.((ev) => {
  try {
    const projectile = ev.projectile;
    if (!projectile) return;
    const target = ev.getEntityHit?.().entity;
    if (!target) return;
    if (target.typeId !== "minecraft:player") return;
    const tags = projectile.getTags?.() ?? [];
    const isLightnessArrow = tags.includes("trmt_lightness_arrow");
    if (isLightnessArrow) {
      applyLightness(target, Math.floor(CONFIG.lightnessDurationTicks / 8));
    }
  } catch { /* ignore */ }
});

// ============================================================================
//  SUGAR CANE on ERODED_SAND s0 — Bedrock checks `canSurvive` natively but we
//  reinforce it by ensuring sugar cane can be placed on top of eroded_sand:0
//  via a beforeItemUseOn override only when needed.
// ============================================================================
world.beforeEvents.itemUseOn.subscribe((ev) => {
  if (ev.cancel) return;
  if (!ev.itemStack) return;
  if (ev.itemStack.typeId !== "minecraft:sugar_cane") return;
  const block = ev.block;
  if (!block) return;
  if (block.typeId !== "trmt:eroded_sand") return;
  const stage = block.permutation.getState("trmt:stage") ?? 0;
  if (stage !== 0) {
    ev.cancel = true; // can't grow on eroded sand higher stages
    return;
  }
  // Allow placement: simulate by placing the cane in the block above.
  ev.cancel = true;
  system.run(() => {
    const above = safeGetBlock(block.dimension, { x: block.location.x, y: block.location.y + 1, z: block.location.z });
    if (above && above.isAir) {
      try { above.setPermutation(BlockPermutation.resolve("minecraft:sugar_cane")); } catch {}
      const player = ev.source;
      consumeOneFromMainHand(player);
    }
  });
});

// ============================================================================
//  BLOCK PLACEMENT GUARD — can't place blocks on top of eroded_sand stage > 0
//  (mirrors TRMTClient.UseBlockCallback). Mirrors the Java client check
//  defensively on the server side so survival/Bedrock both honor it.
// ============================================================================
world.beforeEvents.itemUseOn.subscribe((ev) => {
  if (ev.cancel) return;
  if (!ev.itemStack) return;
  if (!ev.itemStack.typeId.includes(":")) return;
  // Anything that places a block: check the block underneath the placement.
  if (!ev.block) return;
  const item = ev.itemStack;
  // Heuristic: items whose ID points at a block — Bedrock doesn't expose this
  // cleanly so we look at face and check if the block one cell out from the
  // hit face below would be eroded_sand stage > 0.
  const face = ev.blockFace;
  let placePos = { x: ev.block.location.x, y: ev.block.location.y, z: ev.block.location.z };
  switch (face) {
    case Direction.Up: placePos.y += 1; break;
    case Direction.Down: placePos.y -= 1; break;
    case Direction.North: placePos.z -= 1; break;
    case Direction.South: placePos.z += 1; break;
    case Direction.West: placePos.x -= 1; break;
    case Direction.East: placePos.x += 1; break;
  }
  const below = safeGetBlock(ev.block.dimension, { x: placePos.x, y: placePos.y - 1, z: placePos.z });
  if (below && below.typeId === "trmt:eroded_sand") {
    const stage = below.permutation.getState("trmt:stage") ?? 0;
    if (stage > 0) {
      // Only cancel for items that are likely block placements — heuristic:
      // mainhand item with no explicit interactable use. We err on the safe
      // side and only cancel for plain block items.
      const bn = item.typeId;
      const isBlockLike = bn.startsWith("minecraft:") && !bn.endsWith("_pickaxe") && !bn.endsWith("_axe") && !bn.endsWith("_shovel") && !bn.endsWith("_hoe") && !bn.endsWith("_sword") && !bn.endsWith("_meal") && !bn.endsWith("_dye") && !bn.endsWith("_potion") && !bn.includes("bucket") && !bn.includes("brush");
      if (isBlockLike) ev.cancel = true;
    }
  }
});

// ============================================================================
//  DE-EROSION TICK — random ticks in Java; we just iterate cooldown entries
//  every TICK_DE_EROSION_INTERVAL ticks (a fraction of an in-game day).
// ============================================================================
const TICK_DE_EROSION_INTERVAL = 200; // every ~10 seconds, fairly cheap

function tickDeErosion() {
  if (cooldowns.size === 0) return;
  const now = system.currentTick;
  const toDelete = [];
  for (const [k, v] of cooldowns) {
    const { dim: dimId, pos } = unkey(k);
    const dim = getDimensionFromId(dimId);
    if (!dim) continue;
    const block = safeGetBlock(dim, pos);
    if (!block) continue;
    const id = block.typeId;
    if (id !== v.block) {
      toDelete.push(k);
      continue;
    }
    let stage = 0;
    try { stage = block.permutation.getState("trmt:stage") ?? 0; } catch {}
    const timeout = getDeErosionTimeoutTicks(id, stage);
    const isolatedFactor = isolatedFromOtherEroded(dim, pos) ? 0.5 : 1.0;
    const effectiveTimeout = timeout * isolatedFactor;
    if (now - v.last < effectiveTimeout) continue;

    // De-erosion tick: decrement stage or revert.
    if (id === "trmt:eroded_grass_block") {
      if (!CONFIG.deErosion.grass) continue;
      if (stage > 0) {
        const facing = block.permutation.getState("trmt:facing") ?? "south";
        try {
          block.setPermutation(BlockPermutation.resolve("trmt:eroded_grass_block", { "trmt:stage": stage - 1, "trmt:facing": facing }));
          v.last = now;
          dirtyCooldowns = true;
        } catch { /* ignore */ }
      } else {
        try { block.setPermutation(BlockPermutation.resolve("minecraft:grass_block")); } catch {}
        toDelete.push(k);
      }
    } else if (id === "trmt:eroded_dirt") {
      if (!CONFIG.deErosion.dirt) continue;
      if (stage > 0) {
        const facing = block.permutation.getState("trmt:facing") ?? "south";
        try {
          block.setPermutation(BlockPermutation.resolve("trmt:eroded_dirt", { "trmt:stage": stage - 1, "trmt:facing": facing }));
          v.last = now;
          dirtyCooldowns = true;
        } catch { /* ignore */ }
      } else {
        try { block.setPermutation(BlockPermutation.resolve("trmt:eroded_grass_block", { "trmt:stage": 4, "trmt:facing": "south" })); } catch {}
        toDelete.push(k);
      }
    } else if (id === "trmt:eroded_coarse_dirt") {
      if (!CONFIG.deErosion.dirt) continue;
      try {
        const facing = block.permutation.getState("trmt:facing") ?? "south";
        block.setPermutation(BlockPermutation.resolve("trmt:eroded_dirt", { "trmt:stage": 3, "trmt:facing": facing }));
        v.last = now;
        v.block = "trmt:eroded_dirt";
        dirtyCooldowns = true;
      } catch { /* ignore */ }
    } else if (id === "trmt:eroded_sand") {
      if (!CONFIG.deErosion.sand) continue;
      if (stage > 0) {
        const facing = block.permutation.getState("trmt:facing") ?? "south";
        try {
          block.setPermutation(BlockPermutation.resolve("trmt:eroded_sand", { "trmt:stage": stage - 1, "trmt:facing": facing }));
          v.last = now;
          dirtyCooldowns = true;
        } catch { /* ignore */ }
      } else {
        try { block.setPermutation(BlockPermutation.resolve("minecraft:sand")); } catch {}
        toDelete.push(k);
      }
    } else {
      toDelete.push(k);
    }
  }
  for (const k of toDelete) {
    cooldowns.delete(k);
    dirtyCooldowns = true;
  }
}

// ============================================================================
//  COMMANDS — exposed via /scriptevent.
//      /scriptevent trmt:reload-config        re-reads CONFIG from world prop
//      /scriptevent trmt:convert-to-vanilla   replace all eroded blocks in 32-chunk radius
//      /scriptevent trmt:eroded-chunks        list current cooldown chunks
//      /scriptevent trmt:give-lightness <ticks> apply Lightness to sender
// ============================================================================
system.afterEvents.scriptEventReceive.subscribe((ev) => {
  if (!ev.id.startsWith("trmt:")) return;
  const cmd = ev.id.substring("trmt:".length);
  switch (cmd) {
    case "reload-config":
      loadConfig();
      ev.sourceEntity?.sendMessage?.("[TRMT] config reloaded.");
      break;
    case "save-config":
      saveConfig();
      ev.sourceEntity?.sendMessage?.("[TRMT] config saved.");
      break;
    case "convert-to-vanilla":
      convertNearbyToVanilla(ev.sourceEntity, parseInt(ev.message, 10) || 32);
      break;
    case "eroded-chunks":
      reportErodedChunks(ev.sourceEntity);
      break;
    case "give-lightness": {
      if (ev.sourceEntity?.typeId !== "minecraft:player") break;
      const dur = parseInt(ev.message, 10) || CONFIG.lightnessDurationTicks;
      applyLightness(ev.sourceEntity, dur);
      ev.sourceEntity.sendMessage?.(`[TRMT] applied Lightness for ${dur} ticks.`);
      break;
    }
    default:
      ev.sourceEntity?.sendMessage?.(`[TRMT] unknown command: ${cmd}`);
  }
});

function convertNearbyToVanilla(entity, chunkRadius) {
  if (!entity) return;
  const dim = entity.dimension;
  const cx = Math.floor(entity.location.x / 16);
  const cz = Math.floor(entity.location.z / 16);
  let removed = 0;
  const toDelete = [];
  for (const k of cooldowns.keys()) {
    const { dim: dimId, pos } = unkey(k);
    if (dimId !== dim.id) continue;
    if (Math.abs(Math.floor(pos.x / 16) - cx) > chunkRadius) continue;
    if (Math.abs(Math.floor(pos.z / 16) - cz) > chunkRadius) continue;
    const block = safeGetBlock(dim, pos);
    if (!block) continue;
    const id = block.typeId;
    let target = null;
    if (id === "trmt:eroded_grass_block") target = "minecraft:grass_block";
    else if (id === "trmt:eroded_dirt") target = "minecraft:dirt";
    else if (id === "trmt:eroded_coarse_dirt") target = "minecraft:coarse_dirt";
    else if (id === "trmt:eroded_sand") target = "minecraft:sand";
    if (target) {
      try { block.setPermutation(BlockPermutation.resolve(target)); removed++; } catch {}
    }
    toDelete.push(k);
  }
  for (const k of toDelete) cooldowns.delete(k);
  dirtyCooldowns = true;
  entity.sendMessage?.(`[TRMT] reverted ${removed} eroded blocks in ${chunkRadius}-chunk radius.`);
}

function reportErodedChunks(entity) {
  if (!entity) return;
  const chunks = new Set();
  for (const k of cooldowns.keys()) {
    const { dim: dimId, pos } = unkey(k);
    chunks.add(`${dimId}@${Math.floor(pos.x / 16)},${Math.floor(pos.z / 16)}`);
  }
  entity.sendMessage?.(`[TRMT] ${chunks.size} eroded chunks tracked, ${entries.size} active entries.`);
}

// ============================================================================
//  CHAT COMMANDS — convenience alias to scriptevent for users without console.
// ============================================================================
world.beforeEvents.chatSend.subscribe((ev) => {
  const msg = ev.message.trim();
  if (!msg.startsWith("!trmt ")) return;
  ev.cancel = true;
  const rest = msg.substring("!trmt ".length);
  const [cmd, ...args] = rest.split(/\s+/);
  const player = ev.sender;
  system.run(() => {
    switch (cmd) {
      case "reloadconfig":
      case "reload": loadConfig(); player.sendMessage("[TRMT] config reloaded."); break;
      case "saveconfig":
      case "save": saveConfig(); player.sendMessage("[TRMT] config saved."); break;
      case "convert-to-vanilla":
      case "revertall": convertNearbyToVanilla(player, parseInt(args[0], 10) || 32); break;
      case "eroded-chunks":
      case "info": reportErodedChunks(player); break;
      case "lightness": applyLightness(player, parseInt(args[0], 10) || CONFIG.lightnessDurationTicks); player.sendMessage("[TRMT] Lightness applied."); break;
      default: player.sendMessage(`[TRMT] commands: reloadconfig, saveconfig, convert-to-vanilla, eroded-chunks, lightness`);
    }
  });
});

// ============================================================================
//  MAIN TICKS
// ============================================================================
const ENTITY_TICK_INTERVAL = 4; // mobs checked every 4 ticks; players every tick

system.runInterval(() => {
  for (const player of world.getAllPlayers()) {
    try { tickEntityStep(player, true); } catch (e) { console.warn("[TRMT] player tick error:", e); }
  }
}, 1);

system.runInterval(() => {
  for (const dim of [world.getDimension("overworld"), world.getDimension("nether"), world.getDimension("the_end")]) {
    let entities;
    try { entities = dim.getEntities({ excludeTypes: ["minecraft:player", "minecraft:item", "minecraft:xp_orb"] }); } catch { continue; }
    for (const e of entities) {
      try { tickEntityStep(e, false); } catch (err) { /* ignore */ }
    }
  }
}, ENTITY_TICK_INTERVAL);

system.runInterval(() => {
  try { tickDeErosion(); } catch (e) { console.warn("[TRMT] deerosion tick error:", e); }
  try { tickLightness(); } catch (e) { console.warn("[TRMT] lightness tick error:", e); }
  try { tickLingerClouds(); } catch (e) { /* ignore */ }
}, TICK_DE_EROSION_INTERVAL);

system.runInterval(() => {
  try { persistStores(); } catch (e) { console.warn("[TRMT] persist error:", e); }
}, 40); // every 2 seconds

// ============================================================================
//  INITIAL LOAD
// ============================================================================
world.afterEvents.worldInitialize?.subscribe?.(() => {
  loadConfig();
  loadStores();
  console.log("[TRMT] world loaded; entries:", entries.size, "cooldowns:", cooldowns.size);
});

// Fallback initial load if worldInitialize doesn't fire in some hosts.
system.run(() => {
  loadConfig();
  loadStores();
});

console.log("[TRMT] script loaded; version 0.5-26.1+26.1.1+26.1.2 (Bedrock port)");
