from redis import Redis

from tests.redis_databases import redis_url

#: The self-tests' Redis database, emptied around every case; one per xdist worker.
CONNECTION = Redis.from_url(redis_url("rq"))
