-- combined_check.lua
--
-- Phase 7 optimization (Doc 1's own bottleneck table: "Sequential Redis
-- calls for RPM, TPM, and Budget evaluation... Network RTT accumulation
-- on hot request paths"). Evaluates the budget precheck AND the RPM/TPM
-- token bucket in ONE atomic Redis round trip, instead of the two
-- sequential calls BudgetEnforcer.precheck() + RateLimiter.check() used
-- to require.
--
-- ORDERING/ATOMICITY GUARANTEE PRESERVED FROM THE TWO-CALL VERSION:
-- budget is evaluated FIRST and is READ-ONLY -- a request that's over
-- budget must never consume any rate-limit capacity (see
-- app/ratelimit/budget.py's own module docstring on why precheck never
-- mutates state). Only if the budget check passes does this script fall
-- through to the token-bucket refill-check-consume logic -- duplicated
-- from token_bucket.lua, not called as a sub-script (Redis Lua has no
-- cross-script call primitive without a second EVALSHA round trip, which
-- would defeat the entire point of combining these).
--
-- SAFETY NOTE (see app/ratelimit/combined.py's own module docstring for
-- the full reasoning): combining these into one Redis call means a Redis
-- outage can no longer fail budget (closed) and rate-limiting (open)
-- independently the way the two separate calls do. The Python wrapper
-- treats this script as a FAST PATH ONLY and falls back to the original
-- two separate calls on any Redis connectivity error, so that asymmetry
-- is preserved on the failure path -- this script itself only ever runs
-- when Redis is healthy.
--
-- KEYS[1] = rl:{team_id}:rpm
-- KEYS[2] = rl:{team_id}:tpm
-- KEYS[3] = budget:{team_id}:{period}
--
-- ARGV[1] = now                        (unix seconds, float)
-- ARGV[2] = rpm_capacity
-- ARGV[3] = rpm_refill_per_second
-- ARGV[4] = tpm_capacity
-- ARGV[5] = tpm_refill_per_second
-- ARGV[6] = requested_rpm              (1 per request)
-- ARGV[7] = requested_tpm              (estimated reservation)
-- ARGV[8] = rl_key_ttl_seconds
-- ARGV[9] = budget_cap_usd             (the LIVE team.budget_cap_usd --
--           per the Phase 5 bugfix in budget_increment.lua/budget.py,
--           NEVER a stale stored cap; the caller always passes the
--           current team config value, same as budget.py's own precheck)
--
-- Returns: {allowed, budget_denied, remaining_rpm, remaining_tpm, retry_after, spend_usd}
--   remaining_rpm/remaining_tpm/retry_after are -1 when budget_denied=1
--   -- the rate-limit buckets are never touched in that case, matching
--   the pre-Phase-7 behavior where RateLimiter.check() was never even
--   called on a budget rejection.

local spend = tonumber(redis.call('HGET', KEYS[3], 'spend_usd')) or 0
local cap = tonumber(ARGV[9])

if cap > 0 and spend >= cap then
    return {0, 1, -1, -1, -1, spend}
end

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

redis.call('HMSET', KEYS[1], 'tokens', rpm_tokens, 'last_update', rpm_last)
redis.call('HMSET', KEYS[2], 'tokens', tpm_tokens, 'last_update', tpm_last)
redis.call('EXPIRE', KEYS[1], ttl)
redis.call('EXPIRE', KEYS[2], ttl)

if allowed == 1 then
    return {1, 0, math.floor(rpm_tokens), math.floor(tpm_tokens), 0, spend}
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

return {0, 0, math.floor(rpm_tokens), math.floor(tpm_tokens), retry_after, spend}
