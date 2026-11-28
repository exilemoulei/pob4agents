-- PoE2 Chinese item text -> English item text reverse translator for PoB2 Item:ParseRaw.
local H = require("pob_host")
local zj = require("zh_json")

local M = {}
local data_poe2_root = "."
local R = nil

local CN_HEADERS = "物品类别=Item Class;稀有度=Rarity;品质=Quality;闪避值=Evasion Rating;能量护盾=Energy Shield;"
	.. "护甲=Armour;护甲值=Armour;精魂=Spirit;需求=Requirements;等级=Level;力量=Str;敏捷=Dex;智慧=Int;智力=Int;"
	.. "插槽=Sockets;物品等级=Item Level;物理伤害=Physical Damage;元素伤害=Elemental Damage;混沌伤害=Chaos Damage;"
	.. "暴击几率=Critical Hit Chance;暴击率=Critical Hit Chance;每秒攻击次数=Attacks per Second;"
	.. "格挡几率=Block Chance;范围=Radius;仅限=Limited to"
local CN_RARITY = "稀有=Rare;魔法=Magic;传奇=Unique;普通=Normal"
local CN_STATUS = "已腐化=Corrupted;已污染=Corrupted;已复制=Mirrored;未鉴定=Unidentified;分裂物品=Fractured Item;"
	.. "圣化=Sanctified;已圣化=Sanctified"

local PH = "{(%d*)[^}]*}"
local NUM = "([%+%-]?%d+%.?%d*%%?)"

local function nows(s) return (s:gsub("%s+", "")) end
local function trim(s) return (s:gsub("^%s+", ""):gsub("%s+$", "")) end
local function has_cjk(s) return s:find("[\128-\255]") ~= nil end
local function skeleton(s) return (s:gsub(PH, ""):gsub("#", ""):gsub("[%d%.%+%-%%%s]", "")) end

local function each_pair(spec, fn)
	for zh, en in spec:gmatch("([^=;]+)=([^;]+)") do fn(zh, en) end
end

