-- PoE2 S4/T3.4 Deep Audit Probe: conversion dead-mods, Tabulate attribution, ArmourRatio=10 ladder,
-- Deflection 95% illusion vs raw MaxHit, 50% Block cap, Crossbow EffectiveReloadTime, Chaos 2x ES / CI lock,
-- Pinnacle Stun/Freeze/Electrocute buildup, [PoB2未模拟] red cards, and WS1/WS2 swap audit.
local calcs = require("Modules.Calcs")
local H = require("pob_host")
local json_obj, now_ms, get_build = H.json_obj, H.now_ms, H.get_build
local M = {}

local DMG_TYPES = { "Physical", "Lightning", "Cold", "Fire", "Chaos" }
local HIT_LADDER = { 1000, 5000, 10000, 20000 }
local RESISTS = { "Fire", "Cold", "Lightning", "Chaos" }
local ATTRS = { "Str", "Dex", "Int" }

local UNSIMULATED_PATTERNS = {
	{ match = "tempest bell", issue = "#2123", name = "Tempest Bell",
	  reason = "PoB2 does not simulate Bell strike cadence, combo hit rate, or shockwave overlap." },
	{ match = "infernal cry", issue = "#714", name = "Infernal Cry",
	  reason = "PoB2 does not simulate Infernal Cry exert cadence or combust explosion overlap." },
	{ match = "ember fusillade", issue = "#1850", name = "Ember Fusillade",
	  reason = "PoB2 does not simulate multi-projectile volley delay / overlapping shotgunning." },
	{ match = "cast on critical", issue = "#181/#2166", name = "Cast on Critical (Meta Energy)",
	  reason = "PoB2 does not simulate PoE2 Meta Energy generation / trigger rate." },
	{ match = "cast on freeze", issue = "#181/#2166", name = "Cast on Freeze (Meta Energy)",
	  reason = "PoB2 does not simulate PoE2 Meta Energy generation / trigger rate." },
	{ match = "cast on shock", issue = "#181/#2166", name = "Cast on Shock (Meta Energy)",
	  reason = "PoB2 does not simulate PoE2 Meta Energy generation / trigger rate." },
	{ match = "cast on ignite", issue = "#181/#2166", name = "Cast on Ignite (Meta Energy)",
	  reason = "PoB2 does not simulate PoE2 Meta Energy generation / trigger rate." },
	{ match = "cast on minion death", issue = "#181/#2166", name = "Cast on Minion Death (Meta Energy)",
	  reason = "PoB2 does not simulate PoE2 Meta Energy generation / trigger rate." },
	{ match = "cast on dodge", issue = "#181/#2166", name = "Cast on Dodge (Meta Energy)",
	  reason = "PoB2 does not simulate PoE2 Meta Energy generation / trigger rate." },
	{ match = "invocation", issue = "#181/#2166", name = "Meta Invocation",
	  reason = "PoB2 does not simulate Meta Invocation Energy accumulation or release cadence." },
}

local function primary_dps(out)
	if (out.FullDPS or 0) > 0 then return out.FullDPS, "FullDPS" end
	return out.CombinedDPS or out.TotalDPS or 0, "CombinedDPS"
end

local function armour_red_pct(armour, hit, cap, ratio)
	if not armour or armour <= 0 then return 0 end
	return math.min(cap or 90, math.max(0, (armour / (armour + hit * ratio)) * 100))
end

