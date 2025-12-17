"""Lua scripts for atomic Redis operations."""

# Acquire script - calculates slot time, records consumption, handles FIFO ordering
# Supports all three limit types: combined TPM, input TPM, output TPM
ACQUIRE_SCRIPT = """
-- KEYS[1]: consumption_key
-- ARGV[1]: input_tokens
-- ARGV[2]: output_tokens
-- ARGV[3]: tpm_limit (combined, 0 = disabled)
-- ARGV[4]: input_tpm_limit (0 = disabled)
-- ARGV[5]: output_tpm_limit (0 = disabled)
-- ARGV[6]: rpm_limit (0 = disabled)
-- ARGV[7]: window_seconds
-- ARGV[8]: current_time
-- ARGV[9]: record_id
-- ARGV[10]: effective_combined_tokens (pre-calculated with burndown rate on Python side)
-- ARGV[11]: rps_limit (0 = disabled, for RPS smoothing)
-- ARGV[12]: smoothing_interval (seconds, e.g., 1.0)

local consumption_key = KEYS[1]
local input_tokens = tonumber(ARGV[1])
local output_tokens = tonumber(ARGV[2])
local tpm_limit = tonumber(ARGV[3])
local input_tpm_limit = tonumber(ARGV[4])
local output_tpm_limit = tonumber(ARGV[5])
local rpm_limit = tonumber(ARGV[6])
local window_seconds = tonumber(ARGV[7])
local current_time = tonumber(ARGV[8])
local record_id = ARGV[9]
local effective_combined_tokens = tonumber(ARGV[10])
local rps_limit = tonumber(ARGV[11])
local smoothing_interval = tonumber(ARGV[12])

local cutoff_time = current_time - window_seconds
local epsilon = 0.001  -- 1ms FIFO spacing

-- STEP 1: Cleanup expired entries (slot_time < cutoff)
redis.call('ZREMRANGEBYSCORE', consumption_key, '-inf', cutoff_time)

-- STEP 2: Get all active records (past in window + future pending)
-- Sorted by slot_time ascending
local records = redis.call('ZRANGE', consumption_key, 0, -1, 'WITHSCORES')

local current_effective_combined = 0  -- With burndown rate applied (stored in each record)
local current_input = 0
local current_output = 0
local current_requests = 0
local last_slot_time = current_time
local record_list = {}  -- Store for wait time calculation

for i = 1, #records, 2 do
    local entry = cjson.decode(records[i])
    local slot_time = tonumber(records[i + 1])

    local entry_input = entry.input_tokens or entry.tokens or 0
    local entry_output = entry.output_tokens or 0
    -- Use stored effective_combined_tokens, fallback to input+output for legacy records
    local entry_effective_combined = entry.effective_combined_tokens or (entry_input + entry_output)

    current_input = current_input + entry_input
    current_output = current_output + entry_output
    current_effective_combined = current_effective_combined + entry_effective_combined
    current_requests = current_requests + 1
    last_slot_time = math.max(last_slot_time, slot_time)

    -- Store for later: {slot_time, input, output, effective_combined, expiry_time}
    table.insert(record_list, {
        slot_time = slot_time,
        input_tokens = entry_input,
        output_tokens = entry_output,
        effective_combined_tokens = entry_effective_combined,
        expiry_time = slot_time + window_seconds
    })
end

-- STEP 3: Calculate capacity needed for each constraint
-- Positive = over limit, need to wait for capacity
local combined_needed = 0
local input_needed = 0
local output_needed = 0
local requests_needed = 0

if tpm_limit > 0 then
    -- Use effective combined tokens (with burndown rate) for combined TPM limit
    combined_needed = effective_combined_tokens - (tpm_limit - current_effective_combined)
end
if input_tpm_limit > 0 then
    input_needed = input_tokens - (input_tpm_limit - current_input)
end
if output_tpm_limit > 0 then
    output_needed = output_tokens - (output_tpm_limit - current_output)
end
if rpm_limit > 0 then
    requests_needed = 1 - (rpm_limit - current_requests)
end

-- STEP 4: FIFO - start after the last request
local your_slot_time = last_slot_time + epsilon

-- STEP 4.5: RPS smoothing - enforce minimum interval between requests
if rps_limit > 0 and smoothing_interval > 0 then
    local min_interval = smoothing_interval / rps_limit
    local min_slot_time = last_slot_time + min_interval
    your_slot_time = math.max(your_slot_time, min_slot_time)
end

-- STEP 5: If over any capacity, find when ENOUGH capacity frees up
if combined_needed > 0 or input_needed > 0 or output_needed > 0 or requests_needed > 0 then

    -- Sort records by expiry_time (when they free up capacity)
    table.sort(record_list, function(a, b)
        return a.expiry_time < b.expiry_time
    end)

    local freed_effective_combined = 0
    local freed_input = 0
    local freed_output = 0
    local freed_requests = 0
    local required_expiry_time = current_time

    -- Walk through records until we have enough capacity for ALL constraints
    for _, rec in ipairs(record_list) do
        freed_effective_combined = freed_effective_combined + rec.effective_combined_tokens
        freed_input = freed_input + rec.input_tokens
        freed_output = freed_output + rec.output_tokens
        freed_requests = freed_requests + 1

        -- Check if we've freed enough for ALL constraints
        local combined_ok = (tpm_limit == 0) or (combined_needed <= 0) or (freed_effective_combined >= combined_needed)
        local input_ok = (input_tpm_limit == 0) or (input_needed <= 0) or (freed_input >= input_needed)
        local output_ok = (output_tpm_limit == 0) or (output_needed <= 0) or (freed_output >= output_needed)
        local requests_ok = (rpm_limit == 0) or (requests_needed <= 0) or (freed_requests >= requests_needed)

        if combined_ok and input_ok and output_ok and requests_ok then
            required_expiry_time = rec.expiry_time
            break
        end
    end

    your_slot_time = math.max(your_slot_time, required_expiry_time + epsilon)
end

-- STEP 6: Record consumption at slot_time
local record = cjson.encode({
    input_tokens = input_tokens,
    output_tokens = output_tokens,
    effective_combined_tokens = effective_combined_tokens,
    record_id = record_id,
    created_at = current_time
})
redis.call('ZADD', consumption_key, your_slot_time, record)
redis.call('EXPIRE', consumption_key, window_seconds * 2)

-- STEP 7: Return results
local queue_position = current_requests
local wait_time = your_slot_time - current_time
if wait_time < 0 then wait_time = 0 end

return {your_slot_time, queue_position, record_id, wait_time}
"""

