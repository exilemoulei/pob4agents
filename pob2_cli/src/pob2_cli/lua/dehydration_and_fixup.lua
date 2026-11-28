-- PoE2 4-Dim Honest Dehydration (preserving quest* & resistancePenalty, stripping Charm/Utopia bloat)
-- and #2536 Smart Main-Skill Group + StatSetIndex Selector.
local calcs = require("Modules.Calcs")
local H = require("pob_host")
local now_ms, get_build = H.now_ms, H.get_build
local M = {}

local function primary_dps(out)
	if (out.FullDPS or 0) > 0 then return out.FullDPS, "FullDPS" end
	return out.CombinedDPS or out.TotalDPS or 0, "CombinedDPS"
end

local function handle_eval_honest_dehydration(_params)
	local build, t0 = get_build(), now_ms()
	local cfg_in = build.configTab.input
	local items_tab = build.itemsTab
	local saved_cfg, saved_charms = {}, {}
	for k, v in pairs(cfg_in) do saved_cfg[k] = v end
	for i = 1, 3 do
		local sl = items_tab and items_tab.slots and items_tab.slots["Charm " .. i]
		if sl then saved_charms[i] = sl.active end
	end

	local function apply_overlay(overlay, disable_charms)
		for k in pairs(cfg_in) do cfg_in[k] = nil end
		for k, v in pairs(saved_cfg) do cfg_in[k] = v end
		for k, v in pairs(overlay or {}) do
			if not tostring(k):match("^quest") and k ~= "resistancePenalty" then
				cfg_in[k] = v
			end
		end
		for i = 1, 3 do
			local sl = items_tab and items_tab.slots and items_tab.slots["Charm " .. i]
			if sl then sl.active = disable_charms and false or saved_charms[i] end
		end
	end

	local scenarios = {
		{ name = "Raw", disableCharms = false, overlay = {
			enemyIsBoss = "None", conditionKilledRecently = true, conditionHitRecently = true,
			usePowerCharges = true, useFrenzyCharges = true, useEnduranceCharges = true,
		} },
		{ name = "PinnacleSustained", disableCharms = true, overlay = {
			enemyIsBoss = "Pinnacle", conditionKilledRecently = false, conditionUsingCharm = false,
			usePowerCharges = false, useFrenzyCharges = false, useEnduranceCharges = false,
		} },
		{ name = "Burst", disableCharms = true, overlay = {
			enemyIsBoss = "Pinnacle", conditionKilledRecently = false, conditionHitRecently = true,
			conditionUsingCharm = false, usePowerCharges = true, useFrenzyCharges = true, useEnduranceCharges = true,
		} },
		{ name = "WorstHit", disableCharms = true, overlay = {
			enemyIsBoss = "Pinnacle", enemyDamageRollRange = 100, conditionKilledRecently = false,
			conditionUsingCharm = false,
		} },
	}

	local out_sc = {}
	local ok, err = pcall(function()
		for _, sc in ipairs(scenarios) do
			apply_overlay(sc.overlay, sc.disableCharms)
			build.configTab:BuildModList()
			local res_env = calcs.buildOutput(build, "MAIN")
			local out = (res_env and res_env.player and res_env.player.output) or {}
			local dps, dps_key = primary_dps(out)
			out_sc[sc.name] = {
				dps = dps, dpsKey = dps_key,
				totalEhp = tonumber(out.TotalEHP) or 0,
				spirit = tonumber(out.Spirit) or 0,
				spiritUnreserved = tonumber(out.SpiritUnreserved) or 0,
				physMaxHit = tonumber(out.PhysicalMaximumHitTaken) or 0,
				eleMinMaxHit = math.min(tonumber(out.FireMaximumHitTaken) or 0, tonumber(out.ColdMaximumHitTaken) or 0, tonumber(out.LightningMaximumHitTaken) or 0),
				chaosMaxHit = tonumber(out.ChaosMaximumHitTaken) or 0,
			}
		end
	end)
	apply_overlay(nil, false)
	build.configTab:BuildModList()
	build.calcsTab:BuildOutput()
	if not ok then error(err, 0) end

	local raw_d = (out_sc.Raw and out_sc.Raw.dps) or 0
	local pin_d = (out_sc.PinnacleSustained and out_sc.PinnacleSustained.dps) or 0
	local raw_e = (out_sc.Raw and out_sc.Raw.totalEhp) or 0
	local pin_e = (out_sc.PinnacleSustained and out_sc.PinnacleSustained.totalEhp) or 0
	return {
		game = "poe2",
		preservedQuestRewards = true,
		preservedResistancePenalty = true,
		strippedCharmAndUtopiaBloat = true,
		scenarios = out_sc,
		dpsInflationRatio = (pin_d > 0) and (math.floor((raw_d / pin_d) * 100 + 0.5) / 100) or 0,
		ehpInflationRatio = (pin_e > 0) and (math.floor((raw_e / pin_e) * 100 + 0.5) / 100) or 0,
		elapsedMs = now_ms() - t0,
	}
