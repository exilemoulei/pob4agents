-- Pure-Lua real-time display translator for PoB2 + PobTools bridge.lua.
-- Reads <data_root>/poe2/zh-rCN and <data_root>/poe2/zh-rTW (plus en pass-through).
local H = require("pob_host")
local zj = require("zh_json")
local M = {}

local LANG_ALIASES = {
	["zh"] = "zh-CN", ["zh-cn"] = "zh-CN", ["zh_cn"] = "zh-CN",
	["zh-rcn"] = "zh-CN", ["zh_rcn"] = "zh-CN", ["cn"] = "zh-CN",
	["chs"] = "zh-CN", ["simplified"] = "zh-CN",
	["zh-tw"] = "zh-TW", ["zh_tw"] = "zh-TW", ["zh-rtw"] = "zh-TW",
	["zh_rtw"] = "zh-TW", ["zh-hk"] = "zh-TW", ["zh_hk"] = "zh-TW",
	["tw"] = "zh-TW", ["cht"] = "zh-TW", ["traditional"] = "zh-TW",
	["en"] = "en", ["en-us"] = "en", ["en_us"] = "en",
	["en-gb"] = "en", ["en_gb"] = "en", ["english"] = "en",
}
local LANG_SUBDIR = { ["zh-CN"] = "zh-rCN", ["zh-TW"] = "zh-rTW" }
local UTF8_CHAR = "[\192-\255][\128-\191]*"

local SELF_DIR = (function()
	local src = debug and debug.getinfo and debug.getinfo(1, "S").source or ""
	local dir = type(src) == "string" and src:sub(1, 1) == "@" and src:sub(2):gsub("\\", "/"):match("^(.*)/[^/]*$")
	return dir or "."
end)()

local data_poe2_root = "."
local active_lang = "zh-CN"
local translate_enabled = true
local draw_override = nil
local dict_cache = {}
local missing_subdirs = {}
local han_fold = nil

local function trim(s) return (s:gsub("^%s+", ""):gsub("%s+$", "")) end

function M.normalize_lang(val)
	if type(val) ~= "string" then return nil end
	return LANG_ALIASES[trim(val):lower()]
end

local function utf8_chars(str)
	local out, n = {}, 0
	for ch in str:gmatch("[%z\1-\127\192-\255][\128-\191]*") do n = n + 1; out[n] = ch end
	return out
end

