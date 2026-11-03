local ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
local BITS_PER_CHARACTER = 6
local BITS_PER_BYTE = 8
local BYTE_MODULUS = 256
local PADDING = "="
local CHUNK_SIZE = 4096

local Base64 = {}

local VALUES: { [number]: number } = {}
for index = 1, #ALPHABET do
	VALUES[string.byte(ALPHABET, index)] = index - 1
end

-- Returns nil for input that is not valid base64
function Base64.decode(encoded: string): string?
	local stripped = string.gsub(encoded, "[\r\n]", "")
	local data = string.gsub(stripped, `{PADDING}+$`, "")
	local pieces = {}
	local bytes = {}
	local accumulator = 0
	local bitCount = 0

	for index = 1, #data do
		local value = VALUES[string.byte(data, index)]
		if not value then
			return nil
		end

		accumulator = accumulator * 64 + value
		bitCount += BITS_PER_CHARACTER
		if bitCount >= BITS_PER_BYTE then
			bitCount -= BITS_PER_BYTE
			local divisor = 2 ^ bitCount
			table.insert(bytes, math.floor(accumulator / divisor) % BYTE_MODULUS)
			accumulator = accumulator % divisor
			if #bytes >= CHUNK_SIZE then
				table.insert(pieces, string.char(table.unpack(bytes)))
				table.clear(bytes)
			end
		end
	end

	if #bytes > 0 then
		table.insert(pieces, string.char(table.unpack(bytes)))
	end
	return table.concat(pieces)
end

return Base64