local function push(tbl, key, val, cap)
	local list = tbl[key]
	if not list then list = {}; tbl[key] = list end
	if #list < cap then
		for _, v in ipairs(list) do if v == val then return end end
		list[#list + 1] = val
	end
end

local function add_stat(r, zh, en)
	if type(zh) ~= "string" or type(en) ~= "string" or zh == "" or en == "" or zh:find("\n", 1, true) then return end
	if zh:find(PH) or zh:find("#", 1, true) then
		local sk = skeleton(zh)
		if sk ~= "" then push(r.tpl, sk, { zh = zh, en = en }, 40) end
	elseif has_cjk(zh) then
		push(r.exact, nows(zh), en, 6)
	end
end

local function add_name(map, zh, en)
	if type(zh) == "string" and type(en) == "string" and zh ~= "" and has_cjk(zh) then
		local k = nows(zh)
		if map[k] == nil then map[k] = en end
	end
end

local function load_tables()
	if R ~= nil then return R or nil end
	R = false
	local r = { exact = {}, tpl = {}, uniques = {}, bases = {}, base_list = {}, headers = {}, rarity = {}, status = {} }
	local any = false
	for _, sub in ipairs({ "zh-rCN", "zh-rTW" }) do
		local dir = data_poe2_root .. "/" .. sub
		for _, fn in ipairs({ "stats.json", "ggpk_display.json" }) do
			local e = zj.entries(dir, fn)
			if e then
				any = true
				for en, zh in pairs(e) do add_stat(r, zh, en) end
			end
		end
		for en, zh in pairs(zj.entries(dir, "items.json") or {}) do any = true; add_name(r.bases, zh, en) end
		for en, zh in pairs(zj.entries(dir, "uniques.json") or {}) do any = true; add_name(r.uniques, zh, en) end
	end
	if not any then return nil end
	each_pair(CN_HEADERS, function(zh, en) if r.headers[zh] == nil then r.headers[zh] = en end end)
	each_pair(CN_RARITY, function(zh, en) if r.rarity[zh] == nil then r.rarity[zh] = en end end)
	each_pair(CN_STATUS, function(zh, en) if r.status[zh] == nil then r.status[zh] = en end end)
	for zh, en in pairs(r.bases) do r.base_list[#r.base_list + 1] = { zh, en } end
	table.sort(r.base_list, function(a, b) return #a[1] > #b[1] end)
	R = r
	return r
end

local function lit_pat(t) return (t:gsub("[%^%$%(%)%%%.%[%]%*%+%-%?]", "%%%0"):gsub("%s+", "%%s*")) end

local function compile(zh)
	local out, keys, pos, seq = { "^%s*" }, {}, 1, 0
	while true do
		local s, e, inner = zh:find(PH, pos)
		local h = zh:find("#", pos, true)
		if h and (not s or h < s) then s, e, inner = h, h, "" end
		if not s then break end
		out[#out + 1] = lit_pat(zh:sub(pos, s - 1))
		out[#out + 1] = NUM
		keys[#keys + 1] = inner ~= "" and tonumber(inner) or seq
		seq = seq + 1
		pos = e + 1
	end
	out[#out + 1] = lit_pat(zh:sub(pos)) .. "%s*$"
	return table.concat(out), keys
end

local function render(en, keys, caps)
	local vals = {}
	for i, k in ipairs(keys) do vals[k] = caps[i] end
	local seq = -1
	local function next_val(d) seq = seq + 1; return vals[(d ~= nil and d ~= "") and tonumber(d) or seq] end
	local out = en:gsub(PH, next_val):gsub("#", function() return next_val(nil) end)
	if out:find("[{#]") or has_cjk(out) then return nil end
	return (out:gsub("%[[^%]|]*|([^%]]*)%]", "%1"):gsub("%[([^%]]*)%]", "%1"))
end

local function translate_mod(r, text)
	local core, suf = text:match("^(.-)%s*(%([%a ]+%))$")
	if not core then core, suf = text, nil end
	core = trim(core:gsub("(%d)%([%d%.]+%-[%d%.]+%)", "%1"))
	local ex = r.exact[nows(core)]
	if ex and ex[1] then return ex[1] .. (suf and " " .. suf or "") end
	for _, t in ipairs(r.tpl[skeleton(core)] or {}) do
		if not t.pat then t.pat, t.keys = compile(t.zh) end
		local caps = { core:match(t.pat) }
		if caps[1] ~= nil then
			local line = render(t.en, t.keys, caps)
			if line then return line .. (suf and " " .. suf or "") end
		end
	end
	return nil
end

local function reverse_text(raw)
	if type(raw) ~= "string" or not has_cjk(raw) then return raw end
	local r = load_tables()
	if not r then return raw end
	local out, rarity, names_left = {}, nil, 0
	for line in (raw:gsub("\r", "") .. "\n"):gmatch("([^\n]*)\n") do
		local t, res = trim(line), nil
		if t == "--------" then names_left, res = 0, t
		elseif names_left > 0 then
			local k = nows(t)
			if names_left == 2 and (rarity == "Rare" or rarity == "Unique") then
				res = r.uniques[k] or r.bases[k] or t
			else
				res = r.bases[k] or t
			end
			names_left = names_left - 1
		elseif not has_cjk(t) then
			res = t
			local rv = t:match("^Rarity:%s*(%a+)")
			if rv then rarity, names_left = rv, (rv == "Rare" or rv == "Unique") and 2 or 1 end
		else
			local key, val = t:gsub("\239\188\154", ":"):match("^([^:]+):%s*(.-)%s*$")
			local en_key = key and r.headers[nows(key)]
			if en_key then
				if en_key == "Rarity" then
					local v = r.rarity[nows(val)] or val
					rarity, names_left = v, (v == "Rare" or v == "Unique") and 2 or 1
					res = "Rarity: " .. v
				else
					res = en_key .. ": " .. (has_cjk(val) and (translate_mod(r, val) or val) or val)
				end
			else
				res = r.status[nows(t)] or translate_mod(r, t) or t
			end
		end
		if res ~= nil then out[#out + 1] = res end
	end
	while out[#out] == "" do out[#out] = nil end
	return table.concat(out, "\n")
end

function M.install(data_root_path)
	local norm = H.norm_path(data_root_path or os.getenv("POB2_DATA_ROOT") or ".")
	if norm:lower():match("/poe2$") then data_poe2_root = norm else data_poe2_root = norm .. "/poe2" end
	R = nil
	_G.PobToolsReverseText = reverse_text
	return true
end

M.reverse_text = reverse_text
return M