end

local function handle_select_best_main_skill(params)
	params = type(params) == "table" and params or {}
	local do_apply = params.apply ~= false
	local build = get_build()
	local st = build.skillsTab
	local list = (st and st.socketGroupList) or {}
	if #list == 0 then return { ok = true, changed = false } end

	local cur_grp = build.mainSocketGroup or 1
	local cur_out = (build.calcsTab and build.calcsTab.mainOutput) or {}
	local cur_dps = primary_dps(cur_out)

	local function get_cur_statset(grp)
		local dl = grp and grp.displaySkillList
		local as = dl and dl[grp.mainActiveSkill or 1]
		local ae = as and as.activeEffect
		if not ae or not ae.srcInstance or not ae.grantedEffect then return 1, nil, nil end
		local ge_id = ae.grantedEffect.id
		local ss_tbl = ae.srcInstance.statSet
		local idx = (type(ss_tbl) == "table" and (ss_tbl[ge_id] or ss_tbl.index)) or 1
		return idx, ae, ge_id
	end

	local cur_ss = get_cur_statset(list[cur_grp])
	if cur_dps > 0 then
		return { ok = true, changed = false, beforeGroup = cur_grp, afterGroup = cur_grp, beforeStatSet = cur_ss, afterStatSet = cur_ss, beforeDps = cur_dps, afterDps = cur_dps }
	end

	local best_grp, best_ss, best_dps, best_name, best_ss_label = cur_grp, cur_ss, cur_dps, "", nil
	local saved_grp = build.mainSocketGroup
	for gi, grp in ipairs(list) do
		if grp and grp.enabled ~= false and type(grp.gemList) == "table" and #grp.gemList > 0 then
			build.mainSocketGroup = gi
			local res_env = calcs.buildOutput(build, "MAIN")
			local out = (res_env and res_env.player and res_env.player.output) or {}
			local d = primary_dps(out)
			local sk = res_env and res_env.player and res_env.player.mainSkill
			local ae = sk and sk.activeEffect
			local ge = ae and ae.grantedEffect
			local nm = (ge and ge.name) or (grp.gemList[1] and grp.gemList[1].nameSpec) or ""
			if d > best_dps then
				best_grp, best_ss, best_dps, best_name, best_ss_label = gi, 1, d, nm, nil
			end
			if ge and type(ge.statSets) == "table" and #ge.statSets > 1 and ae.srcInstance then
				local src_inst = ae.srcInstance
				local prev_ss = src_inst.statSet
				for ssi = 1, #ge.statSets do
					src_inst.statSet = { [ge.id] = ssi, index = ssi }
					local env2 = calcs.buildOutput(build, "MAIN")
					local out2 = (env2 and env2.player and env2.player.output) or {}
					local d2 = primary_dps(out2)
					if d2 > best_dps then
						best_grp, best_ss, best_dps, best_name = gi, ssi, d2, nm
						best_ss_label = ge.statSets[ssi] and ge.statSets[ssi].label or tostring(ssi)
					end
				end
				src_inst.statSet = prev_ss
			end
		end
	end

	local changed = (best_dps > cur_dps) and (best_grp ~= cur_grp or best_ss ~= cur_ss)
	if changed and do_apply then
		build.mainSocketGroup = best_grp
		local grp = list[best_grp]
		if grp and best_ss > 1 then
			local _, ae, ge_id = get_cur_statset(grp)
			if ae and ae.srcInstance and ge_id then
				ae.srcInstance.statSet = { [ge_id] = best_ss, index = best_ss }
			end
		end
		build.buildFlag = true
		H.frame()
	else
		build.mainSocketGroup = saved_grp
		build.calcsTab:BuildOutput()
	end
	return {
		ok = true, changed = changed, recommended = changed, applied = changed and do_apply,
		beforeGroup = cur_grp, afterGroup = best_grp, beforeStatSet = cur_ss, afterStatSet = best_ss,
		skillName = best_name, statSetLabel = best_ss_label, beforeDps = cur_dps, afterDps = best_dps,
	}
end

M.rpc_handlers = {
	eval_honest_dehydration = handle_eval_honest_dehydration,
	select_best_main_skill = handle_select_best_main_skill,
}

return M
