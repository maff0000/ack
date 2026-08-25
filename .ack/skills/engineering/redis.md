# Redis

Use an explicit project namespace, documented ownership/contract, bounded data, and TTL/retention policy where relevant. Prevent cross-project collisions and define safe failure behaviour. Do not treat Redis as durable truth unless explicitly designed as such. For ACK, Redis is live operational state only.
