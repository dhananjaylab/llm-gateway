-- circuit_check.lua
--
-- Atomic "may this request through?" decision for the three-state circuit
-- breaker (Closed / Open / Half-Open), per Document 05's routing decision
-- hierarchy: "Circuit State Check: Evaluates target provider state. If
-- Closed, proceed; if Open, short-circuit directly to fallback chain."
--
-- Runs via EVALSHA (see app/core/redis_script.py) so that under
-- concurrent load, the Open -> Half-Open cooldown transition AND the
-- "exactly one probe in flight" invariant are both race-free: two
-- gateway instances hitting this script at the same moment are still
-- served one at a time by Redis's single-threaded script execution, so
-- only one of them can ever be the request that flips Open -> Half-Open
-- and claims the probe slot.
--
-- KEYS[1] = circuit:{provider}:{model}   (HASH: state, opened_at, probe_in_flight)
--
-- ARGV[1] = now              (unix seconds, float)
-- ARGV[2] = cooldown_seconds (Open -> Half-Open after this many seconds)
--
-- Returns: {allowed (0/1), state_code}
--   state_code: 0 = closed, 1 = open, 2 = half_open
--
-- No key at all (a provider-model pair never seen before) is treated
-- identically to "closed" — a circuit starts healthy by default and only
-- opens once it has actually observed failures (see circuit_record.lua).

local now = tonumber(ARGV[1])
local cooldown = tonumber(ARGV[2])

local state = redis.call('HGET', KEYS[1], 'state')

if state == false or state == 'closed' then
    return {1, 0}
end

if state == 'open' then
    local opened_at = tonumber(redis.call('HGET', KEYS[1], 'opened_at')) or now
    if (now - opened_at) >= cooldown then
        -- Cooldown elapsed: this request becomes the single Half-Open
        -- probe. Claim the probe slot in the same atomic step so no
        -- concurrent request can also become "the" probe.
        redis.call('HSET', KEYS[1], 'state', 'half_open', 'probe_in_flight', '1')
        return {1, 2}
    end
    return {0, 1}
end

if state == 'half_open' then
    local claimed = redis.call('HGET', KEYS[1], 'probe_in_flight')
    if claimed == '1' then
        -- A probe is already outstanding; everyone else waits for its
        -- outcome (record_success/record_failure) rather than piling on.
        return {0, 2}
    end
    redis.call('HSET', KEYS[1], 'probe_in_flight', '1')
    return {1, 2}
end

-- Unknown/corrupt state value: fail open (allow) rather than wedge every
-- request behind a circuit breaker bug.
return {1, 0}
