-- PoE2 Agent-Native Probes: agent_probe_build, eval_scenario_envelope, compute_grad_build.
-- Clean PoE2 model: Spirit / Deflection / Charm 1..3 / Flask 1..2 / dual weapon sets.
-- Explicitly excludes PoE1 0.033s trigger cliffs, PoE1 guard skills, Utility flasks, and Tinctures.
local calcs = require("Modules.Calcs")
local H = require("pob_host")
local json_obj, now_ms, get_build = H.json_obj, H.now_ms, H.get_build
local M = {}

local RESISTS = { "Fire", "Cold", "Lightning", "Chaos" }
local ATTRS = { "Str", "Dex", "Int" }
local RES_MOD_MAP = {
	FireResist = { "Fire" }, ColdResist = { "Cold" }, LightningResist = { "Lightning" },
	ChaosResist = { "Chaos" }, ElementalResist = { "Fire", "Cold", "Lightning" },
}
local RES_MAX_MAP = {
	FireResistMax = { "Fire" }, ColdResistMax = { "Cold" }, LightningResistMax = { "Lightning" },
	ChaosResistMax = { "Chaos" }, ElementalResistMax = { "Fire", "Cold", "Lightning" },
}
local CORE_DIMS = {
	"flat_phys", "flat_fire", "flat_cold", "flat_light", "flat_chaos",
	"inc_damage", "inc_spell_dmg", "inc_attack_dmg", "inc_crit_chance", "crit_multi", "inc_speed",
	"gem_level", "minus_enemy_res", "flat_life", "flat_es", "flat_armour", "flat_evasion",
	"inc_life", "inc_es", "flat_mana",
}
local ALL_DIMS = {}
for i, d in ipairs(CORE_DIMS) do ALL_DIMS[i] = d end
ALL_DIMS[21], ALL_DIMS[22], ALL_DIMS[23], ALL_DIMS[24] = "fire_res", "cold_res", "light_res", "chaos_res"

local function primary_dps(out)
	if (out.FullDPS or 0) > 0 then return out.FullDPS, "FullDPS" end
	return out.CombinedDPS or out.TotalDPS or 0, "CombinedDPS"
end

local function sum_item_attr_res(item, slot_num, mod_db)
	local src_list = item.modList or (item.slotModList and item.slotModList[slot_num])
	local base_attr = { Str = 0, Dex = 0, Int = 0 }
	local inc_attr = { Str = 0, Dex = 0, Int = 0 }
	local base_res = { Fire = 0, Cold = 0, Lightning = 0, Chaos = 0 }
	local base_res_max = { Fire = 0, Cold = 0, Lightning = 0, Chaos = 0 }
	if not src_list then return base_attr, inc_attr, base_res, base_res_max end

	for _, mod in ipairs(src_list) do
		if type(mod.value) == "number" and (mod.flags or 0) == 0 and (mod.keywordFlags or 0) == 0 then
			local ok, val = pcall(mod_db.EvalMod, mod_db, mod)
			if ok and type(val) == "number" then
				local n, t = mod.name, mod.type
				if t == "BASE" then
					if base_attr[n] ~= nil then
						base_attr[n] = base_attr[n] + val
					elseif RES_MOD_MAP[n] then
						for _, e in ipairs(RES_MOD_MAP[n]) do base_res[e] = base_res[e] + val end
					elseif RES_MAX_MAP[n] then
						for _, e in ipairs(RES_MAX_MAP[n]) do base_res_max[e] = base_res_max[e] + val end
					end
				elseif t == "INC" and inc_attr[n] ~= nil then
					inc_attr[n] = inc_attr[n] + val
				end
			end
		end
	end
	return base_attr, inc_attr, base_res, base_res_max
end

