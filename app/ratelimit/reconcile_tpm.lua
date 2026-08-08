-- reconcile_tpm.lua
--
-- Phase 2 of the two-phase reservation protocol (TRD, "Post-Execution
-- Reconciliation"): once the real usage is known, refund the difference
-- between what was reserved and what was actually consumed back into the
-- TPM bucket, capped at capacity. Never called for RPM — RPM is a request
-- *count*, not a token measure, so there is nothing to reconcile on that
-- axis (see app/ratelimit/limiter.py::reconcile).
--
-- KEYS[1] = rl:{team_id}:tpm
--
-- ARGV[1] = refund_amount   (reserved - actual; may be <= 0, meaning the
--                             call used at least as many tokens as
--                             reserved — silently accepted as an overage,
--                             not clawed back, since the client already
--                             received the response)
-- ARGV[2] = capacity
-- ARGV[3] = key_ttl_seconds
--
-- Returns the bucket's tokens value after the refund (or the unmodified
-- value if refund_amount <= 0 or the key doesn't exist yet).

local refund = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])

if refund <= 0 then
    return -1
end

local tokens = tonumber(redis.call('HGET', KEYS[1], 'tokens'))
if tokens == nil then
    -- Bucket doesn't exist (expired or never touched) — nothing to
    -- refund into; the next check() call will re-seed it at full capacity.
    return -1
end

tokens = math.min(capacity, tokens + refund)
redis.call('HSET', KEYS[1], 'tokens', tokens)
redis.call('EXPIRE', KEYS[1], ttl)

return tokens
