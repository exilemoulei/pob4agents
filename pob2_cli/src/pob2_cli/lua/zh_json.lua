-- Fast flat string-map JSON reader for PobTools PoE2 zh dictionaries ({"entries": {...}, "templates": {...}}).
local bit = require("bit")

local ESC_BYTE = {
	[34] = '"', [92] = '\\', [47] = '/',
	[98] = '\b', [102] = '\f', [110] = '\n', [114] = '\r', [116] = '\t',
}

local function utf8_from_cp(cp)
	if cp < 0x80 then return string.char(cp)
	elseif cp < 0x800 then return string.char(0xC0 + bit.rshift(cp, 6), 0x80 + bit.band(cp, 0x3F))
	elseif cp < 0x10000 then
		return string.char(
			0xE0 + bit.rshift(cp, 12),
			0x80 + bit.band(bit.rshift(cp, 6), 0x3F),
			0x80 + bit.band(cp, 0x3F)
		)
	end
	return string.char(
		0xF0 + bit.rshift(cp, 18),
		0x80 + bit.band(bit.rshift(cp, 12), 0x3F),
		0x80 + bit.band(bit.rshift(cp, 6), 0x3F),
		0x80 + bit.band(cp, 0x3F)
	)
end

local function unesc_json_str(s)
	local bs = s:find("\\", 1, true)
	if not bs then return s end
	local out, n, pos, len = {}, 0, 1, #s
	while bs do
		if bs > pos then n = n + 1; out[n] = s:sub(pos, bs - 1) end
		local nb = s:byte(bs + 1)
		if not nb then pos = len + 1; break end
		local rep = ESC_BYTE[nb]
		if rep then
			n = n + 1; out[n] = rep; pos = bs + 2
		elseif nb == 117 and bs + 5 <= len then
			local cp = tonumber(s:sub(bs + 2, bs + 5), 16)
			if cp then
				if cp >= 0xD800 and cp <= 0xDBFF and s:byte(bs + 6) == 92 and s:byte(bs + 7) == 117 and bs + 11 <= len then
					local lo = tonumber(s:sub(bs + 8, bs + 11), 16)
					if lo and lo >= 0xDC00 and lo <= 0xDFFF then
						cp = 0x10000 + bit.lshift(cp - 0xD800, 10) + (lo - 0xDC00)
						n = n + 1; out[n] = utf8_from_cp(cp); pos = bs + 12
					else
						n = n + 1; out[n] = utf8_from_cp(cp); pos = bs + 6
					end
				else
					n = n + 1; out[n] = utf8_from_cp(cp); pos = bs + 6
				end
			else
				n = n + 1; out[n] = "u"; pos = bs + 2
			end
		else
			n = n + 1; out[n] = string.char(nb); pos = bs + 2
		end
		bs = s:find("\\", pos, true)
	end
	if pos <= len then n = n + 1; out[n] = s:sub(pos) end
	return table.concat(out)
end

local function parse_json_str(buf, pos, len)
	local start = pos
	while pos <= len do
		local b = buf:byte(pos)
		if b == 34 then return unesc_json_str(buf:sub(start, pos - 1)), pos + 1
		elseif b == 92 then pos = pos + 2
		else pos = pos + 1 end
	end
	return nil, pos
end

local function skip_ws(buf, pos, len)
	while pos <= len do
		local b = buf:byte(pos)
		if b == 32 or b == 9 or b == 10 or b == 13 then pos = pos + 1 else break end
	end
	return pos
end

local function load_flat_section(buf, key_name, target)
	local needle = '"' .. key_name .. '"'
	local search_from, len, brace = 1, #buf, nil
	while search_from <= len do
		local _, k_end = buf:find(needle, search_from, true)
		if not k_end then return target end
		local p = skip_ws(buf, k_end + 1, len)
		if buf:byte(p) == 58 then
			p = skip_ws(buf, p + 1, len)
			if buf:byte(p) == 123 then brace = p; break end
		end
		search_from = k_end + 1
	end
	if not brace then return target end
	local pos = brace + 1
	target = target or {}
	while pos <= len do
		local b = buf:byte(pos)
		if b == 125 then break
		elseif b == 34 then
			local k, p1 = parse_json_str(buf, pos + 1, len)
			if not k then break end
			pos = skip_ws(buf, p1, len)
			if buf:byte(pos) ~= 58 then break end
			pos = skip_ws(buf, pos + 1, len)
			if buf:byte(pos) == 34 then
				local v, p2 = parse_json_str(buf, pos + 1, len)
				if not v then break end
				target[k] = v; pos = p2
			else
				while pos <= len and buf:byte(pos) ~= 44 and buf:byte(pos) ~= 125 do pos = pos + 1 end
			end
		else
			pos = pos + 1
		end
	end
	return target
end

local function read_file_bytes(path)
	local f = io.open(path, "rb")
	if not f then return nil end
	local data = f:read("*a")
	f:close()
	return data
end

local cache = {}
local function load_file(path)
	local c = cache[path]
	if c then return c end
	local raw = read_file_bytes(path)
	if not raw then return nil end
	c = {
		entries = load_flat_section(raw, "entries", {}),
		templates = load_flat_section(raw, "templates", nil) or false,
	}
	cache[path] = c
	return c
end

local function entries(dir, filename)
	local c = load_file(dir .. "/" .. filename)
	return c and c.entries
end

local function load_entries_into(dir, filename, primary_tbl, merged_tbl)
	local e = entries(dir, filename)
	if not e then return false end
	for k, v in pairs(e) do
		if primary_tbl then primary_tbl[k] = v end
		if merged_tbl then merged_tbl[k] = v end
	end
	return true
end

local function load_id_table(dir, filename)
	local c = load_file(dir .. "/" .. filename)
	if not c then return nil, nil end
	return c.entries, c.templates or nil
end

return {
	read_file_bytes = read_file_bytes,
	entries = entries,
	load_entries_into = load_entries_into,
	load_id_table = load_id_table,
}