local function handle_agent_probe_build(_params)
	local build, t0 = get_build(), now_ms()
	local env = build.calcsTab and build.calcsTab.mainEnv
	local output = (env and env.player and env.player.output) or {}
	local mod_db = env and env.player and env.player.modDB
	local resist_slack, res_total, res_cap, res_inc = {}, {}, {}, {}
	local max_cap = (data and data.misc or {}).MaxResistCap or 90

	for _, elem in ipairs(RESISTS) do
		local is_ele = elem ~= "Chaos"
		local cap = 75
		if mod_db then
			cap = mod_db:Override(nil, elem .. "ResistMax")
				or math.min(max_cap, mod_db:Sum("BASE", nil, elem .. "ResistMax", is_ele and "ElementalResistMax" or nil))
		end
		local tot = output[elem .. "ResistTotal"] or 0
		res_cap[elem], res_total[elem] = math.modf(cap), tot
		res_inc[elem] = mod_db and math.max(calcLib.mod(mod_db, nil, elem .. "Resist", is_ele and "ElementalResist" or nil), 0) or 1
		resist_slack[elem] = { final = output[elem .. "Resist"] or 0, total = tot, cap = res_cap[elem], overcapSlack = tot - res_cap[elem] }
	end

	local attr_cur, attr_slack, attr_sum = {}, {}, {}
	for _, a in ipairs(ATTRS) do
		local cur, req = output[a] or 0, output["Req" .. a] or 0
		attr_cur[a], attr_slack[a] = cur, { current = cur, required = req, slack = cur - req }
		if mod_db then
			attr_sum[a] = { mod_db:Sum("BASE", nil, a), mod_db:Sum("INC", nil, a), mod_db:More(nil, a) }
		else
			attr_sum[a] = { cur, 0, 1 }
		end
	end

	local spirit_total = output.Spirit or 0
	local spirit_unres = output.SpiritUnreserved or spirit_total
	local resource_slack = {
		life = output.Life or 0, lifeUnreserved = output.LifeUnreserved or 0, lifeUnreservedPct = output.LifeUnreservedPercent or 0,
		mana = output.Mana or 0, manaUnreserved = output.ManaUnreserved or 0, manaUnreservedPct = output.ManaUnreservedPercent or 0,
		energyShield = output.EnergyShield or 0,
		spirit = spirit_total,
		spiritUnreserved = spirit_unres,
		spiritDemanded = spirit_total - spirit_unres,
		spiritUnreservedPct = output.SpiritUnreservedPercent or 0,
	}
	local defence_slack = {
		armour = output.Armour or 0,
		evasion = output.Evasion or 0,
		energyShield = output.EnergyShield or 0,
		blockChance = output.EffectiveBlockChance or output.BlockChance or 0,
		deflectionChance = output.DeflectionChance or output.EffectiveDeflectionChance or 0,
		totalEhp = output.TotalEHP or 0,
	}

	local req_mult = mod_db and calcLib.mod(mod_db, nil, "GlobalAttributeRequirements") or 1
	local ignore_req = mod_db and mod_db:Flag(nil, "IgnoreAttributeRequirements") or false
	local req_table = (env and env.requirementsTable) or {}
	local load_bearing, slots_inspected = {}, 0

	for slot_name, item in pairs((env and env.player and env.player.itemList) or {}) do
		if not slot_name:match("^Flask") and not slot_name:match("^Charm") then
			slots_inspected = slots_inspected + 1
			local slot_obj = build.itemsTab and build.itemsTab.slots and build.itemsTab.slots[slot_name]
			local b_attr, i_attr, b_res, b_res_max = sum_item_attr_res(item, (slot_obj and slot_obj.slotNum) or 1, mod_db)
			local new_attr, bricked = {}, {}
			for _, a in ipairs(ATTRS) do
				local s = attr_sum[a]
				new_attr[a] = math.max(round((s[1] - b_attr[a]) * (1 + (s[2] - i_attr[a]) / 100) * s[3]), 0)
			end
			if not ignore_req then
				for _, req in ipairs(req_table) do
					if not (req.source == "Item" and req.sourceSlot == slot_name) then
						local missing = nil
						for _, a in ipairs(ATTRS) do
							local r_val = (req[a] and req[a] > 0) and math.floor(req[a] * req_mult) or 0
							if attr_cur[a] >= r_val and r_val > new_attr[a] then
								missing = missing or {}
								missing[a] = { required = r_val, afterRemoval = new_attr[a], deficit = new_attr[a] - r_val }
							end
						end
						if missing then
							local sg = req.sourceGem
							local label = (req.source == "Item" and req.sourceItem and (req.sourceItem.name .. " [" .. (req.sourceSlot or "") .. "]"))
								or (req.source == "Gem" and sg and (sg.nameSpec .. " " .. tostring(sg.level) .. "/" .. tostring(sg.quality))) or "Unknown"
							bricked[#bricked + 1] = { source = req.source, name = label, missing = missing }
						end
					end
				end
			end
			local res_deficits = json_obj({})
			for _, elem in ipairs(RESISTS) do
				if b_res[elem] > 0 then
					local post_tot = math.modf(res_total[elem] - b_res[elem] * res_inc[elem])
					local post_cap = res_cap[elem] - b_res_max[elem]
					if post_tot < post_cap then
						res_deficits[elem] = { beforeSlack = res_total[elem] - res_cap[elem], afterSlack = post_tot - post_cap, lost = b_res[elem] }
					end
				end
			end
			if #bricked > 0 or next(res_deficits) ~= nil then
				load_bearing[#load_bearing + 1] = {
					slot = slot_name, itemName = item.name or item.title or "Item",
					attrDelta = b_attr, brickedCount = #bricked, bricked = bricked, resistDeficits = res_deficits,
				}
			end
		end
	end
	table.sort(load_bearing, function(a, b) return a.slot < b.slot end)

	local aset = build.itemsTab and build.itemsTab.activeItemSet
	local ws1, ws2 = 0, 0
	if build.spec and type(build.spec.CountAllocNodes) == "function" then
		local _, _, _, _, u1, u2 = build.spec:CountAllocNodes()
		ws1, ws2 = tonumber(u1) or 0, tonumber(u2) or 0
	end
	return {
		game = "poe2",
		resistSlack = resist_slack,
		attrSlack = attr_slack,
		resourceSlack = resource_slack,
		defenceSlack = defence_slack,
		weaponSet = {
			activeWeaponSet = (type(aset) == "table" and aset.useSecondWeaponSet) and 2 or 1,
			allocMode = (build.spec and build.spec.allocMode) or 0,
			weaponSet1Used = ws1,
			weaponSet2Used = ws2,
		},
		loadBearingGear = load_bearing,
		slotsInspected = slots_inspected,
		elapsedMs = now_ms() - t0,
	}
end

local ENVELOPE_SCENARIOS = {
	{ name = "Raw", overlay = {
		enemyIsBoss = "None", conditionKilledRecently = true, conditionHitRecently = true,
		usePowerCharges = true, useFrenzyCharges = true, useEnduranceCharges = true,
	} },
	{ name = "PinnacleSustained", overlay = {
		enemyIsBoss = "Pinnacle", conditionKilledRecently = false,
	} },
	{ name = "Burst", overlay = {
		enemyIsBoss = "Pinnacle", conditionKilledRecently = false, conditionHitRecently = true,
		usePowerCharges = true, useFrenzyCharges = true, useEnduranceCharges = true,
	} },
	{ name = "WorstHit", overlay = {
		enemyIsBoss = "Pinnacle", enemyDamageRollRange = 100, conditionKilledRecently = false,
	} },
}

local function handle_eval_scenario_envelope(_params)
	local build = get_build()
	local cfg_input = build.configTab.input
	local saved_cfg = {}
	for k, v in pairs(cfg_input) do saved_cfg[k] = v end

	local function reset_cfg(overlay)
		for k in pairs(cfg_input) do cfg_input[k] = nil end
		for k, v in pairs(saved_cfg) do cfg_input[k] = v end
		for k, v in pairs(overlay or {}) do cfg_input[k] = v end
	end
	local function restore_all()
		reset_cfg()
		build.configTab:BuildModList()
		build.calcsTab:BuildOutput()
	end

	local t0, out_scenarios = now_ms(), {}
	local ok, err = pcall(function()
		for _, sc in ipairs(ENVELOPE_SCENARIOS) do
			local ts = now_ms()
			reset_cfg(sc.overlay)
			build.configTab:BuildModList()
			local res_env = calcs.buildOutput(build, "MAIN")
			local out = (res_env and res_env.player and res_env.player.output) or {}
			local dps, dps_key = primary_dps(out)
			out_scenarios[sc.name] = {
				dps = dps, dpsKey = dps_key, combinedDps = out.CombinedDPS or 0, fullDps = out.FullDPS or 0,
				totalEhp = out.TotalEHP or 0, physMaxHit = out.PhysicalMaximumHitTaken or 0,
				eleMinMaxHit = math.min(out.FireMaximumHitTaken or 0, out.ColdMaximumHitTaken or 0, out.LightningMaximumHitTaken or 0),
				chaosMaxHit = out.ChaosMaximumHitTaken or 0, elapsedMs = now_ms() - ts,
			}
		end
	end)
	restore_all()
	if not ok then error(err, 0) end

	local raw_dps = (out_scenarios.Raw and out_scenarios.Raw.dps) or 0
	local pin_dps = (out_scenarios.PinnacleSustained and out_scenarios.PinnacleSustained.dps) or 0
	return {
		game = "poe2",
		scenarios = out_scenarios,
		inflationRatio = (pin_dps > 0) and (raw_dps / pin_dps) or 0,
		elapsedMs = now_ms() - t0,
	}
end

local FLAT_ELEM_SPECS = {
	flat_phys = { "PhysicalMin", "PhysicalMax" }, flat_fire = { "FireMin", "FireMax" },
	flat_cold = { "ColdMin", "ColdMax" }, flat_light = { "LightningMin", "LightningMax" },
	flat_chaos = { "ChaosMin", "ChaosMax" },
}
local DEF_MOD_SPECS = {
	flat_life = { "Life", "BASE", 50 }, flat_es = { "EnergyShield", "BASE", 50 },
	flat_armour = { "Armour", "BASE", 200 }, flat_evasion = { "Evasion", "BASE", 200 },
	inc_life = { "Life", "INC", 10 }, inc_es = { "EnergyShield", "INC", 10 },
	flat_mana = { "Mana", "BASE", 50 }, fire_res = { "FireResist", "BASE", 20 },
	cold_res = { "ColdResist", "BASE", 20 }, light_res = { "LightningResist", "BASE", 20 },
	chaos_res = { "ChaosResist", "BASE", 20 },
}

local function build_probe_mods(dim_name, is_minion)
	local ml = H.make_obj("ModList")
	local function add_off(name, mtype, val, flags)
		if is_minion then
			ml:NewMod("MinionModifier", "LIST", { mod = modLib.createMod(name, mtype, val, "GradProbe", flags or 0) }, "GradProbe")
		else
			ml:NewMod(name, mtype, val, "GradProbe", flags or 0)
		end
	end
	if FLAT_ELEM_SPECS[dim_name] then
		local pair = FLAT_ELEM_SPECS[dim_name]
		add_off(pair[1], "BASE", 10); add_off(pair[2], "BASE", 10)
		return ml, 10
	elseif DEF_MOD_SPECS[dim_name] then
		local sp = DEF_MOD_SPECS[dim_name]
		ml:NewMod(sp[1], sp[2], sp[3], "GradProbe")
		return ml, sp[3]
	elseif dim_name == "inc_damage" then add_off("Damage", "INC", 10); return ml, 10
	elseif dim_name == "inc_spell_dmg" then add_off("Damage", "INC", 10, ModFlag.Spell); return ml, 10
	elseif dim_name == "inc_attack_dmg" then add_off("Damage", "INC", 10, ModFlag.Attack); return ml, 10
	elseif dim_name == "inc_crit_chance" then add_off("CritChance", "INC", 20); return ml, 20
	elseif dim_name == "crit_multi" then add_off("CritMultiplier", "BASE", 10); return ml, 10
	elseif dim_name == "inc_speed" then add_off("Speed", "INC", 10); return ml, 10
	elseif dim_name == "gem_level" then
		ml:NewMod("GemProperty", "LIST", { keywordList = { "skill" }, key = "level", value = 1 }, "GradProbe")
		return ml, 1
	elseif dim_name == "minus_enemy_res" then
		add_off("ElementalPenetration", "BASE", 5); add_off("ChaosPenetration", "BASE", 5)
		return ml, 5
	end
	return ml, 1
end

local function handle_compute_grad_build(params)
	params = type(params) == "table" and params or {}
	local build = get_build()
	local main_env = build.calcsTab and build.calcsTab.mainEnv
	local main_skill = main_env and main_env.player and main_env.player.mainSkill
	local is_minion = (main_env and main_env.minion ~= nil)
		or (main_skill and main_skill.skillFlags and main_skill.skillFlags.minion) or false
	local ehp_weight = tonumber(params.ehpWeight) or 0.5
	local dims = params.includeResists == true and ALL_DIMS or CORE_DIMS
	local t0 = now_ms()
	local calc_func, calc_base = calcs.getMiscCalculator(build)
	calc_base = calc_base or {}
	local base_dps, dps_key = primary_dps(calc_base)
	local target_stat = (type(params.stat) == "string" and params.stat ~= "") and params.stat or dps_key
	local use_full_dps = (target_stat == "FullDPS") or (dps_key == "FullDPS")
	local base_ehp = calc_base.TotalEHP or 1
	local base_stat = (target_stat == dps_key) and base_dps or (calc_base[target_stat] or base_dps)
	local safe_dps, safe_ehp = (base_dps > 0) and base_dps or 1, (base_ehp > 0) and base_ehp or 1
	local weights = {}
	for i, dim_name in ipairs(dims) do
		local ml, unit = build_probe_mods(dim_name, is_minion)
		local out = calc_func({ addNodes = { [{ id = 991000 + i, modList = ml }] = true } }, use_full_dps) or {}
		local new_dps = (dps_key == "FullDPS" and (out.FullDPS or 0))
			or (out[dps_key] or out.CombinedDPS or out.TotalDPS or 0)
		local new_ehp = out.TotalEHP or 0
		local new_stat = (target_stat == dps_key) and new_dps or (out[target_stat] or new_dps)
		local rel_dps = ((new_dps - base_dps) / unit) / safe_dps
		local rel_ehp = ((new_ehp - base_ehp) / unit) / safe_ehp
		weights[dim_name] = {
			dStatPerUnit = (new_stat - base_stat) / unit, dDpsPerUnit = (new_dps - base_dps) / unit,
			dEhpPerUnit = (new_ehp - base_ehp) / unit, relDpsPerUnit = rel_dps, relEhpPerUnit = rel_ehp,
			scorePerUnit = rel_dps + ehp_weight * rel_ehp,
		}
	end
	return {
		game = "poe2",
		stat = target_stat, baseStat = base_stat, baseDps = base_dps, baseEhp = base_ehp,
		ehpWeight = ehp_weight, dimCount = #dims, isMinion = is_minion, weights = weights,
		elapsedMs = now_ms() - t0,
	}
end

M.rpc_handlers = {
	agent_probe_build = handle_agent_probe_build,
	eval_scenario_envelope = handle_eval_scenario_envelope,
	compute_grad_build = handle_compute_grad_build,
}

return M