# Adjust script - updates output tokens for a record (split TPM only)
ADJUST_SCRIPT = """
-- KEYS[1]: consumption_key
-- ARGV[1]: record_id
-- ARGV[2]: new_output_tokens

local consumption_key = KEYS[1]
local record_id = ARGV[1]
local new_output_tokens = tonumber(ARGV[2])

-- Find and update the record
local records = redis.call('ZRANGE', consumption_key, 0, -1, 'WITHSCORES')

for i = 1, #records, 2 do
    local entry_json = records[i]
    local slot_time = tonumber(records[i + 1])
    local success, entry = pcall(cjson.decode, entry_json)

    if success and entry and entry.record_id == record_id then
        -- Update output_tokens
        entry.output_tokens = new_output_tokens

        -- Remove old and add updated
        redis.call('ZREM', consumption_key, entry_json)
        redis.call('ZADD', consumption_key, slot_time, cjson.encode(entry))

        return {1, 'updated'}
    end
end

return {0, 'not_found'}
"""

# Status script - gets current consumption stats
STATUS_SCRIPT = """
-- KEYS[1]: consumption_key
-- ARGV[1]: current_time
-- ARGV[2]: window_seconds

local consumption_key = KEYS[1]
local current_time = tonumber(ARGV[1])
local window_seconds = tonumber(ARGV[2])

local cutoff_time = current_time - window_seconds

-- Cleanup expired entries
redis.call('ZREMRANGEBYSCORE', consumption_key, '-inf', cutoff_time)

-- Get all active records
local records = redis.call('ZRANGE', consumption_key, 0, -1, 'WITHSCORES')

local total_input = 0
local total_output = 0
local total_requests = 0
local queue_depth = 0

for i = 1, #records, 2 do
    local entry = cjson.decode(records[i])
    local slot_time = tonumber(records[i + 1])

    local entry_input = entry.input_tokens or entry.tokens or 0
    local entry_output = entry.output_tokens or 0

    total_input = total_input + entry_input
    total_output = total_output + entry_output
    total_requests = total_requests + 1

    if slot_time > current_time then
        queue_depth = queue_depth + 1
    end
end

return {total_input, total_output, total_requests, queue_depth}
"""