local function py_tuple_text(src, name)
	local body = src:match(name .. "%s*=%s*(%b())")
	if not body then return nil end
	local parts = {}
	for piece in body:gmatch('"([^"]*)"') do parts[#parts + 1] = piece end
	return table.concat(parts)
end

local function get_han_fold()
	if han_fold ~= nil then return han_fold or nil end
	han_fold = false
	local han_src = zj.read_file_bytes(SELF_DIR .. "/../han_fold.py")
	local trad = han_src and py_tuple_text(han_src, "_TRAD")
	local simp = han_src and py_tuple_text(han_src, "_SIMP")
	if not trad or not simp then return nil end
	local tc, sc = utf8_chars(trad), utf8_chars(simp)
	if #tc ~= #sc or #tc == 0 then return nil end
	local map = {}
	for i = 1, #tc do map[tc[i]] = sc[i] end
	local extra = han_src:match("_POE_HAN_OVERRIDES[^=]*=%s*(%b{})")
	for k, v in (extra or ""):gmatch('"([^"]+)"%s*:%s*"([^"]+)"') do map[k] = v end
	han_fold = map
	return map
end

local function add_tw_fallback(d)
	local fold = get_han_fold()
	if not fold then return false end
	local tw = data_poe2_root .. "/zh-rTW"
	local function fill(dst, src, skip)
		for k, v in pairs(src) do
			if dst[k] == nil and not (skip and skip[k]) then dst[k] = (v:gsub(UTF8_CHAR, fold)) end
		end
	end
	local function read(fn) local t = {}; zj.load_entries_into(tw, fn, t, nil); return t end
	fill(d.tags, read("tags.json"))
	fill(d.title_words, read("title_words.json"))
	fill(d.merged, read("ggpk_display.json"))
	fill(d.merged, read("pob_manual.json"))
	fill(d.items, read("items.json"))
	fill(d.uniques, read("uniques.json"))
	fill(d.gems, read("gems.json"))
	fill(d.passives, read("passives.json"))
	return true
end

local function get_active_dict()
	local subdir = LANG_SUBDIR[active_lang]
	if not subdir then return nil end
	if dict_cache[subdir] then return dict_cache[subdir] end
	if missing_subdirs[subdir] then return nil end

	local dir = data_poe2_root .. "/" .. subdir
	local d = {
		loaded = false, subdir = subdir, merged = {}, items = {}, uniques = {},
		gems = {}, passives = {}, tags = {}, affixes = {}, title_words = {},
	}
	local any = false
	for _, spec in ipairs({
		{ "ggpk_display.json", nil }, { "tags.json", d.tags }, { "items.json", d.items },
		{ "gems.json", d.gems }, { "ui.json", nil }, { "pob_manual.json", nil },
		{ "passives.json", d.passives }, { "uniques.json", d.uniques }, { "monsters.json", nil },
	}) do
		any = zj.load_entries_into(dir, spec[1], spec[2], d.merged) or any
	end
	zj.load_entries_into(dir, "title_words.json", d.title_words, nil)
	if not any then missing_subdirs[subdir] = true; return nil end

	local ui_fb = subdir == "zh-rTW" and {
		["Spirit"] = "精魂", ["Deflection"] = "偏轉", ["Charm"] = "護符", ["Waystone"] = "引路石",
		["Recovery"] = "恢復", ["Reserved"] = "保留", ["Unreserved"] = "未保留", ["Total"] = "總計",
		["Effective Hit Pool"] = "有效生命池", ["Average Hit"] = "平均擊中", ["Hit Chance"] = "命中率",
	} or {
		["Spirit"] = "精魂", ["Deflection"] = "偏转", ["Charm"] = "护符", ["Waystone"] = "引路石",
		["Recovery"] = "回复", ["Reserved"] = "保留", ["Unreserved"] = "未保留", ["Total"] = "总计",
		["Effective Hit Pool"] = "有效生命池", ["Average Hit"] = "平均击中", ["Hit Chance"] = "命中率",
	}
	for uk, uv in pairs(ui_fb) do if d.merged[uk] == nil then d.merged[uk] = uv end end
	if subdir == "zh-rCN" then d.twFallback = add_tw_fallback(d) end
	d.loaded = true
	dict_cache[subdir] = d
	return d
end

local function split_lead_color(text)
	local i = 1
	while true do
		local e = text:match("^%^x%x%x%x%x%x%x()", i) or text:match("^%^%d()", i)
		if not e then break end
		i = e
	end
	return text:sub(1, i - 1), text:sub(i)
end

local function translate_piece(d, piece, is_base_hint)
	if draw_override and draw_override[piece] then return draw_override[piece] end
	if is_base_hint then
		local clean_b = piece:gsub(" %(.+%)$", "")
		local bh = d.items[clean_b] or d.items[piece] or d.merged[clean_b]
		if bh then return bh end
	end
	local exact = d.uniques[piece] or d.items[piece] or d.gems[piece] or d.passives[piece] or d.merged[piece]
	if exact then return exact end
	if piece:find(" Support$", 1) then
		local short = piece:gsub(" Support$", "")
		if d.gems[short] then return d.gems[short] end
	end
	return nil
end

local function translate_display(s)
	if type(s) ~= "string" or s == "" or not translate_enabled or active_lang == "en" then return s end
	local d = get_active_dict()
	if not d or not d.loaded then return s end
	if draw_override and draw_override[s] then return draw_override[s] end
	if d.merged[s] then return d.merged[s] end
	local lead, rest = split_lead_color(s)
	if rest == "" then return s end
	if draw_override and draw_override[rest] then return lead .. draw_override[rest] end
	if d.merged[rest] then return lead .. d.merged[rest] end
	local lws, core, rws = rest:match("^(%s*)(.-)(%s*)$")
	if not core or core == "" then return s end
	if d.merged[core] then return lead .. lws .. d.merged[core] .. rws end
	if core:sub(-1) == ":" then
		local stem = core:sub(1, -2)
		if d.merged[stem] then return lead .. lws .. d.merged[stem] .. ":" .. rws end
	end
	if core:find(", ", 1, true) then
		local parts, count, start = {}, 0, 1
		while true do
			local si, ei = core:find(", ", start, true)
			if not si then count = count + 1; parts[count] = core:sub(start); break end
			count = count + 1; parts[count] = core:sub(start, si - 1); start = ei + 1
		end
		local changed = false
		for i = 1, count do
			local tr_p = translate_piece(d, parts[i], count == 2 and i == 2)
			if tr_p and tr_p ~= parts[i] then parts[i] = tr_p; changed = true end
		end
		if changed then return lead .. lws .. table.concat(parts, ", ") .. rws end
	end
	return s
end

local function gem_name_zh(_id, name)
	if not translate_enabled or active_lang == "en" or type(name) ~= "string" or name == "" then return nil end
	local d = get_active_dict()
	if not d or not d.loaded then return nil end
	local hit = d.gems[name] or d.gems[name .. " Support"] or d.gems[(name:gsub(" Support$", ""))]
	return (hit and hit ~= name) and hit or nil
end

local function ascendancy_name_zh(_id, name)
	if not translate_enabled or active_lang == "en" or type(name) ~= "string" or name == "" then return nil end
	local d = get_active_dict()
	if not d or not d.loaded then return nil end
	local hit = d.passives[name] or d.merged[name]
	return (hit and hit ~= name) and hit or nil
end

local function item_name_zh(it)
	if not translate_enabled or active_lang == "en" or type(it) ~= "table" then return nil end
	local d = get_active_dict()
	if not d or not d.loaded then return nil end
	local base_raw = type(it.baseName) == "string" and it.baseName or nil
	local base = base_raw and base_raw:gsub(" %(.+%)$", "") or nil
	if type(it.title) == "string" and it.title ~= "" and base then
		local t_zh = d.uniques[it.title] or d.merged[it.title] or it.title
		local b_zh = d.items[base] or d.items[base_raw] or d.merged[base] or base
		if t_zh ~= it.title or b_zh ~= base then return t_zh .. ", " .. b_zh end
	elseif base then
		local b_zh = d.items[base] or d.items[base_raw] or d.merged[base]
		if b_zh then return b_zh end
	end
	return nil
end

local function tree_data(params, dispatch)
	params = type(params) == "table" and params or {}
	if not translate_enabled or active_lang == "en" then return dispatch("tree_data", params) end
	local v = params.version
	if type(v) ~= "string" or v == "" then
		local b = H.get_build({ rebuild = false, optional = true })
		v = b and b.spec and b.spec.treeVersion or latestTreeVersion
	end
	local m = type(launch) == "table" and launch.main or nil
	local tree = type(v) == "string" and m and type(m.tree) == "table" and m:LoadTree(v)
	if not tree then return dispatch("tree_data", params) end
	local alias, aliased = v .. "@" .. active_lang, {}
	for k, val in pairs(params) do aliased[k] = val end
	aliased.version = alias
	m.tree[alias] = tree
	local ok, out = pcall(dispatch, "tree_data", aliased)
	m.tree[alias] = nil
	if not ok or type(out) ~= "table" then return dispatch("tree_data", params) end
	out.treeVersion = v
	return out
end

function M.set_locale(lang_or_params)
	local raw = type(lang_or_params) == "table" and lang_or_params.lang or lang_or_params
	local norm = M.normalize_lang(raw)
	if not norm then error("unsupported locale: " .. tostring(raw) .. " (expected zh-CN, zh-TW, or en)", 0) end
	active_lang = norm
	if norm == "en" then
		translate_enabled = false
		return { ok = true, lang = active_lang, translate = false, dictLoaded = false }
	end
	translate_enabled = true
	local subdir = LANG_SUBDIR[norm]
	if subdir then missing_subdirs[subdir] = nil end
	local d = get_active_dict()
	return {
		ok = true, lang = active_lang, translate = true,
		dictLoaded = (d ~= nil and d.loaded == true) or false,
		twFallback = (d ~= nil and d.twFallback == true) or nil,
	}
end

function M.get_locale()
	local subdir = LANG_SUBDIR[active_lang]
	local d = subdir and dict_cache[subdir] or nil
	return {
		ok = true, lang = active_lang, translate = translate_enabled and true or false,
		dictLoaded = (active_lang ~= "en" and d ~= nil and d.loaded == true) or false,
	}
end

function M.install_pre_bridge(data_root_path, opts)
	local norm = H.norm_path(data_root_path or os.getenv("POB2_DATA_ROOT") or ".")
	if norm:lower():match("/poe2$") then data_poe2_root = norm else data_poe2_root = norm .. "/poe2" end
	dict_cache, missing_subdirs, draw_override = {}, {}, nil
	local init_raw = (type(opts) == "table" and opts.lang) or os.getenv("POB2_LANG") or os.getenv("POB_LANG") or "zh-CN"
	active_lang = M.normalize_lang(init_raw) or "zh-CN"
	translate_enabled = (active_lang ~= "en")

	function _G.PobToolsSetTranslate(on) translate_enabled = (on ~= false and on ~= nil) end
	function _G.PobToolsGetTranslate() return translate_enabled and true or false end
	function _G.PobToolsSetDrawOverride(map) draw_override = type(map) == "table" and map or nil end
	_G.PobToolsTranslateDisplay = translate_display
	_G.PobToolsTranslate = translate_display
	_G.PobToolsGemNameZh = gem_name_zh
	_G.PobToolsAscendancyNameZh = ascendancy_name_zh
	_G.PobToolsItemNameZh = item_name_zh
	return true
end

M.translate_display = translate_display
M.rpc_handlers = {
	set_locale = function(p) return M.set_locale(type(p) == "table" and p.lang or p) end,
	get_locale = function(_p) return M.get_locale() end,
	tree_data = tree_data,
	translate_text = function(p)
		local txt = type(p) == "table" and p.text or ""
		return { text = txt, translated = translate_display(txt), lang = active_lang }
	end,
}

return M
