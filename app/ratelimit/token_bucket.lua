-- token_bucket.lua
--
-- Atomic dual-axis (RPM + TPM) token bucket check-and-consume, per
-- Document 05 (Backend Schema) and the TRD's "Atomic Token Bucket via
-- Redis Lua Scripting" section. Runs entirely inside Redis via
-- EVALSHA (see app/core/redis_script.py) so concurrent gateway instances
-- checking the same team's bucket can never both observe "tokens
-- available" and both decrement past zero — this is the whole reason the
-- refill-check-consume sequence has to be one script, not three round trips.
--
-- KEYS[1] = rl:{team_id}:rpm   (HASH: tokens, last_update)
-- KEYS[2] = rl:{team_id}:tpm   (HASH: tokens, last_update)
--
-- ARGV[1] = now                        (unix seconds, float)
-- ARGV[2] = rpm_capacity
-- ARGV[3] = rpm_refill_per_second      (= rpm_capacity / 60)
-- ARGV[4] = tpm_capacity
-- ARGV[5] = tpm_refill_per_second      (= tpm_capacity / 60)
-- ARGV[6] = requested_rpm              (1 per request; 0 for a read-only "peek")
-- ARGV[7] = requested_tpm              (estimated reservation for this request)
-- ARGV[8] = key_ttl_seconds            (Document 05: 7200s, auto-expire idle teams)
--
-- Returns: {allowed (0/1), remaining_rpm, remaining_tpm, retry_after_seconds}
--
-- Passing ARGV[6]=0 and ARGV[7]=0 turns this into a non-destructive
-- "peek": refill still applies and is persisted (so idle buckets keep
-- accruing), nothing is consumed, and `allowed` is always 1. The Admin
-- API's GET /admin/limits/{team} reuses this exact script that way
-- instead of duplicating the refill math in a second script.

local function refill(key, capacity, refill_rate, now)
    local data = redis.call('HMGET', key, 'tokens', 'last_update')
    local tokens = tonumber(data[1])
    local last_update = tonumber(data[2])

    if tokens == nil then
        tokens = capacity
        last_update = now
    else
        local delta = now - last_update
        if delta < 0 then
            delta = 0
        end
        tokens = math.min(capacity, tokens + delta * refill_rate)
        last_update = now
    end

    return tokens, last_update
end

local now = tonumber(ARGV[1])
local rpm_capacity = tonumber(ARGV[2])
local rpm_refill_rate = tonumber(ARGV[3])
local tpm_capacity = tonumber(ARGV[4])
local tpm_refill_rate = tonumber(ARGV[5])
local requested_rpm = tonumber(ARGV[6])
local requested_tpm = tonumber(ARGV[7])
local ttl = tonumber(ARGV[8])

local rpm_tokens, rpm_last = refill(KEYS[1], rpm_capacity, rpm_refill_rate, now)
local tpm_tokens, tpm_last = refill(KEYS[2], tpm_capacity, tpm_refill_rate, now)

local allowed = 0
if rpm_tokens >= requested_rpm and tpm_tokens >= requested_tpm then
    allowed = 1
    rpm_tokens = rpm_tokens - requested_rpm
    tpm_tokens = tpm_tokens - requested_tpm
end

-- Persist the refilled state whether or not the request was admitted —
-- otherwise a client hammering an empty bucket would never observe
-- refill, since last_update would never advance past the first failure.
redis.call('HMSET', KEYS[1], 'tokens', rpm_tokens, 'last_update', rpm_last)
redis.call('HMSET', KEYS[2], 'tokens', tpm_tokens, 'last_update', tpm_last)
redis.call('EXPIRE', KEYS[1], ttl)
redis.call('EXPIRE', KEYS[2], ttl)

if allowed == 1 then
    return {1, math.floor(rpm_tokens), math.floor(tpm_tokens), 0}
end

local rpm_wait = 0
if rpm_tokens < requested_rpm and rpm_refill_rate > 0 then
    rpm_wait = (requested_rpm - rpm_tokens) / rpm_refill_rate
end
local tpm_wait = 0
if tpm_tokens < requested_tpm and tpm_refill_rate > 0 then
    tpm_wait = (requested_tpm - tpm_tokens) / tpm_refill_rate
end

local retry_after = math.ceil(math.max(rpm_wait, tpm_wait))
if retry_after < 1 then
    retry_after = 1
end

return {0, math.floor(rpm_tokens), math.floor(tpm_tokens), retry_after}