local function scan_conversion_and_tabulate(env)
	local sk = env and env.player and env.player.mainSkill
	local conv_out, dead_mods, attr_rows = json_obj({}), {}, {}
	if not sk then return conv_out, dead_mods, attr_rows end

	local ctbl, fully_conv = sk.conversionTable or {}, {}
	for _, dt in ipairs(DMG_TYPES) do
		local c = ctbl[dt]
		if type(c) == "table" then
			local mult, dests = tonumber(c.mult) or 1, json_obj({})
			for _, to_dt in ipairs(DMG_TYPES) do
				local frac = tonumber(c[to_dt]) or 0
				if frac > 0 then dests[to_dt] = math.floor(frac * 1000 + 0.5) / 10 end
			end
			conv_out[dt] = { unconvertedPct = math.floor(mult * 1000 + 0.5) / 10, convertedTo = dests }
			if mult <= 1e-4 and next(dests) ~= nil then fully_conv[dt] = dests end
		end
	end

	local sml, cfg = sk.skillModList, sk.skillCfg
	if sml and type(sml.Tabulate) == "function" then
		for src_dt, dests in pairs(fully_conv) do
			local mod_name = src_dt .. "Damage"
			local tot_i, tot_m, srcs = 0, 0, {}
			for _, mt in ipairs({ "INC", "MORE" }) do
				local ok, rows = pcall(sml.Tabulate, sml, mt, cfg, mod_name)
				if ok and type(rows) == "table" then
					for _, e in ipairs(rows) do
						local v = tonumber(e.value) or 0
						if v > 0 then
							if mt == "INC" then tot_i = tot_i + v else tot_m = tot_m + v end
							srcs[#srcs + 1] = { type = mt, value = v, source = tostring((e.mod and e.mod.source) or "Unknown") }
						end
					end
				end
			end
			if tot_i > 0 or tot_m > 0 then
				dead_mods[#dead_mods + 1] = {
					sourceDamageType = src_dt, convertedTo = dests,
					wastedIncPct = tot_i, wastedMorePct = tot_m, sources = srcs,
					rule = "PoE2 two-step terminal conversion forgets source damage type tags (CalcOffence.lua:110, 2319).",
				}
			end
		end

		local by_src = {}
		local names = { "Damage", "PhysicalDamage", "ElementalDamage", "FireDamage", "ColdDamage", "LightningDamage", "ChaosDamage" }
		for _, mt in ipairs({ "INC", "MORE" }) do
			local ok, rows = pcall(sml.Tabulate, sml, mt, cfg, unpack(names))
			if ok and type(rows) == "table" then
				for _, e in ipairs(rows) do
					local v = tonumber(e.value) or 0
					if v ~= 0 then
						local src = tostring((e.mod and e.mod.source) or "Unknown")
						local nm = tostring((e.mod and e.mod.name) or "Damage")
						local k = mt .. "|" .. nm .. "|" .. src
						local r = by_src[k]
						if not r then r = { modType = mt, stat = nm, source = src, value = 0 }; by_src[k] = r end
						r.value = r.value + v
					end
				end
			end
		end
		for _, r in pairs(by_src) do attr_rows[#attr_rows + 1] = r end
		table.sort(attr_rows, function(a, b) return math.abs(a.value) > math.abs(b.value) end)
		while #attr_rows > 15 do table.remove(attr_rows) end
	end
	return conv_out, dead_mods, attr_rows
end

local function scan_armour_ladder(env, output)
	local mod_db = env and env.player and env.player.modDB
	local ratio = (data and data.misc and data.misc.ArmourRatio) or 10
	local armour, phys_cap = tonumber(output.Armour) or 0, tonumber(output.PhysicalDamageReductionMax) or 90
	local phys_ladder = {}
	for _, hit in ipairs(HIT_LADDER) do
		phys_ladder[#phys_ladder + 1] = { hitDamage = hit, mitigationPct = math.floor(armour_red_pct(armour, hit, phys_cap, ratio) * 10 + 0.5) / 10 }
	end
	local ele_ladders, has_ele = json_obj({}), false
	if mod_db and armour > 0 then
		for _, elem in ipairs({ "Fire", "Cold", "Lightning" }) do
			local pct = mod_db:Sum(
				"BASE",
				nil,
				"ArmourAppliesTo" .. elem .. "DamageTaken",
				"ArmourAppliesTo" .. elem .. "Damage",
				"ArmourAppliesToElementalDamage"
			) or 0
			local eff = armour * pct / 100
			if eff > 0 then
				has_ele = true
				local ecap, rows = tonumber(output[elem .. "DamageReductionMax"]) or 90, {}
				for _, hit in ipairs(HIT_LADDER) do
					rows[#rows + 1] = { hitDamage = hit, preResistMitigationPct = math.floor(armour_red_pct(eff, hit, ecap, ratio) * 10 + 0.5) / 10 }
				end
				ele_ladders[elem] = { appliedPct = pct, effectiveArmour = math.floor(eff + 0.5), ladder = rows }
			end
		end
	end
	return {
		armour = armour, armourRatio = ratio, physReductionCap = phys_cap,
		physicalLadder = phys_ladder, hasElementalArmour = has_ele, elementalLadders = ele_ladders,
		tooltipErrata = "CalcDefence.lua:2400 computes elemental armour against PRE-resistance raw hit with ArmourRatio=10, despite CalcDefence.lua:2433 mislabelling it 'After Resistance'.",
	}
end

local function scan_unsimulated_skills(build)
	local hits, seen = {}, {}
	for gi, grp in ipairs((build.skillsTab and build.skillsTab.socketGroupList) or {}) do
		if grp and grp.enabled ~= false then
			for _, gem in ipairs(grp.gemList or {}) do
				if gem and gem.enabled ~= false then
					local nm = tostring(gem.nameSpec or (gem.gemData and gem.gemData.name) or "")
					local low = nm:lower()
					for _, pat in ipairs(UNSIMULATED_PATTERNS) do
						if low:find(pat.match, 1, true) and not seen[pat.issue .. "|" .. nm] then
							seen[pat.issue .. "|" .. nm] = true
							hits[#hits + 1] = { tag = "[PoB2未模拟]", issue = pat.issue, skillName = nm, groupIndex = gi, label = pat.name, reason = pat.reason }
						end
					end
				end
			end
		end
	end
	return hits
end

local function scan_bonded_runes(build, mod_db)
	local spec = build and build.spec
	local asc_name = tostring((spec and spec.curAscendClassName) or "")
	local is_shaman = asc_name:lower() == "shaman"
	local can_use_bonded = (mod_db and mod_db:Flag(nil, "Condition:CanUseBondedModifiers")) and true or false
	local affected = {}
	local itab = build and build.itemsTab
	local rune_db = data and data.itemMods and data.itemMods.Runes or {}
	local seen_ids = {}
	for slot_name, sl in pairs((itab and itab.slots) or {}) do
		local iid = sl and tonumber(sl.selItemId) or 0
		if iid > 0 and not seen_ids[iid] then
			seen_ids[iid] = true
			local it = itab.items and itab.items[iid]
			if type(it) == "table" then
				local bonded_lines, runes_with_bonded = {}, {}
				for _, ml in ipairs(it.runeModLines or {}) do
					local ln = type(ml) == "table" and tostring(ml.line or "") or ""
					if (type(ml) == "table" and ml.bonded) or ln:find("Bonded:", 1, true) then
						bonded_lines[#bonded_lines + 1] = ln
					end
				end
				if #bonded_lines == 0 and type(it.raw) == "string" and it.raw:find("Bonded:", 1, true) then
					for line in (it.raw:gsub("\r", "") .. "\n"):gmatch("([^\n]*)\n") do
						if line:find("Bonded:", 1, true) then bonded_lines[#bonded_lines + 1] = line end
					end
				end
				for _, rname in ipairs(it.runes or {}) do
					local rentry = rune_db[rname]
					if type(rentry) == "table" then
						local has_b = false
						for _, cat in pairs(rentry) do
							if type(cat) == "table" then
								for _, ml in ipairs(cat) do
									if type(ml) == "string" and ml:find("Bonded:", 1, true) then
										has_b = true
										break
									end
								end
							end
							if has_b then break end
						end
						if has_b then runes_with_bonded[#runes_with_bonded + 1] = rname end
					end
				end
				if #bonded_lines > 0 or #runes_with_bonded > 0 then
					affected[#affected + 1] = {
						slot = slot_name,
						itemId = iid,
						itemName = it.name or it.title or "Item",
						runes = runes_with_bonded,
						bondedLines = bonded_lines,
					}
				end
			end
		end
	end
	table.sort(affected, function(a, b) return tostring(a.slot) < tostring(b.slot) end)
	return {
		ascendClassName = asc_name,
		isShaman = is_shaman,
		canUseBonded = can_use_bonded,
		affectedItemCount = #affected,
		affectedItems = affected,
	}
end

local function check_weapon_set_swap_gaps(build)
	local itab = build.itemsTab
	local aset = itab and itab.activeItemSet
	if type(aset) ~= "table" then return { checked = false } end
	local orig_sw = aset.useSecondWeaponSet and true or false

	local function slot_item_id(sn)
		local sl = itab.slots and itab.slots[sn]
		if sl and tonumber(sl.selItemId) and tonumber(sl.selItemId) > 0 then return tonumber(sl.selItemId) end
		local ss = aset[sn]
		if type(ss) == "table" and tonumber(ss.selItemId) and tonumber(ss.selItemId) > 0 then return tonumber(ss.selItemId) end
		return 0
	end
	local ws1_has_item = (slot_item_id("Weapon 1") > 0) or (slot_item_id("Weapon 2") > 0)
	local ws2_has_item = (slot_item_id("Weapon 1 Swap") > 0) or (slot_item_id("Weapon 2 Swap") > 0)
	local ws1_nodes, ws2_nodes = 0, 0
	if build.spec and type(build.spec.CountAllocNodes) == "function" then
		local _, _, _, _, u1, u2 = build.spec:CountAllocNodes()
		ws1_nodes, ws2_nodes = tonumber(u1) or 0, tonumber(u2) or 0
	end
	local ws2_excl_skill = false
	for _, grp in ipairs((build.skillsTab and build.skillsTab.socketGroupList) or {}) do
		if grp and grp.enabled ~= false then
			local sn = tostring(grp.slot or "")
			if sn:find("Swap", 1, true) or (grp.set2 == true and grp.set1 == false) then
				ws2_excl_skill = true
				break
			end
		end
	end
	local ws1_pop = ws1_has_item or (ws1_nodes > 0)
	local ws2_pop = ws2_has_item or (ws2_nodes > 0) or ws2_excl_skill

	local function measure(use_second)
		aset.useSecondWeaponSet = use_second
		local res_env = calcs.buildOutput(build, "MAIN")
		local out = (res_env and res_env.player and res_env.player.output) or {}
		local deficits = {}
		for _, a in ipairs(ATTRS) do
			local cur, req = tonumber(out[a]) or 0, tonumber(out["Req" .. a]) or 0
			if cur < req then deficits[a] = { current = cur, required = req, deficit = cur - req } end
		end
		local sp_tot = tonumber(out.Spirit) or 0
		local sp_unres = tonumber(out.SpiritUnreserved) or 0
		local active_auras = 0
		for _, sk in ipairs((res_env and res_env.player and res_env.player.activeSkillList) or {}) do
			local ge = sk and sk.activeEffect and sk.activeEffect.grantedEffect
			if sk and sk.skillFlags and (sk.skillFlags.aura or sk.skillFlags.buffs or (ge and ge.statSet and ge.statSet.constantStats)) then
				if (sk.skillData and (tonumber(sk.skillData.spiritReservationFlat) or 0) > 0)
					or (sk.activeEffect and sk.activeEffect.grantedEffect and sk.activeEffect.grantedEffect.spiritReservationFlat) then
					active_auras = active_auras + 1
				end
			end
		end
		return {
			weaponSet = use_second and 2 or 1,
			populated = use_second and ws2_pop or ws1_pop,
			spirit = sp_tot,
			spiritUnreserved = sp_unres,
			spiritReserved = sp_tot - sp_unres,
			spiritNegative = sp_unres < 0,
			activeAuraCount = active_auras,
			attrDeficits = json_obj(deficits),
			hasAttrDeficit = next(deficits) ~= nil,
			dps = primary_dps(out),
		}
	end
	local ok, ws1, ws2 = pcall(function() return measure(false), measure(true) end)
	aset.useSecondWeaponSet = orig_sw
	if type(wipeGlobalCache) == "function" then wipeGlobalCache() end
	build.calcsTab:BuildOutput()
	if not ok then return { checked = false, error = tostring(ws1) } end
	local cur_ws = orig_sw and ws2 or ws1
	local oth_ws = orig_sw and ws1 or ws2
	local oth_pop = orig_sw and ws1_pop or ws2_pop
	local new_attr = false
	if oth_pop then
		for _, a in ipairs(ATTRS) do
			local od = oth_ws.attrDeficits[a]
			local cd = cur_ws.attrDeficits[a]
			if od and (not cd or math.abs(od.deficit) > math.abs(cd.deficit)) then
				new_attr = true
			end
		end
	end
	local spirit_drop_deficit = oth_pop and (
		(not cur_ws.spiritNegative and oth_ws.spiritNegative)
		or (cur_ws.spiritNegative and oth_ws.spiritUnreserved < cur_ws.spiritUnreserved)
	)
	local disables_aura = oth_pop and (
		(cur_ws.activeAuraCount > oth_ws.activeAuraCount)
		or (cur_ws.spiritReserved > 0 and oth_ws.spiritReserved < cur_ws.spiritReserved and oth_ws.spirit < cur_ws.spirit)
	)
	return {
		checked = true, activeWeaponSet = orig_sw and 2 or 1,
		ws1Populated = ws1_pop, ws2Populated = ws2_pop, ws2Empty = not ws2_pop,
		ws1 = ws1, ws2 = ws2,
		hasSwapSpiritDrop = ws1.spiritNegative or ws2.spiritNegative,
		hasSwapAttrRedText = ws1.hasAttrDeficit or ws2.hasAttrDeficit,
		introducesNewAttrDeficit = new_attr,
		introducesSpiritDeficit = spirit_drop_deficit,
		disablesAuraOnSwap = disables_aura,
	}
end

local function handle_deep_audit_probe(_params)
	local build, t0 = get_build(), now_ms()
	local env = build.calcsTab and build.calcsTab.mainEnv
	local output = (env and env.player and env.player.output) or {}
	local mod_db = env and env.player and env.player.modDB
	local conv_tbl, dead_mods, tab_attr = scan_conversion_and_tabulate(env)

	local defl_ch = tonumber(output.DeflectChance) or tonumber(output.DeflectionChance) or 0
	local defl_eff = tonumber(output.DeflectEffect) or ((data and data.misc and data.misc.DeflectEffect) or 40)
	local defl_cap = (data and data.misc and data.misc.DeflectionChanceCap) or 95
	local defl_mult = (defl_ch < 100) and (1 - defl_ch * defl_eff / 10000) or 1

	local life, es = tonumber(output.Life) or 0, tonumber(output.EnergyShield) or 0
	local is_ci = (life <= 1 and es > 0) or (mod_db and mod_db:Flag(nil, "ChaosInoculation") or false)
	local chaos_no_2x = mod_db and mod_db:Flag(nil, "ChaosNotDoubleESDamage") or false
	local stun_es = mod_db and ((mod_db:Sum("BASE", nil, "StunThresholdFromES") or 0) > 0 or mod_db:Flag(nil, "StunThresholdUsesES") or mod_db:Flag(nil, "CannotBeStunned")) or false
	local freeze_es = mod_db and ((mod_db:Sum("BASE", nil, "FreezeThresholdFromES") or 0) > 0 or mod_db:Flag(nil, "FreezeThresholdUsesES") or mod_db:Flag(nil, "CannotBeFrozen")) or false

	local avg_hit = tonumber(output.AverageDamage) or 0
	local cold_hit = tonumber(output.ColdAverage) or ((tonumber(output.ColdMin) or 0) + (tonumber(output.ColdMax) or 0)) / 2
	local light_hit = tonumber(output.LightningAverage) or ((tonumber(output.LightningMin) or 0) + (tonumber(output.LightningMax) or 0)) / 2
	local phys_hit = tonumber(output.PhysicalAverage) or ((tonumber(output.PhysicalMin) or 0) + (tonumber(output.PhysicalMax) or 0)) / 2
	local pin_thr = 3000000

	local has_utopia = false
	for _, nd in pairs((build.spec and build.spec.allocNodes) or {}) do
		if type(nd) == "table" and tostring(nd.dn or ""):lower():find("utopia", 1, true) then has_utopia = true end
	end
	local active_charms = {}
	for i = 1, 3 do
		local sl = build.itemsTab and build.itemsTab.slots and build.itemsTab.slots["Charm " .. i]
		if sl and sl.selItemId and sl.selItemId > 0 then
			local it = build.itemsTab.items and build.itemsTab.items[sl.selItemId]
			active_charms[#active_charms + 1] = { slot = "Charm " .. i, name = it and (it.title or it.name) or "Charm", active = sl.active and true or false }
		end
	end

	local rt, fr, spd = tonumber(output.ReloadTime), tonumber(output.FiringRate), tonumber(output.Speed) or 0
	local crossbow = (rt and rt > 0) and {
		reloadTime = rt, effectiveReloadTime = tonumber(output.EffectiveReloadTime) or rt,
		firingRate = fr or spd, effectiveCycleRate = spd,
		boltCount = tonumber(output.BoltCount) or 1,
		effectiveBoltCount = tonumber(output.EffectiveBoltCount) or tonumber(output.BoltCount) or 1,
		reloadDpsLossPct = (fr and fr > 0) and (math.floor((1 - spd / fr) * 1000 + 0.5) / 10) or 0,
	} or nil

	local resists, max_cap = json_obj({}), (data and data.misc and data.misc.MaxResistCap) or 90
	for _, elem in ipairs(RESISTS) do
		local is_ele = elem ~= "Chaos"
		local cap = mod_db and (mod_db:Override(nil, elem .. "ResistMax") or math.min(max_cap, mod_db:Sum("BASE", nil, elem .. "ResistMax", is_ele and "ElementalResistMax" or nil))) or 75
		local tot = tonumber(output[elem .. "ResistTotal"]) or tonumber(output[elem .. "Resist"]) or 0
		local fin = tonumber(output[elem .. "Resist"]) or 0
		resists[elem] = { final = fin, total = tot, cap = math.floor(cap), overcapBuffer = tot - math.floor(cap), capped = fin >= math.floor(cap) }
	end

	return {
		game = "poe2",
		pob2Version = (type(launch) == "table" and launch.versionNumber) or "unknown",
		treeVersion = build.spec and build.spec.treeVersion or "unknown",
		conversion = { table = conv_tbl, deadMods = dead_mods },
		tabulateAttribution = tab_attr,
		armour = scan_armour_ladder(env, output),
		deflection = {
			rating = tonumber(output.DeflectionRating) or 0, chance = defl_ch, chanceCap = defl_cap,
			effectPct = defl_eff, expectedMultiplier = math.floor(defl_mult * 10000 + 0.5) / 10000,
			totalEhp = tonumber(output.TotalEHP) or 0,
			rawPhysMaxHit = tonumber(output.PhysicalMaximumHitTaken) or 0,
			rawEleMinMaxHit = math.min(tonumber(output.FireMaximumHitTaken) or 0, tonumber(output.ColdMaximumHitTaken) or 0, tonumber(output.LightningMaximumHitTaken) or 0),
			rawChaosMaxHit = tonumber(output.ChaosMaximumHitTaken) or 0,
			isIllusionRisk = defl_ch > 0 and defl_ch < 100,
		},
		block = {
			chance = tonumber(output.BlockChance) or tonumber(output.EffectiveBlockChance) or 0,
			maxCap = tonumber(output.BlockChanceMax) or ((data and data.misc and data.misc.BlockChanceCap) or 50),
			overcap = tonumber(output.BlockChanceOverCap) or 0,
		},
		esChaosCi = {
			life = life, energyShield = es, isCI = is_ci,
			chaosDoubleEsDamage = (es > 0 and not is_ci and not chaos_no_2x),
			poisonBypassesEs = (es > 0 and not is_ci),
			ciStunFreezeLockRisk = (is_ci and (not stun_es or not freeze_es)),
			stunThresholdFromEs = stun_es, freezeThresholdFromEs = freeze_es,
		},
		pinnacleBuildup = {
			referenceThreshold = pin_thr,
			estHitsHeavyStun = (phys_hit + avg_hit * 0.5) > 0 and math.ceil(pin_thr / math.max(phys_hit + avg_hit * 0.5, 1)) or nil,
			estHitsFreeze = cold_hit > 0 and math.ceil(pin_thr / math.max(cold_hit, 1)) or nil,
			estHitsElectrocute = light_hit > 0 and math.ceil(pin_thr / math.max(light_hit, 1)) or nil,
		},
		crossbow = crossbow,
		resists = resists,
		spirit = {
			total = tonumber(output.Spirit) or 0, unreserved = tonumber(output.SpiritUnreserved) or 0,
			demanded = (tonumber(output.Spirit) or 0) - (tonumber(output.SpiritUnreserved) or 0),
			overloaded = (tonumber(output.SpiritUnreserved) or 0) < 0,
		},
		charmBloat = {
			conditionUsingCharm = build.configTab and build.configTab.input and (build.configTab.input.conditionUsingCharm and true or false) or false,
			hasForUtopia = has_utopia, charms = active_charms,
		},
		unsimulatedRedCards = scan_unsimulated_skills(build),
		bondedRunes = scan_bonded_runes(build, mod_db),
		weaponSetSwapAudit = check_weapon_set_swap_gaps(build),
		elapsedMs = now_ms() - t0,
	}
end

M.rpc_handlers = { deep_audit_probe = handle_deep_audit_probe }

local ok_df, df = pcall(require, "dehydration_and_fixup")
if ok_df and type(df) == "table" and type(df.rpc_handlers) == "table" then
	for k, v in pairs(df.rpc_handlers) do M.rpc_handlers[k] = v end
end

return M
